from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from finance import pattern_detector
from finance.config_loader import (
    AppConfig,
    ClassificationConfig,
    PayPeriodConfig,
    RecurringBill,
)
from finance.pattern_detector import (
    DETECTORS,
    detect_anomaly,
    detect_category_delta,
    detect_missing_recurring,
    detect_new_recurring,
    detect_runway_variance,
    detect_top_movers,
    detect_uncategorized_creep,
    run_all,
)

# Fixed clock for the pure-period detectors. Pay period 2026-06-08..2026-06-21
# (prior period 2026-05-25..2026-06-07); TODAY is day 8 of the period.
TODAY = date(2026, 6, 15)
PP_START = date(2026, 6, 8)

DF_COLUMNS = [
    "date", "description", "amount", "account_name", "account_type",
    "category", "subcategory",
]

PATTERN_KEYS = {
    "pattern_type", "magnitude", "direction", "raw_facts",
    "drill_down_filter", "headline",
}
DRILL_DOWN_KEYS = {"category", "account", "start_date", "end_date", "search"}


def txn(
    d: date,
    description: str,
    amount: float,
    subcategory: str | None = None,
    category: str = "expense",
    account: str = "Test Checking",
):
    return {
        "date": d,
        "description": description,
        "amount": amount,
        "account_name": account,
        "account_type": "checking",
        "category": category,
        "subcategory": subcategory,
    }


def make_df(rows: list[dict]) -> pd.DataFrame:
    """Synthetic DataFrame with the exact post-classification cache columns."""
    if not rows:
        df = pd.DataFrame(columns=DF_COLUMNS)
        df["date"] = pd.to_datetime(df["date"])
        return df
    df = pd.DataFrame(rows, columns=DF_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def make_cfg(
    start: date = PP_START,
    frequency_days: int = 14,
    bills: list[RecurringBill] | None = None,
) -> AppConfig:
    return AppConfig(
        pay_period=PayPeriodConfig(start_date=start, frequency_days=frequency_days),
        accounts=[],
        classification=ClassificationConfig(),
        recurring_bills=bills or [],
    )


def daily_spend(start: date, n_days: int, amount: float,
                description: str = "Grocery Store",
                subcategory: str = "Groceries") -> list[dict]:
    return [
        txn(start + timedelta(days=i), description, -abs(amount), subcategory)
        for i in range(n_days)
    ]


ANCHOR = txn(date(2026, 4, 1), "History Anchor", -5.00, "Misc")  # >60d before TODAY


# =====================================================================
# category_delta
# =====================================================================


def test_category_delta_up_formula_and_shape():
    df = make_df([
        txn(date(2026, 6, 1), "Doordash", -100.00, "Dining Out"),   # prior period
        txn(date(2026, 6, 12), "Doordash", -150.00, "Dining Out"),  # current period
    ])
    patterns = detect_category_delta(df, make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    p = patterns[0]
    assert set(p) == PATTERN_KEYS
    assert p["pattern_type"] == "category_delta"
    assert p["direction"] == "up"
    # pct = 50/max(100,25) = 0.5; magnitude = 0.5 * sqrt(50)
    assert p["magnitude"] == pytest.approx(0.5 * math.sqrt(50), abs=1e-3)
    assert p["raw_facts"]["current_total"] == 150.00
    assert p["raw_facts"]["prior_total"] == 100.00
    assert p["raw_facts"]["delta"] == 50.00
    assert p["raw_facts"]["pct_change"] == pytest.approx(0.5)
    assert set(p["drill_down_filter"]) == DRILL_DOWN_KEYS
    assert p["drill_down_filter"]["category"] == ["Dining Out"]
    assert p["drill_down_filter"]["start_date"] == "2026-06-08"
    assert p["drill_down_filter"]["end_date"] == "2026-06-21"
    assert "Dining Out" in p["headline"]


def test_category_delta_down_direction():
    df = make_df([
        txn(date(2026, 6, 1), "Hy-Vee", -150.00, "Groceries"),
        txn(date(2026, 6, 12), "Hy-Vee", -100.00, "Groceries"),
    ])
    patterns = detect_category_delta(df, make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    assert patterns[0]["direction"] == "down"
    assert patterns[0]["magnitude"] == pytest.approx((50 / 150) * math.sqrt(50), abs=1e-3)


def test_category_delta_dollar_floor_prevents_infinite_pct():
    # prior=$5: pct uses the $25 floor -> 35/25 = 1.4
    df = make_df([
        txn(date(2026, 6, 1), "Etsy", -5.00, "Hobbies"),
        txn(date(2026, 6, 12), "Etsy", -40.00, "Hobbies"),
    ])
    patterns = detect_category_delta(df, make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    assert patterns[0]["raw_facts"]["pct_change"] == pytest.approx(1.4)
    assert patterns[0]["magnitude"] == pytest.approx(1.4 * math.sqrt(35), abs=1e-3)


def test_category_delta_below_pct_threshold_not_surfaced():
    # delta $60 >= $25 but pct 60/500 = 12% < 20%
    df = make_df([
        txn(date(2026, 6, 1), "Rent Co", -500.00, "Rent"),
        txn(date(2026, 6, 12), "Rent Co", -560.00, "Rent"),
    ])
    assert detect_category_delta(df, make_cfg(), {}, today=TODAY) == []


def test_category_delta_below_dollar_threshold_not_surfaced():
    # pct 24/max(30,25)=80% but delta $24 < $25
    df = make_df([
        txn(date(2026, 6, 1), "Cafe", -30.00, "Coffee"),
        txn(date(2026, 6, 12), "Cafe", -54.00, "Coffee"),
    ])
    assert detect_category_delta(df, make_cfg(), {}, today=TODAY) == []


@pytest.mark.parametrize(
    "today,expected_count",
    [
        (date(2026, 6, 8), 0),   # day 1
        (date(2026, 6, 10), 0),  # day 3 — still gated
        (date(2026, 6, 11), 1),  # day 4 — boundary: runs
    ],
)
def test_category_delta_day_of_period_gate(today, expected_count):
    df = make_df([
        txn(date(2026, 6, 1), "Doordash", -100.00, "Dining Out"),
        txn(date(2026, 6, 9), "Doordash", -150.00, "Dining Out"),
    ])
    patterns = detect_category_delta(df, make_cfg(), {}, today=today)
    assert len(patterns) == expected_count


def test_category_delta_cold_start_no_prior_period():
    # All history inside the current period -> min-history gate returns []
    df = make_df([
        txn(date(2026, 6, 9), "Doordash", -100.00, "Dining Out"),
        txn(date(2026, 6, 12), "Doordash", -150.00, "Dining Out"),
    ])
    assert detect_category_delta(df, make_cfg(), {}, today=TODAY) == []


def test_category_delta_empty_df_returns_empty():
    assert detect_category_delta(make_df([]), make_cfg(), {}, today=TODAY) == []


def test_category_delta_excludes_uncategorized():
    df = make_df([
        txn(date(2026, 6, 1), "Mystery", -100.00, None),
        txn(date(2026, 6, 12), "Mystery", -300.00, ""),
    ])
    assert detect_category_delta(df, make_cfg(), {}, today=TODAY) == []


def test_category_delta_excludes_transfers():
    df = make_df([
        txn(date(2026, 6, 1), "Doordash", -100.00, "Dining Out"),
        txn(date(2026, 6, 12), "Transfer to PayPal", -300.00, "Dining Out", category="transfer"),
    ])
    patterns = detect_category_delta(df, make_cfg(), {}, today=TODAY)
    # Transfer ignored: current total is $0, so Dining Out dropped 100%
    assert len(patterns) == 1
    assert patterns[0]["direction"] == "down"
    assert patterns[0]["raw_facts"]["current_total"] == 0.0


# =====================================================================
# anomaly
# =====================================================================


def test_anomaly_spike_flagged_z_capped_at_5():
    rows = [ANCHOR] + daily_spend(date(2026, 5, 1), 35, 10.00)  # 35 obs @ $10
    rows.append(txn(date(2026, 6, 10), "Grocery Store", -100.00, "Groceries"))
    patterns = detect_anomaly(make_df(rows), make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    p = patterns[0]
    assert p["pattern_type"] == "anomaly"
    assert p["direction"] == "up"
    # mean 12.5, sample stdev 15, raw z 5.833 -> capped at 5
    assert p["magnitude"] == pytest.approx(5.0)
    assert p["raw_facts"]["z_score"] == pytest.approx(5.8333, abs=1e-3)
    assert p["raw_facts"]["date"] == "2026-06-10"
    assert p["raw_facts"]["daily_total"] == 100.00
    assert p["drill_down_filter"]["category"] == ["Groceries"]
    assert p["drill_down_filter"]["start_date"] == "2026-06-10"
    assert p["drill_down_filter"]["end_date"] == "2026-06-10"


def test_anomaly_z_between_2_and_5_not_capped():
    rows = [ANCHOR]
    rows += daily_spend(date(2026, 5, 1), 17, 30.00)   # 17 obs @ $30
    rows += daily_spend(date(2026, 5, 18), 17, 70.00)  # 17 obs @ $70
    rows.append(txn(date(2026, 6, 10), "Grocery Store", -120.00, "Groceries"))
    patterns = detect_anomaly(make_df(rows), make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    # mean 52, sample stdev sqrt(540) -> z = 68/23.2379 = 2.9263
    assert patterns[0]["magnitude"] == pytest.approx(2.9263, abs=1e-3)


def test_anomaly_min_observations_gate():
    # Only 21 observation days (< 30): even a huge spike is not flagged
    rows = [ANCHOR] + daily_spend(date(2026, 5, 10), 20, 10.00)
    rows.append(txn(date(2026, 6, 10), "Grocery Store", -200.00, "Groceries"))
    assert detect_anomaly(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_anomaly_stdev_noise_floor_gate():
    # 34 obs @ $10 + one $40 day: sample stdev ~= 5.07 < $10 floor
    rows = [ANCHOR] + daily_spend(date(2026, 5, 1), 34, 10.00)
    rows.append(txn(date(2026, 6, 10), "Grocery Store", -40.00, "Groceries"))
    assert detect_anomaly(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_anomaly_min_history_gate_under_60_days():
    rows = daily_spend(date(2026, 5, 1), 35, 10.00)  # earliest txn 45d before TODAY
    rows.append(txn(date(2026, 6, 10), "Grocery Store", -400.00, "Groceries"))
    assert detect_anomaly(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_anomaly_same_day_same_merchant_refund_nets_to_zero():
    base = [ANCHOR] + daily_spend(date(2026, 5, 1), 35, 10.00)
    purchase = txn(date(2026, 6, 10), "Costco", -400.00, "Groceries")
    refund = txn(date(2026, 6, 10), "Costco", 400.00, "Groceries", category="income")

    # Without the refund the purchase is a capped-z anomaly...
    flagged = detect_anomaly(make_df(base + [purchase]), make_cfg(), {}, today=TODAY)
    assert len(flagged) == 1
    assert flagged[0]["magnitude"] == pytest.approx(5.0)

    # ...with the same-day same-merchant refund it nets to $0 and disappears.
    netted = detect_anomaly(make_df(base + [purchase, refund]), make_cfg(), {}, today=TODAY)
    assert netted == []


def test_anomaly_transfers_excluded():
    rows = [ANCHOR] + daily_spend(date(2026, 5, 1), 35, 10.00)
    rows.append(txn(date(2026, 6, 10), "Transfer to Savings", -400.00, "Groceries",
                    category="transfer"))
    assert detect_anomaly(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_anomaly_empty_df_returns_empty():
    assert detect_anomaly(make_df([]), make_cfg(), {}, today=TODAY) == []


# =====================================================================
# new_recurring
# =====================================================================


def test_new_recurring_basic_detection_and_run_rate():
    df = make_df([
        ANCHOR,
        txn(date(2026, 5, 10), "Netflix", -15.49, "Streaming"),
        txn(date(2026, 6, 9), "Netflix", -15.49, "Streaming"),
    ])
    patterns = detect_new_recurring(df, make_cfg(), {"seen_recurring_merchants": {}}, today=TODAY)
    assert len(patterns) == 1
    p = patterns[0]
    assert p["pattern_type"] == "new_recurring"
    assert p["direction"] == "new"
    # avg $15.49, avg gap 30d -> run rate = 15.49 * 30/30 = 15.49
    assert p["magnitude"] == pytest.approx(15.49)
    assert p["raw_facts"]["merchant_key"] == "netflix"
    assert p["raw_facts"]["charge_count"] == 2
    assert p["raw_facts"]["avg_days_between_charges"] == pytest.approx(30.0)
    assert p["raw_facts"]["monthly_run_rate"] == pytest.approx(15.49)
    assert p["drill_down_filter"]["search"] == "Netflix"
    assert "Netflix" in p["headline"]


def test_new_recurring_run_rate_formula_three_charges():
    # 3 x $10, gaps 20+20 -> avg gap 20 -> run rate = 10 * 30/20 = 15.0
    df = make_df([
        ANCHOR,
        txn(date(2026, 5, 6), "Gym Plus", -10.00, None),
        txn(date(2026, 5, 26), "Gym Plus", -10.00, None),
        txn(date(2026, 6, 15), "Gym Plus", -10.00, None),
    ])
    patterns = detect_new_recurring(df, make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    assert patterns[0]["magnitude"] == pytest.approx(15.0)


@pytest.mark.parametrize("seen_key", ["netflix", "Netflix", "  NETFLIX  "])
def test_new_recurring_seen_merchant_excluded(seen_key):
    df = make_df([
        ANCHOR,
        txn(date(2026, 5, 10), "Netflix", -15.49, "Streaming"),
        txn(date(2026, 6, 9), "Netflix", -15.49, "Streaming"),
    ])
    state = {"seen_recurring_merchants": {seen_key: {"first_seen": "2026-05-10"}}}
    assert detect_new_recurring(df, make_cfg(), state, today=TODAY) == []


def test_new_recurring_existing_recurring_bill_excluded():
    df = make_df([
        ANCHOR,
        txn(date(2026, 5, 10), "Netflix", -15.49, "Streaming"),
        txn(date(2026, 6, 9), "Netflix", -15.49, "Streaming"),
    ])
    cfg = make_cfg(bills=[
        RecurringBill(name="Netflix", amount=15.49, day_of_month=9, match_criteria=["netflix"])
    ])
    assert detect_new_recurring(df, cfg, {}, today=TODAY) == []


def test_new_recurring_min_history_gate_under_60_days():
    df = make_df([
        txn(date(2026, 5, 10), "Netflix", -15.49, "Streaming"),
        txn(date(2026, 6, 9), "Netflix", -15.49, "Streaming"),
    ])
    assert detect_new_recurring(df, make_cfg(), {}, today=TODAY) == []


def test_new_recurring_single_charge_not_flagged():
    df = make_df([ANCHOR, txn(date(2026, 6, 9), "Netflix", -15.49, "Streaming")])
    assert detect_new_recurring(df, make_cfg(), {}, today=TODAY) == []


def test_new_recurring_same_day_duplicates_skipped():
    # Two charges the same day is not a cadence (and would divide by zero)
    df = make_df([
        ANCHOR,
        txn(date(2026, 6, 9), "Food Truck", -12.00, None),
        txn(date(2026, 6, 9), "Food Truck", -9.00, None),
    ])
    assert detect_new_recurring(df, make_cfg(), {}, today=TODAY) == []


def test_new_recurring_charges_outside_60_day_window_ignored():
    df = make_df([
        ANCHOR,
        txn(date(2026, 4, 10), "Netflix", -15.49, "Streaming"),  # outside window
        txn(date(2026, 6, 9), "Netflix", -15.49, "Streaming"),
    ])
    assert detect_new_recurring(df, make_cfg(), {}, today=TODAY) == []


def test_new_recurring_transfers_excluded():
    df = make_df([
        ANCHOR,
        txn(date(2026, 5, 10), "Transfer to PayPal", -50.00, None, category="transfer"),
        txn(date(2026, 6, 9), "Transfer to PayPal", -50.00, None, category="transfer"),
    ])
    assert detect_new_recurring(df, make_cfg(), {}, today=TODAY) == []


def test_new_recurring_empty_df_returns_empty():
    assert detect_new_recurring(make_df([]), make_cfg(), {}, today=TODAY) == []


# =====================================================================
# missing_recurring
# (get_recurring_bill_status reads the real clock, so these build data
#  relative to date.today() and pass it explicitly.)
# =====================================================================

REAL_TODAY = date.today()
REAL_PP = date.today() - timedelta(days=7)  # current period: today-7 .. today+6


def _bill(due_offset_days: int, name: str = "Internet", amount: float = 80.0,
          criteria: str = "internet") -> RecurringBill:
    """A bill due `due_offset_days` before REAL_TODAY (within the current period)."""
    return RecurringBill(
        name=name,
        amount=amount,
        day_of_month=(REAL_TODAY - timedelta(days=due_offset_days)).day,
        match_criteria=[criteria],
    )


def _missing_df(extra: list[dict] | None = None) -> pd.DataFrame:
    rows = [
        # prior occurrence of the bill, well before the current period
        txn(REAL_TODAY - timedelta(days=35), "Internet Provider", -80.00, "Utilities"),
    ]
    return make_df(rows + (extra or []))


def test_missing_recurring_flags_unpaid_bill_past_grace():
    cfg = make_cfg(start=REAL_PP, bills=[_bill(due_offset_days=5)])
    patterns = detect_missing_recurring(_missing_df(), cfg, {}, today=REAL_TODAY)
    assert len(patterns) == 1
    p = patterns[0]
    assert p["pattern_type"] == "missing_recurring"
    assert p["direction"] == "missing"
    assert p["magnitude"] == pytest.approx(80.0)  # magnitude = bill amount
    assert p["raw_facts"]["bill_name"] == "Internet"
    assert p["raw_facts"]["days_late"] == 5
    assert p["drill_down_filter"]["search"] == "internet"


def test_missing_recurring_paid_bill_not_flagged():
    paid = [txn(REAL_TODAY - timedelta(days=4), "Internet Provider", -80.00, "Utilities")]
    cfg = make_cfg(start=REAL_PP, bills=[_bill(due_offset_days=5)])
    assert detect_missing_recurring(_missing_df(paid), cfg, {}, today=REAL_TODAY) == []


@pytest.mark.parametrize(
    "due_offset,expected_count",
    [
        (2, 0),  # due 2 days ago: within grace
        (3, 0),  # due + 3 == today: boundary, still within grace
        (4, 1),  # due + 3 < today: missed
    ],
)
def test_missing_recurring_three_day_grace_boundary(due_offset, expected_count):
    cfg = make_cfg(start=REAL_PP, bills=[_bill(due_offset_days=due_offset)])
    patterns = detect_missing_recurring(_missing_df(), cfg, {}, today=REAL_TODAY)
    assert len(patterns) == expected_count


def test_missing_recurring_requires_prior_occurrence():
    # No transaction has ever matched this bill -> can't be "missing"
    df = make_df([txn(REAL_TODAY - timedelta(days=35), "Some Other Shop", -12.00, None)])
    cfg = make_cfg(start=REAL_PP, bills=[_bill(due_offset_days=5)])
    assert detect_missing_recurring(df, cfg, {}, today=REAL_TODAY) == []


def test_missing_recurring_no_bills_configured():
    assert detect_missing_recurring(_missing_df(), make_cfg(start=REAL_PP), {},
                                    today=REAL_TODAY) == []


def test_missing_recurring_empty_df_returns_empty():
    cfg = make_cfg(start=REAL_PP, bills=[_bill(due_offset_days=5)])
    assert detect_missing_recurring(make_df([]), cfg, {}, today=REAL_TODAY) == []


# =====================================================================
# runway_variance
# (also leans on get_recurring_bill_status -> real clock)
# =====================================================================


def _runway_df(current_spend: float) -> pd.DataFrame:
    rows = [
        # income in two completed prior periods -> avg $2000/period
        txn(REAL_TODAY - timedelta(days=28), "ACME Payroll", 2000.00, None, category="income"),
        txn(REAL_TODAY - timedelta(days=14), "ACME Payroll", 2000.00, None, category="income"),
    ]
    if current_spend:
        rows.append(txn(REAL_TODAY - timedelta(days=2), "Big Spend", -abs(current_spend), "Shopping"))
    return make_df(rows)


def _runway_cfg(bill_amount: float = 500.0) -> AppConfig:
    # One bill due this period -> implied budget = 2000 - 500 = 1500
    return make_cfg(start=REAL_PP, bills=[
        _bill(due_offset_days=1, name="Rent", amount=bill_amount, criteria="rent")
    ])


def test_runway_variance_over_budget():
    patterns = detect_runway_variance(_runway_df(2000.00), _runway_cfg(), {}, today=REAL_TODAY)
    assert len(patterns) == 1
    p = patterns[0]
    assert p["pattern_type"] == "runway_variance"
    assert p["direction"] == "up"
    assert p["magnitude"] == pytest.approx(500.0)  # |2000 - 1500|
    assert p["raw_facts"]["implied_budget"] == pytest.approx(1500.0)
    assert p["raw_facts"]["actual_spend"] == pytest.approx(2000.0)
    assert p["raw_facts"]["drift"] == pytest.approx(500.0)


def test_runway_variance_under_budget():
    patterns = detect_runway_variance(_runway_df(500.00), _runway_cfg(), {}, today=REAL_TODAY)
    assert len(patterns) == 1
    assert patterns[0]["direction"] == "down"
    assert patterns[0]["magnitude"] == pytest.approx(1000.0)


@pytest.mark.parametrize(
    "spend",
    [
        1540.00,  # drift $40 <= $50 floor
        1550.00,  # drift exactly $50: strict > required
        1600.00,  # drift $100 > $50 but <= 10% of $1500
        1650.00,  # drift exactly 10% of budget: strict > required
    ],
)
def test_runway_variance_below_thresholds_not_surfaced(spend):
    assert detect_runway_variance(_runway_df(spend), _runway_cfg(), {}, today=REAL_TODAY) == []


def test_runway_variance_no_completed_income_periods():
    # Income only inside the current period -> no baseline -> no pattern
    df = make_df([
        txn(REAL_TODAY - timedelta(days=1), "ACME Payroll", 2000.00, None, category="income"),
        txn(REAL_TODAY - timedelta(days=2), "Big Spend", -2000.00, "Shopping"),
    ])
    assert detect_runway_variance(df, _runway_cfg(), {}, today=REAL_TODAY) == []


def test_runway_variance_non_positive_budget_skipped():
    # avg income $400 - $500 bill -> implied budget <= 0 -> skip
    df = make_df([
        txn(REAL_TODAY - timedelta(days=14), "ACME Payroll", 400.00, None, category="income"),
        txn(REAL_TODAY - timedelta(days=2), "Big Spend", -2000.00, "Shopping"),
    ])
    assert detect_runway_variance(df, _runway_cfg(bill_amount=500.0), {}, today=REAL_TODAY) == []


def test_runway_variance_empty_df_returns_empty():
    assert detect_runway_variance(make_df([]), _runway_cfg(), {}, today=REAL_TODAY) == []


# =====================================================================
# top_movers
# =====================================================================


def _movers_df() -> pd.DataFrame:
    prior, curr = date(2026, 6, 1), date(2026, 6, 12)
    return make_df([
        txn(prior, "A Shop", -50.00, "Cat A"), txn(curr, "A Shop", -150.00, "Cat A"),   # +100
        txn(prior, "B Shop", -100.00, "Cat B"), txn(curr, "B Shop", -40.00, "Cat B"),   # -60
        txn(prior, "C Shop", -10.00, "Cat C"), txn(curr, "C Shop", -50.00, "Cat C"),    # +40
        txn(prior, "D Shop", -20.00, "Cat D"), txn(curr, "D Shop", -30.00, "Cat D"),    # +10
    ])


def test_top_movers_top_three_by_abs_dollar_change():
    patterns = detect_top_movers(_movers_df(), make_cfg(), {}, today=TODAY)
    assert len(patterns) == 3
    assert [p["raw_facts"]["category"] for p in patterns] == ["Cat A", "Cat B", "Cat C"]
    assert [p["magnitude"] for p in patterns] == [pytest.approx(100.0), pytest.approx(60.0), pytest.approx(40.0)]
    assert [p["direction"] for p in patterns] == ["up", "down", "up"]
    assert all(p["pattern_type"] == "top_movers" for p in patterns)
    assert patterns[0]["drill_down_filter"]["category"] == ["Cat A"]


def test_top_movers_excludes_uncategorized_even_when_largest():
    rows = [
        txn(date(2026, 6, 1), "Mystery", -10.00, None),
        txn(date(2026, 6, 12), "Mystery", -900.00, None),   # +890 but Uncategorized
        txn(date(2026, 6, 1), "A Shop", -50.00, "Cat A"),
        txn(date(2026, 6, 12), "A Shop", -150.00, "Cat A"),
    ]
    patterns = detect_top_movers(make_df(rows), make_cfg(), {}, today=TODAY)
    assert [p["raw_facts"]["category"] for p in patterns] == ["Cat A"]


def test_top_movers_zero_delta_categories_excluded():
    rows = [
        txn(date(2026, 6, 1), "Same Shop", -75.00, "Steady"),
        txn(date(2026, 6, 12), "Same Shop", -75.00, "Steady"),  # delta 0
        txn(date(2026, 6, 1), "A Shop", -50.00, "Cat A"),
        txn(date(2026, 6, 12), "A Shop", -60.00, "Cat A"),      # +10
    ]
    patterns = detect_top_movers(make_df(rows), make_cfg(), {}, today=TODAY)
    assert [p["raw_facts"]["category"] for p in patterns] == ["Cat A"]


def test_top_movers_day_of_period_gate():
    assert detect_top_movers(_movers_df(), make_cfg(), {}, today=date(2026, 6, 10)) == []


def test_top_movers_cold_start_no_prior_period():
    df = make_df([txn(date(2026, 6, 12), "A Shop", -150.00, "Cat A")])
    assert detect_top_movers(df, make_cfg(), {}, today=TODAY) == []


def test_top_movers_empty_df_returns_empty():
    assert detect_top_movers(make_df([]), make_cfg(), {}, today=TODAY) == []


# =====================================================================
# uncategorized_creep
# =====================================================================


def _uncat_rows(n: int, day: date, amount: float = 10.00,
                descriptions: list[str] | None = None) -> list[dict]:
    descriptions = descriptions or ["Mystery Shop"] * n
    # Alternate NaN and "" to pin both normalization paths
    return [
        txn(day + timedelta(days=i % 5), descriptions[i], -abs(amount),
            None if i % 2 == 0 else "")
        for i in range(n)
    ]


def test_uncategorized_creep_count_trigger_with_top_merchants():
    descs = ["Mystery Shop"] * 5 + ["Vendor A"] * 4 + ["Vendor B"] * 2 + ["Vendor C"]
    rows = _uncat_rows(12, date(2026, 6, 9), descriptions=descs)
    rows += _uncat_rows(2, date(2026, 5, 30))  # prior period: 2 uncategorized
    patterns = detect_uncategorized_creep(make_df(rows), make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    p = patterns[0]
    assert p["pattern_type"] == "uncategorized_creep"
    assert p["direction"] == "up"
    assert p["raw_facts"]["count"] == 12
    assert p["raw_facts"]["dollars"] == pytest.approx(120.0)
    assert p["raw_facts"]["prior_count"] == 2
    assert p["raw_facts"]["top_merchants"] == ["Mystery Shop", "Vendor A", "Vendor B"]
    assert p["magnitude"] == pytest.approx(12 * math.sqrt(120.0), abs=1e-3)
    assert "Mystery Shop" in p["headline"] and "/rules" in p["headline"]
    assert p["drill_down_filter"]["category"] == ["Uncategorized"]
    assert p["drill_down_filter"]["start_date"] == "2026-06-08"


def test_uncategorized_creep_dollar_trigger():
    # Only 5 txns (< 10) but $250 (>= $200), and grew vs prior
    rows = _uncat_rows(5, date(2026, 6, 9), amount=50.00)
    rows += _uncat_rows(1, date(2026, 5, 30))
    patterns = detect_uncategorized_creep(make_df(rows), make_cfg(), {}, today=TODAY)
    assert len(patterns) == 1
    assert patterns[0]["raw_facts"]["dollars"] == pytest.approx(250.0)


def test_uncategorized_creep_below_both_thresholds():
    rows = _uncat_rows(9, date(2026, 6, 9))  # 9 txns, $90
    rows += _uncat_rows(1, date(2026, 5, 30))
    assert detect_uncategorized_creep(make_df(rows), make_cfg(), {}, today=TODAY) == []


@pytest.mark.parametrize("prior_n", [12, 13])
def test_uncategorized_creep_requires_growth(prior_n):
    # current 12 (over threshold) but prior >= current -> not "creep"
    rows = _uncat_rows(12, date(2026, 6, 9))
    rows += _uncat_rows(prior_n, date(2026, 5, 26))
    assert detect_uncategorized_creep(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_uncategorized_creep_cold_start_no_prior_period():
    rows = _uncat_rows(12, date(2026, 6, 9))
    assert detect_uncategorized_creep(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_uncategorized_creep_ignores_transfers_and_income():
    rows = _uncat_rows(9, date(2026, 6, 9))  # 9 uncategorized expenses
    rows += [
        txn(date(2026, 6, 10), "Transfer to PayPal", -100.00, None, category="transfer"),
        txn(date(2026, 6, 10), "Interest Paid", 5.00, None, category="income"),
    ]
    rows += _uncat_rows(1, date(2026, 5, 30))
    # Still 9 expense uncategorized -> below thresholds -> no pattern
    assert detect_uncategorized_creep(make_df(rows), make_cfg(), {}, today=TODAY) == []


def test_uncategorized_creep_empty_df_returns_empty():
    assert detect_uncategorized_creep(make_df([]), make_cfg(), {}, today=TODAY) == []


# =====================================================================
# run_all
# =====================================================================


def test_run_all_combines_detectors_with_standard_shape():
    rows = [
        txn(date(2026, 6, 1), "Doordash", -100.00, "Dining Out"),
        txn(date(2026, 6, 12), "Doordash", -150.00, "Dining Out"),
    ]
    rows += _uncat_rows(12, date(2026, 6, 9))
    patterns = run_all(make_df(rows), make_cfg(), {}, today=TODAY)
    types = {p["pattern_type"] for p in patterns}
    assert "category_delta" in types
    assert "top_movers" in types
    assert "uncategorized_creep" in types
    for p in patterns:
        assert set(p) == PATTERN_KEYS
        assert set(p["drill_down_filter"]) == DRILL_DOWN_KEYS
        assert isinstance(p["magnitude"], float)
        assert p["direction"] in ("up", "down", "new", "missing")
        assert p["headline"]


def test_run_all_empty_df_returns_empty():
    assert run_all(make_df([]), make_cfg(), {}, today=TODAY) == []


def test_run_all_registry_contains_all_seven_detectors():
    assert DETECTORS == [
        detect_category_delta,
        detect_anomaly,
        detect_new_recurring,
        detect_missing_recurring,
        detect_runway_variance,
        detect_top_movers,
        detect_uncategorized_creep,
    ]


def test_run_all_survives_a_failing_detector(monkeypatch, caplog):
    def boom(df, config, state, today=None):
        raise RuntimeError("detector bug")

    def ok(df, config, state, today=None):
        return [pattern_detector._pattern("fake", 1.0, "up", {}, pattern_detector._drill_down(), "ok")]

    monkeypatch.setattr(pattern_detector, "DETECTORS", [boom, ok])
    with caplog.at_level("ERROR"):
        patterns = run_all(make_df([txn(TODAY, "x", -1.0, None)]), make_cfg(), {}, today=TODAY)
    assert [p["pattern_type"] for p in patterns] == ["fake"]
    assert "boom" in caplog.text
