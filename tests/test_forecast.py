from __future__ import annotations

from datetime import date

import pandas as pd

from finance.config_loader import PayPeriodConfig, RecurringBill, TemporaryExpense
from finance.forecast import (
    DEFAULT_HORIZON_DAYS,
    MAX_HORIZON_DAYS,
    MIN_HORIZON_DAYS,
    _bill_occurrences,
    _clamp_day_of_month,
    _paycheck_occurrences,
    _temporary_expense_occurrence,
    clamp_horizon,
    derive_paycheck_amount,
    project_cash_flow,
)


# --- Bill day-of-month clamping ---


def test_clamp_day_of_month_31st_in_30_day_month():
    # April has 30 days
    assert _clamp_day_of_month(2026, 4, 31) == date(2026, 4, 30)


def test_clamp_day_of_month_31st_in_february_non_leap():
    assert _clamp_day_of_month(2026, 2, 31) == date(2026, 2, 28)


def test_clamp_day_of_month_31st_in_february_leap_year():
    assert _clamp_day_of_month(2024, 2, 31) == date(2024, 2, 29)


def test_clamp_day_of_month_no_clamp_needed():
    assert _clamp_day_of_month(2026, 3, 15) == date(2026, 3, 15)


def test_bill_occurrences_clamps_across_30_day_month():
    bill = RecurringBill(name="Rent", amount=1000.0, day_of_month=31)
    occurrences = _bill_occurrences(bill, date(2026, 4, 1), date(2026, 4, 30))
    assert occurrences == [date(2026, 4, 30)]


def test_bill_occurrences_spans_multiple_months():
    bill = RecurringBill(name="Rent", amount=1000.0, day_of_month=1)
    occurrences = _bill_occurrences(bill, date(2026, 1, 15), date(2026, 3, 15))
    assert occurrences == [date(2026, 2, 1), date(2026, 3, 1)]


# --- Paycheck cadence from anchor date ---


def test_paycheck_occurrences_from_anchor():
    pay_period = PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14)
    occurrences = _paycheck_occurrences(pay_period, date(2026, 1, 1), date(2026, 2, 15))
    assert occurrences == [
        date(2026, 1, 1),
        date(2026, 1, 15),
        date(2026, 1, 29),
        date(2026, 2, 12),
    ]


def test_paycheck_occurrences_start_mid_cycle_across_month_boundary():
    # Cadence from anchor 2026-01-06 every 14 days: 1/6, 1/20, 2/3, 2/17...
    # Window starts mid-cycle (1/25) and crosses a month boundary; the first
    # on-cadence date inside the window is 2/3, not the anchor itself.
    pay_period = PayPeriodConfig(start_date=date(2026, 1, 6), frequency_days=14)
    occurrences = _paycheck_occurrences(pay_period, date(2026, 1, 25), date(2026, 2, 10))
    assert occurrences == [date(2026, 2, 3)]


def test_paycheck_occurrences_window_starts_exactly_on_a_paycheck():
    pay_period = PayPeriodConfig(start_date=date(2026, 1, 6), frequency_days=14)
    occurrences = _paycheck_occurrences(pay_period, date(2026, 1, 20), date(2026, 1, 20))
    assert occurrences == [date(2026, 1, 20)]


# --- Running balance math ---


def test_project_cash_flow_running_balance():
    pay_period = PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14)
    bills = [RecurringBill(name="Rent", amount=500.0, day_of_month=5)]

    result = project_cash_flow(
        current_balance=1000.0,
        pay_period=pay_period,
        paycheck_amount=800.0,
        recurring_bills=bills,
        temporary_expenses=[],
        start_date=date(2026, 1, 1),
        horizon_days=10,
    )

    by_date = {d["date"]: d for d in result["days"]}

    # Day 1: paycheck lands (start_date coincides with the pay period anchor)
    assert by_date["2026-01-01"]["net_change"] == 800.0
    assert by_date["2026-01-01"]["projected_balance"] == 1800.0

    # No events in between — balance holds flat
    assert by_date["2026-01-03"]["net_change"] == 0.0
    assert by_date["2026-01-03"]["projected_balance"] == 1800.0

    # Day 5: bill lands
    assert by_date["2026-01-05"]["net_change"] == -500.0
    assert by_date["2026-01-05"]["projected_balance"] == 1300.0

    assert result["starting_balance"] == 1000.0
    assert len(result["days"]) == 10


def test_project_cash_flow_monthly_summary_aggregates():
    pay_period = PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=30)
    bills = [RecurringBill(name="Rent", amount=200.0, day_of_month=15)]

    # Horizon confined to exactly January: paychecks land on 1/1 and 1/31
    # (both inside January), bill lands once on 1/15.
    result = project_cash_flow(
        current_balance=0.0,
        pay_period=pay_period,
        paycheck_amount=1000.0,
        recurring_bills=bills,
        temporary_expenses=[],
        start_date=date(2026, 1, 1),
        horizon_days=31,
    )

    assert result["monthly"] == [
        {"month": "2026-01", "projected_in": 2000.0, "projected_out": -200.0, "projected_net": 1800.0}
    ]


# --- Negative-balance warning detection ---


def test_negative_balance_warning_detected():
    pay_period = PayPeriodConfig(start_date=date(2026, 6, 1), frequency_days=30)
    bills = [RecurringBill(name="Big Bill", amount=2000.0, day_of_month=10)]

    result = project_cash_flow(
        current_balance=500.0,
        pay_period=pay_period,
        paycheck_amount=0.0,
        recurring_bills=bills,
        temporary_expenses=[],
        start_date=date(2026, 1, 1),
        horizon_days=20,
    )

    assert result["warnings"]["goes_negative"] is True
    assert result["warnings"]["first_negative_date"] == "2026-01-10"
    assert result["warnings"]["min_balance"] == -1500.0
    assert result["warnings"]["min_balance_date"] == "2026-01-10"


def test_no_negative_balance_warning_when_balance_stays_positive():
    pay_period = PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14)
    result = project_cash_flow(
        current_balance=10000.0,
        pay_period=pay_period,
        paycheck_amount=1000.0,
        recurring_bills=[RecurringBill(name="Rent", amount=500.0, day_of_month=1)],
        temporary_expenses=[],
        start_date=date(2026, 1, 1),
        horizon_days=30,
    )

    assert result["warnings"]["goes_negative"] is False
    assert result["warnings"]["first_negative_date"] is None


# --- Temporary expense inclusion within its date range only ---


def test_temporary_expense_occurrence_maps_half_to_day():
    assert _temporary_expense_occurrence(
        TemporaryExpense(name="Vet Bill", amount=300.0, half=1), date(2026, 3, 20)
    ) == date(2026, 3, 1)
    assert _temporary_expense_occurrence(
        TemporaryExpense(name="Vet Bill", amount=300.0, half=2), date(2026, 3, 1)
    ) == date(2026, 3, 16)


def test_temporary_expense_included_when_within_horizon():
    expense = TemporaryExpense(name="Vet Bill", amount=300.0, half=2)

    result = project_cash_flow(
        current_balance=1000.0,
        pay_period=PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14),
        paycheck_amount=0.0,
        recurring_bills=[],
        temporary_expenses=[expense],
        start_date=date(2026, 3, 1),
        horizon_days=31,
    )

    by_date = {d["date"]: d for d in result["days"]}
    assert by_date["2026-03-16"]["net_change"] == -300.0
    assert by_date["2026-03-16"]["events"][0]["type"] == "temporary"
    assert by_date["2026-03-16"]["events"][0]["name"] == "Vet Bill"


def test_temporary_expense_excluded_when_outside_horizon():
    # half 1 (day 1 of the month) already passed relative to a horizon
    # starting mid-month — it must not reappear anywhere in the projection.
    expense = TemporaryExpense(name="Vet Bill", amount=300.0, half=1)

    result = project_cash_flow(
        current_balance=1000.0,
        pay_period=PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14),
        paycheck_amount=0.0,
        recurring_bills=[],
        temporary_expenses=[expense],
        start_date=date(2026, 3, 20),
        horizon_days=10,
    )

    for day in result["days"]:
        assert day["events"] == []


# --- Horizon bounds ---


def test_clamp_horizon_below_minimum():
    assert clamp_horizon(0) == MIN_HORIZON_DAYS
    assert clamp_horizon(-5) == MIN_HORIZON_DAYS


def test_clamp_horizon_above_maximum():
    assert clamp_horizon(1000) == MAX_HORIZON_DAYS


def test_clamp_horizon_within_range_unchanged():
    assert clamp_horizon(90) == 90


def test_clamp_horizon_invalid_input_falls_back_to_default():
    assert clamp_horizon("not-a-number") == DEFAULT_HORIZON_DAYS
    assert clamp_horizon(None) == DEFAULT_HORIZON_DAYS


def test_project_cash_flow_clamps_horizon_internally():
    result = project_cash_flow(
        current_balance=100.0,
        pay_period=PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14),
        paycheck_amount=0.0,
        recurring_bills=[],
        temporary_expenses=[],
        start_date=date(2026, 1, 1),
        horizon_days=500,
    )
    assert result["horizon_days"] == MAX_HORIZON_DAYS
    assert len(result["days"]) == MAX_HORIZON_DAYS


def test_project_cash_flow_clamps_horizon_below_minimum():
    result = project_cash_flow(
        current_balance=100.0,
        pay_period=PayPeriodConfig(start_date=date(2026, 1, 1), frequency_days=14),
        paycheck_amount=0.0,
        recurring_bills=[],
        temporary_expenses=[],
        start_date=date(2026, 1, 1),
        horizon_days=0,
    )
    assert result["horizon_days"] == MIN_HORIZON_DAYS
    assert len(result["days"]) == MIN_HORIZON_DAYS


# --- Paycheck amount derivation ---


def test_derive_paycheck_amount_uses_median_of_period_totals():
    # Mirrors finance.analytics.biweekly_income's output shape: one row per
    # pay period with a total_income column (already summed across any
    # small non-payroll credits that landed in that period).
    biweekly_income = pd.DataFrame(
        {
            "period_start": pd.to_datetime(["2026-01-01", "2026-01-15", "2026-01-29"]),
            "period_end": pd.to_datetime(["2026-01-14", "2026-01-28", "2026-02-11"]),
            "total_income": [3600.0, 3650.0, 3700.0],
            "transaction_count": [1, 2, 1],
        }
    )
    assert derive_paycheck_amount(biweekly_income) == 3650.0


def test_derive_paycheck_amount_not_skewed_by_small_non_payroll_credits():
    # A period containing one real paycheck plus several small refund/interest
    # credits should still summarize near the paycheck amount, not collapse
    # toward the small per-transaction values (that's the whole point of
    # aggregating to the period level before taking the median).
    biweekly_income = pd.DataFrame(
        {
            "period_start": pd.to_datetime(["2026-01-01"]),
            "period_end": pd.to_datetime(["2026-01-14"]),
            "total_income": [3611.36],  # 3600 payroll + 0.10 + 0.17 + 10.09 interest/refunds
            "transaction_count": [4],
        }
    )
    assert derive_paycheck_amount(biweekly_income) == 3611.36


def test_derive_paycheck_amount_empty_df_returns_zero():
    df = pd.DataFrame(columns=["period_start", "period_end", "total_income", "transaction_count"])
    assert derive_paycheck_amount(df) == 0.0


def test_derive_paycheck_amount_none_returns_zero():
    assert derive_paycheck_amount(None) == 0.0


def test_derive_paycheck_amount_missing_column_returns_zero():
    df = pd.DataFrame({"period_start": pd.to_datetime(["2026-01-01"]), "other": [1.0]})
    assert derive_paycheck_amount(df) == 0.0
