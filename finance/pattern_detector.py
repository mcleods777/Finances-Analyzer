"""
Pure pattern-detection logic for the AI briefing (Phase 1 of the co-pilot
design). No IO here: detectors consume the post-classification DataFrame
(the exact shape data_service.refresh_data() caches), the AppConfig, and a
briefing-state dict (see finance/briefing_state.py) — and return pattern
dicts. Persisting seen-merchants etc. is the briefing writer's job (Phase 2).

Every detector is a plain function:

    detect_<name>(df, config, state, today=None) -> list[dict]

returning zero or more pattern dicts of the standard shape:

    {
      "pattern_type": str,        # e.g. "category_delta"
      "magnitude": float,         # ranking score; formula per detector
      "direction": str,           # "up" | "down" | "new" | "missing"
      "raw_facts": dict,          # category, amounts, dates, merchant, ...
      "drill_down_filter": dict,  # see _drill_down()
      "headline": str,            # short factual sentence (LLM-off fallback)
    }

`drill_down_filter` schema (multi-category aware, aligned with the
/transactions filters):

    {
      "category": list[str] | None,   # -> ?category=A,B
      "account": list[str] | None,    # -> ?account=X,Y
      "start_date": str | None,       # ISO date -> ?start=YYYY-MM-DD
      "end_date": str | None,         # ISO date -> ?end=YYYY-MM-DD
      "search": str | None,           # -> ?search=... (merchant drill-down)
    }

Cold-start behavior: when a detector's min-history gate is not met it
returns an empty list, never an error.

The canonical comparison window is the *current pay period vs the prior pay
period*, using the same period-bucketing math as analytics.biweekly_spending
/ compute_runway (days since pay_period.start_date // frequency_days).

`today` is injectable for tests; it defaults to the real clock. Note that
detect_missing_recurring and detect_runway_variance delegate to
analytics.get_recurring_bill_status, which reads the real clock internally —
tests for those pass today=date.today() and build data relative to it.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import pandas as pd

from finance.analytics import biweekly_income, get_recurring_bill_status
from finance.config_loader import AppConfig

logger = logging.getLogger(__name__)

UNCATEGORIZED = "Uncategorized"

# --- Thresholds / gates (per design doc; tuned in production later) ---

MIN_DAY_OF_PERIOD = 4  # 1-indexed: period start date is day 1

CATEGORY_DELTA_BASELINE_FLOOR = 25.0  # $ floor on the prior-period baseline
CATEGORY_DELTA_MIN_PCT = 0.20
CATEGORY_DELTA_MIN_DOLLARS = 25.0

ANOMALY_WINDOW_DAYS = 60
ANOMALY_MIN_OBSERVATIONS = 30  # days with spend activity in the window
ANOMALY_MIN_STDEV = 10.0  # $ noise floor
ANOMALY_Z_THRESHOLD = 2.0
ANOMALY_Z_CAP = 5.0

NEW_RECURRING_WINDOW_DAYS = 60
NEW_RECURRING_MIN_CHARGES = 2
NEW_RECURRING_MIN_HISTORY_DAYS = 60

MISSING_GRACE_DAYS = 3

RUNWAY_MIN_DRIFT_DOLLARS = 50.0
RUNWAY_MIN_DRIFT_PCT = 0.10

TOP_MOVERS_COUNT = 3

UNCAT_MIN_COUNT = 10
UNCAT_MIN_DOLLARS = 200.0
UNCAT_TOP_MERCHANTS = 3


# --- Shared helpers ---


def _pattern(
    pattern_type: str,
    magnitude: float,
    direction: str,
    raw_facts: dict,
    drill_down_filter: dict,
    headline: str,
) -> dict:
    return {
        "pattern_type": pattern_type,
        "magnitude": round(float(magnitude), 4),
        "direction": direction,
        "raw_facts": raw_facts,
        "drill_down_filter": drill_down_filter,
        "headline": headline,
    }


def _drill_down(
    category: list[str] | None = None,
    account: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    search: str | None = None,
) -> dict:
    """Standard drill-down filter dict (keys map to /transactions params:
    category->category, account->account, start_date->start, end_date->end,
    search->search)."""
    return {
        "category": category,
        "account": account,
        "start_date": start_date,
        "end_date": end_date,
        "search": search,
    }


def _period_bounds(pay_period, today: date) -> tuple[date, date, int]:
    """
    (period_start, period_end, day_of_period) for the pay period containing
    `today`. Mirrors analytics.compute_runway / biweekly_spending bucketing.
    day_of_period is 1-indexed: today == period_start -> day 1.
    """
    freq = pay_period.frequency_days
    days_since_start = (today - pay_period.start_date).days
    period_index = days_since_start // freq
    period_start = pay_period.start_date + timedelta(days=period_index * freq)
    period_end = period_start + timedelta(days=freq - 1)
    day_of_period = (today - period_start).days + 1
    return period_start, period_end, day_of_period


def _normalize_subcategory(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of df with NaN/empty subcategory rolled into 'Uncategorized'
    (same convention as analytics.get_category_trends)."""
    df = df.copy()
    df["subcategory"] = df["subcategory"].fillna(UNCATEGORIZED)
    df.loc[df["subcategory"] == "", "subcategory"] = UNCATEGORIZED
    return df


def _expenses(df: pd.DataFrame) -> pd.DataFrame:
    """Expense rows (transfers and income excluded) with normalized subcategory."""
    return _normalize_subcategory(df[df["category"] == "expense"])


def _category_totals(expenses: pd.DataFrame, start: date, end: date) -> dict[str, float]:
    """Abs-sum expense totals per subcategory within [start, end]."""
    day = expenses["date"].dt.date
    window = expenses[(day >= start) & (day <= end)]
    if window.empty:
        return {}
    totals = window.groupby("subcategory")["amount"].apply(lambda x: float(x.abs().sum()))
    return totals.to_dict()


def _has_prior_period_history(df: pd.DataFrame, period_start: date) -> bool:
    """Min-history gate: at least one transaction dated before the current
    pay period (i.e. the prior period is at least partially observed)."""
    return df["date"].min().date() < period_start


# --- Detectors ---


def detect_category_delta(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Per-subcategory spend, current pay period vs prior pay period.
    pct_change = (curr - prior) / max(prior, $25 floor); surface only if
    abs(pct_change) >= 20% AND abs(curr - prior) >= $25.
    Magnitude = abs(pct_change) * sqrt(abs(delta)).
    Gates: 1 prior pay period of history AND day-of-period >= 4.
    """
    today = today or date.today()
    if df is None or df.empty:
        return []
    period_start, period_end, day_of_period = _period_bounds(config.pay_period, today)
    if day_of_period < MIN_DAY_OF_PERIOD:
        return []
    if not _has_prior_period_history(df, period_start):
        return []

    freq = config.pay_period.frequency_days
    prior_start = period_start - timedelta(days=freq)
    prior_end = period_start - timedelta(days=1)

    expenses = _expenses(df)
    curr = _category_totals(expenses, period_start, period_end)
    prior = _category_totals(expenses, prior_start, prior_end)

    patterns = []
    for cat in sorted(set(curr) | set(prior)):
        if cat == UNCATEGORIZED:  # handled by detect_uncategorized_creep
            continue
        c = curr.get(cat, 0.0)
        p = prior.get(cat, 0.0)
        delta = c - p
        if abs(delta) < CATEGORY_DELTA_MIN_DOLLARS:
            continue
        pct_change = delta / max(p, CATEGORY_DELTA_BASELINE_FLOOR)
        if abs(pct_change) < CATEGORY_DELTA_MIN_PCT:
            continue
        direction = "up" if delta > 0 else "down"
        magnitude = abs(pct_change) * math.sqrt(abs(delta))
        headline = (
            f"{cat} spending is {direction} {abs(pct_change) * 100:.0f}% vs the prior "
            f"pay period (${c:,.2f} vs ${p:,.2f})."
        )
        patterns.append(
            _pattern(
                "category_delta",
                magnitude,
                direction,
                {
                    "category": cat,
                    "current_total": round(c, 2),
                    "prior_total": round(p, 2),
                    "delta": round(delta, 2),
                    "pct_change": round(pct_change, 4),
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "prior_period_start": prior_start.isoformat(),
                    "prior_period_end": prior_end.isoformat(),
                },
                _drill_down(
                    category=[cat],
                    start_date=period_start.isoformat(),
                    end_date=period_end.isoformat(),
                ),
                headline,
            )
        )
    return patterns


def detect_anomaly(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Per-category daily spend totals over the last 60 days: flag days where
    abs(daily_total - mean) > 2 * stdev. Magnitude = z-score capped at 5.
    Gates: >= 60 days of history, N >= 30 daily observations per category,
    stdev >= $10 (noise floor).
    Refund netting: same-day same-merchant transactions net together first
    (a purchase + same-day refund at one merchant contributes $0).
    """
    today = today or date.today()
    if df is None or df.empty:
        return []
    if (today - df["date"].min().date()).days < ANOMALY_WINDOW_DAYS:
        return []

    window_start = today - timedelta(days=ANOMALY_WINDOW_DAYS)
    # Keep income rows so a refund (often classified income) can net against
    # its same-day purchase; transfers are excluded per app convention.
    dfx = _normalize_subcategory(df[df["category"] != "transfer"])
    day = dfx["date"].dt.date
    dfx = dfx[(day >= window_start) & (day <= today)].copy()
    if dfx.empty:
        return []
    dfx["day"] = dfx["date"].dt.date

    # Net same-day same-merchant, then keep only net spend (negative nets).
    netted = (
        dfx.groupby(["subcategory", "day", "description"])["amount"].sum().reset_index()
    )
    spend = netted[netted["amount"] < 0]
    if spend.empty:
        return []
    daily = (
        spend.groupby(["subcategory", "day"])["amount"].sum().abs().reset_index()
    )

    patterns = []
    for cat, grp in daily.groupby("subcategory"):
        if len(grp) < ANOMALY_MIN_OBSERVATIONS:
            continue
        mean = float(grp["amount"].mean())
        stdev = float(grp["amount"].std())  # sample stdev (ddof=1)
        if pd.isna(stdev) or stdev < ANOMALY_MIN_STDEV:
            continue
        flagged = grp[(grp["amount"] - mean).abs() > ANOMALY_Z_THRESHOLD * stdev]
        for row in flagged.itertuples(index=False):
            total = float(row.amount)
            z = min(abs(total - mean) / stdev, ANOMALY_Z_CAP)
            direction = "up" if total > mean else "down"
            day_iso = row.day.isoformat()
            headline = (
                f"Unusual {cat} day on {day_iso}: ${total:,.2f} vs a typical "
                f"${mean:,.2f}/day."
            )
            patterns.append(
                _pattern(
                    "anomaly",
                    z,
                    direction,
                    {
                        "category": cat,
                        "date": day_iso,
                        "daily_total": round(total, 2),
                        "mean_daily_total": round(mean, 2),
                        "stdev": round(stdev, 2),
                        "z_score": round(abs(total - mean) / stdev, 4),
                    },
                    _drill_down(category=[cat], start_date=day_iso, end_date=day_iso),
                    headline,
                )
            )
    return patterns


def detect_new_recurring(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Merchants with >= 2 expense charges in the last 60 days that are neither
    in the seen-merchants set nor matched by an existing RecurringBill.
    Magnitude = monthly run rate = avg_amount * 30 / avg_days_between_charges.
    Gate: >= 60 days of history.

    Pure detection: this never persists to the seen-set — the briefing
    writer (Phase 2) records surfaced merchants via briefing_state.
    """
    today = today or date.today()
    if df is None or df.empty:
        return []
    if (today - df["date"].min().date()).days < NEW_RECURRING_MIN_HISTORY_DAYS:
        return []

    window_start = today - timedelta(days=NEW_RECURRING_WINDOW_DAYS)
    day = df["date"].dt.date
    charges = df[
        (df["category"] == "expense") & (day >= window_start) & (day <= today)
    ].copy()
    if charges.empty:
        return []
    charges["merchant_key"] = charges["description"].fillna("").str.strip().str.lower()
    charges = charges[charges["merchant_key"] != ""]

    seen = {
        str(key).strip().lower()
        for key in (state or {}).get("seen_recurring_merchants", {})
    }
    bill_criteria = [
        criteria
        for bill in config.recurring_bills
        for criteria in bill.match_criteria
    ]

    patterns = []
    for key, grp in charges.groupby("merchant_key"):
        if len(grp) < NEW_RECURRING_MIN_CHARGES:
            continue
        if key in seen:
            continue
        if any(criteria in key for criteria in bill_criteria):
            continue  # already tracked as a RecurringBill
        dates = sorted(grp["date"].dt.date)
        span_days = (dates[-1] - dates[0]).days
        avg_gap = span_days / (len(dates) - 1)
        if avg_gap < 1:
            continue  # same-day duplicates, not a cadence
        avg_amount = float(grp["amount"].abs().mean())
        run_rate = avg_amount * 30 / avg_gap
        merchant = str(grp.sort_values("date")["description"].iloc[-1]).strip()
        headline = (
            f"New recurring charge detected: {merchant} — {len(grp)} charges in the "
            f"last 60 days averaging ${avg_amount:,.2f} (~${run_rate:,.2f}/mo)."
        )
        patterns.append(
            _pattern(
                "new_recurring",
                run_rate,
                "new",
                {
                    "merchant": merchant,
                    "merchant_key": key,
                    "charge_count": int(len(grp)),
                    "first_date": dates[0].isoformat(),
                    "last_date": dates[-1].isoformat(),
                    "avg_amount": round(avg_amount, 2),
                    "avg_days_between_charges": round(avg_gap, 2),
                    "monthly_run_rate": round(run_rate, 2),
                },
                _drill_down(
                    search=merchant,
                    start_date=window_start.isoformat(),
                    end_date=today.isoformat(),
                ),
                headline,
            )
        )
    return patterns


def detect_missing_recurring(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Recurring bills due this pay period that are still unpaid past
    due_date + 3-day grace. Reuses analytics.get_recurring_bill_status
    (which is 'missed' == still pending after the grace window).
    Magnitude = the bill's amount.
    Gate: at least 1 prior occurrence of the bill in history.
    """
    today = today or date.today()
    if df is None or df.empty or not config.recurring_bills:
        return []
    period_start, period_end, _ = _period_bounds(config.pay_period, today)

    statuses = get_recurring_bill_status(df, config.pay_period, config.recurring_bills)
    bills_by_name = {bill.name: bill for bill in config.recurring_bills}

    hist = df[(df["category"] == "expense") & (df["date"].dt.date < period_start)]
    hist_desc = hist["description"].str.lower() if not hist.empty else None

    patterns = []
    for status in statuses:
        if status["status"] == "paid":
            continue
        due = date.fromisoformat(status["due_date"])
        if due + timedelta(days=MISSING_GRACE_DAYS) >= today:
            continue  # still within grace
        bill = bills_by_name.get(status["name"])
        criteria = bill.match_criteria if bill else []
        has_prior = hist_desc is not None and any(
            hist_desc.str.contains(c, regex=False, na=False).any() for c in criteria
        )
        if not has_prior:
            continue  # never occurred before — can't be "missing"
        days_late = (today - due).days
        headline = (
            f"{status['name']} (${status['amount']:,.2f}) was due {status['due_date']} "
            f"and hasn't been paid — {days_late} days late."
        )
        patterns.append(
            _pattern(
                "missing_recurring",
                float(status["amount"]),
                "missing",
                {
                    "bill_name": status["name"],
                    "amount": round(float(status["amount"]), 2),
                    "due_date": status["due_date"],
                    "days_late": days_late,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                },
                _drill_down(
                    search=status.get("search_keyword"),
                    start_date=period_start.isoformat(),
                    end_date=period_end.isoformat(),
                ),
                headline,
            )
        )
    return patterns


def detect_runway_variance(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Actual spend in the current pay period vs the implied budget
    (avg per-period income over completed periods, minus recurring bills due
    this period). Magnitude = abs(actual - implied_budget) in dollars.
    Surface only if drift > $50 AND > 10% of the implied budget.
    """
    today = today or date.today()
    if df is None or df.empty:
        return []
    period_start, period_end, _ = _period_bounds(config.pay_period, today)

    income = biweekly_income(df, config.pay_period)
    if income.empty:
        return []
    completed = income[income["period_start"] < pd.Timestamp(period_start)]
    if completed.empty:
        return []
    avg_income = float(completed["total_income"].mean())

    statuses = get_recurring_bill_status(df, config.pay_period, config.recurring_bills)
    bills_total = float(sum(status["amount"] for status in statuses))
    implied_budget = avg_income - bills_total
    if implied_budget <= 0:
        return []

    expenses = _expenses(df)
    day = expenses["date"].dt.date
    actual = float(
        expenses[(day >= period_start) & (day <= period_end)]["amount"].abs().sum()
    )

    drift = actual - implied_budget
    if abs(drift) <= RUNWAY_MIN_DRIFT_DOLLARS:
        return []
    if abs(drift) <= RUNWAY_MIN_DRIFT_PCT * implied_budget:
        return []

    direction = "up" if drift > 0 else "down"
    over_under = "over" if drift > 0 else "under"
    headline = (
        f"Spending this pay period is ${actual:,.2f} — ${abs(drift):,.2f} {over_under} "
        f"the implied budget of ${implied_budget:,.2f} (avg income minus bills)."
    )
    return [
        _pattern(
            "runway_variance",
            abs(drift),
            direction,
            {
                "actual_spend": round(actual, 2),
                "implied_budget": round(implied_budget, 2),
                "avg_period_income": round(avg_income, 2),
                "bills_due_this_period": round(bills_total, 2),
                "drift": round(drift, 2),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            },
            _drill_down(
                start_date=period_start.isoformat(), end_date=period_end.isoformat()
            ),
            headline,
        )
    ]


def detect_top_movers(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Top 3 subcategories (excluding Uncategorized) by abs dollar change,
    current pay period vs prior. Magnitude = the abs dollar change.
    Gates: 1 prior pay period of history AND day-of-period >= 4.
    """
    today = today or date.today()
    if df is None or df.empty:
        return []
    period_start, period_end, day_of_period = _period_bounds(config.pay_period, today)
    if day_of_period < MIN_DAY_OF_PERIOD:
        return []
    if not _has_prior_period_history(df, period_start):
        return []

    freq = config.pay_period.frequency_days
    prior_start = period_start - timedelta(days=freq)
    prior_end = period_start - timedelta(days=1)

    expenses = _expenses(df)
    curr = _category_totals(expenses, period_start, period_end)
    prior = _category_totals(expenses, prior_start, prior_end)

    movers = []
    for cat in set(curr) | set(prior):
        if cat == UNCATEGORIZED:
            continue
        c = curr.get(cat, 0.0)
        p = prior.get(cat, 0.0)
        delta = c - p
        if delta == 0:
            continue
        movers.append((cat, c, p, delta))
    movers.sort(key=lambda m: abs(m[3]), reverse=True)

    patterns = []
    for cat, c, p, delta in movers[:TOP_MOVERS_COUNT]:
        direction = "up" if delta > 0 else "down"
        headline = (
            f"{cat} moved ${abs(delta):,.2f} {direction} vs the prior pay period "
            f"(${c:,.2f} vs ${p:,.2f})."
        )
        patterns.append(
            _pattern(
                "top_movers",
                abs(delta),
                direction,
                {
                    "category": cat,
                    "current_total": round(c, 2),
                    "prior_total": round(p, 2),
                    "delta": round(delta, 2),
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "prior_period_start": prior_start.isoformat(),
                    "prior_period_end": prior_end.isoformat(),
                },
                _drill_down(
                    category=[cat],
                    start_date=period_start.isoformat(),
                    end_date=period_end.isoformat(),
                ),
                headline,
            )
        )
    return patterns


def detect_uncategorized_creep(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Triggers when current-period uncategorized expense transactions exceed
    a threshold (count >= 10 OR dollars >= $200) AND the count grew vs the
    prior period. Magnitude = count * sqrt(dollars). Headline names the top
    3 unmatched merchants by frequency.
    Gate: 1 prior pay period of history.
    """
    today = today or date.today()
    if df is None or df.empty:
        return []
    period_start, period_end, _ = _period_bounds(config.pay_period, today)
    if not _has_prior_period_history(df, period_start):
        return []

    freq = config.pay_period.frequency_days
    prior_start = period_start - timedelta(days=freq)
    prior_end = period_start - timedelta(days=1)

    expenses = _expenses(df)
    uncat = expenses[expenses["subcategory"] == UNCATEGORIZED]
    day = uncat["date"].dt.date

    current = uncat[(day >= period_start) & (day <= period_end)]
    prior = uncat[(day >= prior_start) & (day <= prior_end)]

    count = int(len(current))
    dollars = float(current["amount"].abs().sum())
    prior_count = int(len(prior))

    if not (count >= UNCAT_MIN_COUNT or dollars >= UNCAT_MIN_DOLLARS):
        return []
    if count <= prior_count:
        return []

    top_merchants = (
        current["description"].value_counts().head(UNCAT_TOP_MERCHANTS).index.tolist()
    )
    magnitude = count * math.sqrt(dollars)
    headline = (
        f"You have {count} uncategorized transactions worth ${dollars:,.2f} this "
        f"period. Top unmatched merchants: {', '.join(top_merchants)} — go to "
        f"/rules to label them."
    )
    return [
        _pattern(
            "uncategorized_creep",
            magnitude,
            "up",
            {
                "count": count,
                "dollars": round(dollars, 2),
                "prior_count": prior_count,
                "top_merchants": top_merchants,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            },
            _drill_down(
                category=[UNCATEGORIZED],
                start_date=period_start.isoformat(),
                end_date=period_end.isoformat(),
            ),
            headline,
        )
    ]


# --- Registry ---

DETECTORS = [
    detect_category_delta,
    detect_anomaly,
    detect_new_recurring,
    detect_missing_recurring,
    detect_runway_variance,
    detect_top_movers,
    detect_uncategorized_creep,
]


def run_all(
    df: pd.DataFrame, config: AppConfig, state: dict, today: date | None = None
) -> list[dict]:
    """
    Run every detector and return the combined pattern list (unranked —
    magnitude ranking, diversity, and freshness filtering are the briefing
    writer's job). A single failing detector is logged and skipped so one
    bug can't kill the whole briefing.
    """
    patterns: list[dict] = []
    for detector in DETECTORS:
        try:
            patterns.extend(detector(df, config, state, today=today))
        except Exception:
            logger.exception("Pattern detector %s failed", detector.__name__)
    return patterns
