from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from flask import Flask

from finance.analytics import attribute_income_months, summary_statistics
from finance.config_loader import IncomeAttributionConfig
import finance.blueprints.dashboard as dashboard_module


DEFAULT_KEYWORDS = ["payroll", "direct deposit", "salary"]


def make_df(rows: list[tuple[str, str, float, str]]) -> pd.DataFrame:
    """Build a transactions DataFrame in the analytics in-memory shape.

    rows: list of (date_iso, description, amount, category).
    """
    if not rows:
        df = pd.DataFrame(
            columns=["date", "description", "amount", "account_name",
                     "account_type", "raw_balance", "category", "subcategory"]
        )
        df["date"] = pd.to_datetime(df["date"])
        return df
    dates, descs, amounts, cats = zip(*rows)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(dates)),
            "description": list(descs),
            "amount": list(amounts),
            "account_name": ["Test Checking"] * len(rows),
            "account_type": ["checking"] * len(rows),
            "raw_balance": [0.0] * len(rows),
            "category": list(cats),
            "subcategory": [None] * len(rows),
        }
    ).sort_values("date").reset_index(drop=True)


# --- attribute_income_months: pure function ---


def test_shift_within_window_moves_to_next_month():
    """A paycheck posting in the last N days of the month attributes to
    next month. June has 30 days; shift_days=4 -> days 27-30 shift."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-06-29", "Wind River Syste XX4681 Aa - Payroll", 3697.71, "income"),
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2026-07"


def test_shift_outside_window_stays_in_posting_month():
    """A paycheck earlier in the month (outside the shift window) is not
    moved, even though the description matches a paycheck keyword."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-06-12", "Wind River Syste XX4681 Aa - Payroll", 3697.70, "income"),
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2026-06"


def test_year_boundary_december_rolls_to_january():
    """Dec 29 (last 4 days of a 31-day December) shifts to January of the
    following year, not month 13 of the same year."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-12-29", "ACME Corp Payroll", 2000.0, "income"),
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2027-01"


def test_non_paycheck_income_never_shifts_even_in_window():
    """Interest and refund income in the shift window stay in their
    posting month — only keyword-matched paycheck income shifts."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-06-29", "Interest Income", 0.19, "income"),
        ("2026-06-30", "Target", 75.69, "income"),  # refund-shaped income
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2026-06"
    assert str(attributed.iloc[1]) == "2026-06"


def test_expense_never_shifts_even_if_keyword_matches():
    """Non-income rows never shift regardless of description or date."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-06-29", "Payroll Processing Fee", -25.0, "expense"),
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2026-06"


def test_feature_off_shift_days_zero_is_pure_calendar_months():
    """shift_days=0 (the default) disables attribution entirely — every
    row, including in-window paycheck-keyword income, keeps its own
    posting month."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=0, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-06-29", "Wind River Syste XX4681 Aa - Payroll", 3697.71, "income"),
        ("2026-12-29", "ACME Corp Payroll", 2000.0, "income"),
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2026-06"
    assert str(attributed.iloc[1]) == "2026-12"


def test_default_config_disables_shifting():
    """The dataclass default (no config block present) is shift_days=0."""
    cfg = IncomeAttributionConfig()
    assert cfg.paycheck_shift_days == 0
    df = make_df([
        ("2026-06-29", "Wind River Syste XX4681 Aa - Payroll", 3697.71, "income"),
    ])
    attributed = attribute_income_months(df, cfg)
    assert str(attributed.iloc[0]) == "2026-06"


def test_empty_df_returns_empty_series():
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([])
    attributed = attribute_income_months(df, cfg)
    assert attributed.empty


# --- summary_statistics: income_this_month + shifted_from + savings_rate ---


class _FrozenDate(date):
    """Freezes date.today() inside finance.analytics for deterministic tests."""

    _frozen = date(2026, 7, 2)

    @classmethod
    def today(cls):
        return cls._frozen


@pytest.fixture
def frozen_today(monkeypatch):
    import finance.analytics as analytics_module
    monkeypatch.setattr(analytics_module, "date", _FrozenDate)
    return _FrozenDate._frozen


def test_income_this_month_includes_shifted_paycheck(frozen_today):
    """Regression test for the real user story: a June 29 paycheck should
    count toward July's income_this_month, not June's."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-06-12", "Wind River Syste XX4681 Aa - Payroll", 3697.70, "income"),
        ("2026-06-29", "Wind River Syste XX4681 Aa - Payroll", 3697.71, "income"),
        ("2026-07-01", "Interest Income", 0.15, "income"),
        ("2026-07-01", "Interest Income", 0.09, "income"),
        ("2026-07-01", "Interest Income", 0.01, "income"),
        ("2026-07-01", "Waiver", 5.00, "income"),
    ])
    empty_biweekly = pd.DataFrame(columns=["period_start", "period_end", "total_spending", "transaction_count"])
    empty_nw = pd.DataFrame(columns=["date", "net_worth"])

    result = summary_statistics(df, empty_nw, empty_biweekly, cfg)

    assert result["income_this_month"] == pytest.approx(3702.96, abs=0.01)
    assert len(result["income_this_month_shifted_from"]) == 1
    shifted = result["income_this_month_shifted_from"][0]
    assert shifted["date"] == "2026-06-29"
    assert shifted["amount"] == pytest.approx(3697.71, abs=0.01)
    assert "Payroll" in shifted["description"]


def test_income_this_month_shifted_from_empty_when_no_shift(frozen_today):
    """When no paycheck falls in the shift window, the explainer list is
    empty (not omitted — the field is always present)."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    df = make_df([
        ("2026-07-01", "Interest Income", 0.15, "income"),
    ])
    empty_biweekly = pd.DataFrame(columns=["period_start", "period_end", "total_spending", "transaction_count"])
    empty_nw = pd.DataFrame(columns=["date", "net_worth"])

    result = summary_statistics(df, empty_nw, empty_biweekly, cfg)

    assert result["income_this_month_shifted_from"] == []


def test_income_this_month_disabled_feature_matches_pure_calendar(frozen_today):
    """With income_attribution omitted (default), income_this_month is the
    old pure-calendar-month behavior — the June 29 paycheck stays in June
    and does NOT count toward July."""
    df = make_df([
        ("2026-06-29", "Wind River Syste XX4681 Aa - Payroll", 3697.71, "income"),
        ("2026-07-01", "Interest Income", 0.15, "income"),
    ])
    empty_biweekly = pd.DataFrame(columns=["period_start", "period_end", "total_spending", "transaction_count"])
    empty_nw = pd.DataFrame(columns=["date", "net_worth"])

    result = summary_statistics(df, empty_nw, empty_biweekly)  # no income_attribution passed

    assert result["income_this_month"] == pytest.approx(0.15, abs=0.01)
    assert result["income_this_month_shifted_from"] == []


def test_savings_rate_uses_attributed_months_consistently(frozen_today):
    """A paycheck that shifts INTO the trailing-3-month window (even from
    a posting date before the window's raw start) must be counted in the
    savings-rate income figure — income and the months it divides over
    have to agree on attributed months, not raw posting dates."""
    cfg = IncomeAttributionConfig(paycheck_shift_days=4, paycheck_keywords=DEFAULT_KEYWORDS)
    # frozen_today = 2026-07-02 -> trailing 3 calendar months = May, June, July.
    # April 29 (30-day month) is in April's shift window and rolls to May,
    # which IS inside the 3-month window, even though April 29 itself is
    # before the window's raw start date (May 1).
    df = make_df([
        ("2026-04-29", "ACME Corp Payroll", 1000.0, "income"),
        ("2026-05-05", "Rent", -200.0, "expense"),
    ])
    empty_biweekly = pd.DataFrame(columns=["period_start", "period_end", "total_spending", "transaction_count"])
    empty_nw = pd.DataFrame(columns=["date", "net_worth"])

    result = summary_statistics(df, empty_nw, empty_biweekly, cfg)

    # income_3m = 1000 (attributed to May), expenses_3m = 200
    assert result["savings_rate"] == pytest.approx(80.0, abs=0.1)


def test_savings_rate_zero_income_no_shift():
    """Feature-off sanity check: savings_rate still computes over a plain
    3-calendar-month window when there's no income at all."""
    df = make_df([
        ("2026-06-01", "Groceries", -100.0, "expense"),
    ])
    empty_biweekly = pd.DataFrame(columns=["period_start", "period_end", "total_spending", "transaction_count"])
    empty_nw = pd.DataFrame(columns=["date", "net_worth"])
    result = summary_statistics(df, empty_nw, empty_biweekly)
    assert result["savings_rate"] == 0


# --- /api/summary field presence ---


@pytest.fixture
def summary_app(monkeypatch):
    canned_summary = {
        "current_net_worth": 1000.0,
        "income_this_month": 3702.96,
        "income_this_month_shifted_from": [
            {"date": "2026-06-29", "amount": 3697.71, "description": "Wind River Syste XX4681 Aa - Payroll"}
        ],
        "savings_rate": 42.0,
    }
    monkeypatch.setattr(dashboard_module, "get_cache", lambda: {"summary": canned_summary})

    flask_app = Flask(__name__)
    flask_app.register_blueprint(dashboard_module.dashboard_bp)
    return flask_app


def test_api_summary_includes_shifted_from_field(summary_app):
    client = summary_app.test_client()
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "income_this_month_shifted_from" in data
    assert data["income_this_month_shifted_from"][0]["date"] == "2026-06-29"
    assert data["income_this_month"] == pytest.approx(3702.96, abs=0.01)
