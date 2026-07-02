from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta

import pandas as pd

from finance.config_loader import PayPeriodConfig, RecurringBill, TemporaryExpense

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 90
MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 365


def clamp_horizon(horizon_days) -> int:
    """Clamp a requested horizon into the supported [1, 365] day range."""
    try:
        horizon_days = int(horizon_days)
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_DAYS
    return max(MIN_HORIZON_DAYS, min(MAX_HORIZON_DAYS, horizon_days))


def derive_paycheck_amount(biweekly_income_df: pd.DataFrame | None) -> float:
    """
    Estimate the per-paycheck amount as the median total income across
    historical pay periods (the shape produced by
    finance.analytics.biweekly_income — one row per pay period, with a
    `total_income` column).

    A per-period aggregate is used rather than a per-transaction median of
    raw "income"-categorized rows: real-world income data is full of small
    non-payroll credits (refunds, interest, disputed-charge reversals) that
    also get classified as income, and those badly skew a flat per-row
    median toward near-zero. Summing to the period first, then taking the
    median across periods, is far closer to the actual paycheck amount.

    Falls back to 0 if there's no income history to derive from.
    """
    if (
        biweekly_income_df is None
        or biweekly_income_df.empty
        or "total_income" not in biweekly_income_df.columns
    ):
        return 0.0
    return round(float(biweekly_income_df["total_income"].median()), 2)


def _clamp_day_of_month(year: int, month: int, day_of_month: int) -> date:
    """Clamp a target day-of-month (e.g. 31) to the real last day of that month (e.g. Feb 28)."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day_of_month, last_day))


def _bill_occurrences(bill: RecurringBill, start: date, end: date) -> list[date]:
    """All dates within [start, end] this bill lands on — one per month, clamped to month end."""
    occurrences = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = _clamp_day_of_month(y, m, bill.day_of_month)
        if start <= d <= end:
            occurrences.append(d)
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return occurrences


def _paycheck_occurrences(pay_period: PayPeriodConfig, start: date, end: date) -> list[date]:
    """All paycheck dates within [start, end], anchored at pay_period.start_date."""
    freq = pay_period.frequency_days
    anchor = pay_period.start_date
    if freq <= 0:
        return []

    days_since_anchor = (start - anchor).days
    period_index = days_since_anchor // freq
    candidate = anchor + timedelta(days=period_index * freq)
    if candidate < start:
        candidate += timedelta(days=freq)

    occurrences = []
    while candidate <= end:
        occurrences.append(candidate)
        candidate += timedelta(days=freq)
    return occurrences


def _temporary_expense_occurrence(expense: TemporaryExpense, as_of: date) -> date:
    """
    One-off date a temporary expense lands on. TemporaryExpense only carries a
    `half` (1 = 1st-15th, 2 = 16th-end), the same one-time-per-config-entry
    semantics analytics.compute_monthly_half_runway uses for the *current*
    calendar month (as of `as_of`) — day 1 for half 1, day 16 for half 2.
    """
    day = 1 if expense.half == 1 else 16
    return date(as_of.year, as_of.month, day)


def project_cash_flow(
    current_balance: float,
    pay_period: PayPeriodConfig,
    paycheck_amount: float,
    recurring_bills: list[RecurringBill],
    temporary_expenses: list[TemporaryExpense],
    start_date: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
    """
    Project daily cash flow forward from `start_date` for `horizon_days`
    (clamped to [MIN_HORIZON_DAYS, MAX_HORIZON_DAYS]).

    Bills land on their day_of_month (clamped to month end). Paychecks land
    every pay_period.frequency_days from the pay_period.start_date anchor.
    Temporary expenses land once, on the day their `half` maps to within the
    calendar month containing `start_date` (see _temporary_expense_occurrence).

    Returns:
        {
            "starting_balance": float,
            "as_of": "YYYY-MM-DD",
            "horizon_days": int,
            "days": [
                {
                    "date": "YYYY-MM-DD",
                    "events": [{"type": "bill"|"income"|"temporary", "name": str, "amount": float}],
                    "net_change": float,
                    "projected_balance": float,
                },
                ...
            ],
            "monthly": [
                {"month": "YYYY-MM", "projected_in": float, "projected_out": float, "projected_net": float},
                ...
            ],
            "warnings": {
                "goes_negative": bool,
                "first_negative_date": str | None,
                "min_balance": float,
                "min_balance_date": str,
            },
        }
    """
    horizon_days = clamp_horizon(horizon_days)
    end_date = start_date + timedelta(days=horizon_days - 1)

    events_by_date: dict[date, list[dict]] = {}

    for bill in recurring_bills:
        for d in _bill_occurrences(bill, start_date, end_date):
            events_by_date.setdefault(d, []).append(
                {"type": "bill", "name": bill.name, "amount": -round(abs(bill.amount), 2)}
            )

    if paycheck_amount:
        for d in _paycheck_occurrences(pay_period, start_date, end_date):
            events_by_date.setdefault(d, []).append(
                {"type": "income", "name": "Paycheck", "amount": round(abs(paycheck_amount), 2)}
            )

    for expense in temporary_expenses:
        d = _temporary_expense_occurrence(expense, start_date)
        if start_date <= d <= end_date:
            events_by_date.setdefault(d, []).append(
                {"type": "temporary", "name": expense.name, "amount": -round(abs(expense.amount), 2)}
            )

    running_balance = current_balance
    days_out = []
    monthly_totals: dict[str, dict[str, float]] = {}

    min_balance = current_balance
    min_balance_date = start_date
    first_negative_date: date | None = None

    d = start_date
    while d <= end_date:
        events = events_by_date.get(d, [])

        net_change = round(sum(e["amount"] for e in events), 2)
        running_balance = round(running_balance + net_change, 2)

        days_out.append(
            {
                "date": d.isoformat(),
                "events": events,
                "net_change": net_change,
                "projected_balance": running_balance,
            }
        )

        if running_balance < min_balance:
            min_balance = running_balance
            min_balance_date = d
        if running_balance < 0 and first_negative_date is None:
            first_negative_date = d

        month_key = f"{d.year:04d}-{d.month:02d}"
        bucket = monthly_totals.setdefault(month_key, {"in": 0.0, "out": 0.0})
        for e in events:
            if e["amount"] >= 0:
                bucket["in"] += e["amount"]
            else:
                bucket["out"] += e["amount"]

        d += timedelta(days=1)

    monthly = [
        {
            "month": month,
            "projected_in": round(totals["in"], 2),
            "projected_out": round(totals["out"], 2),
            "projected_net": round(totals["in"] + totals["out"], 2),
        }
        for month, totals in sorted(monthly_totals.items())
    ]

    warnings = {
        "goes_negative": first_negative_date is not None,
        "first_negative_date": first_negative_date.isoformat() if first_negative_date else None,
        "min_balance": round(min_balance, 2),
        "min_balance_date": min_balance_date.isoformat(),
    }

    return {
        "starting_balance": round(current_balance, 2),
        "as_of": start_date.isoformat(),
        "horizon_days": horizon_days,
        "days": days_out,
        "monthly": monthly,
        "warnings": warnings,
    }
