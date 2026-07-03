from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from finance.config_loader import RecurringBill
from finance.analytics import (
    _assign_bill_payments,
    _best_search_keyword,
    _get_bill_status_for_range,
)


def make_df(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Build a transactions DataFrame in the analytics in-memory shape.

    rows: list of (date_iso, description, amount) — all treated as expenses.
    """
    if not rows:
        df = pd.DataFrame(
            columns=["date", "description", "amount", "account_name",
                     "account_type", "raw_balance", "category", "subcategory"]
        )
        df["date"] = pd.to_datetime(df["date"])
        return df
    dates, descs, amounts = zip(*rows)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(dates)),
            "description": list(descs),
            "amount": list(amounts),
            "account_name": ["Test Checking"] * len(rows),
            "account_type": ["checking"] * len(rows),
            "raw_balance": [0.0] * len(rows),
            "category": ["expense"] * len(rows),
            "subcategory": [None] * len(rows),
        }
    ).sort_values("date").reset_index(drop=True)


def statuses_by_name(results: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in results}


# --- Amount-aware disambiguation ---


def test_overlapping_keyword_txn_assigned_to_closest_amount():
    """Two bills share a keyword; each txn goes to the bill with the
    closest |amount|."""
    bills = [
        RecurringBill(name="Allowance", amount=100.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
        RecurringBill(name="Rent", amount=1825.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
    ]
    df = make_df([
        ("2026-06-05", "Transfer to PayPal", -1825.0),
        ("2026-06-06", "Transfer to PayPal", -100.0),
    ])
    results = statuses_by_name(
        _get_bill_status_for_range(df, date(2026, 6, 1), date(2026, 6, 15), bills)
    )
    assert results["Rent"]["status"] == "paid"
    assert results["Rent"]["paid_amount"] == 1825.0
    assert results["Allowance"]["status"] == "paid"
    assert results["Allowance"]["paid_amount"] == 100.0


def test_one_txn_consumed_by_only_one_bill():
    """A single txn matching two bills is consumed by the closest-amount
    bill only; the other stays pending."""
    bills = [
        RecurringBill(name="Allowance", amount=100.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
        RecurringBill(name="Rent", amount=1825.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
    ]
    df = make_df([("2026-06-05", "Transfer to PayPal", -100.0)])
    results = statuses_by_name(
        _get_bill_status_for_range(df, date(2026, 6, 1), date(2026, 6, 15), bills)
    )
    assert results["Allowance"]["status"] == "paid"
    assert results["Rent"]["status"] == "pending"
    assert "paid_amount" not in results["Rent"]


def test_two_similar_txns_do_not_double_pay_one_bill():
    """Two txns both closest to the same bill: the bill consumes the earliest
    one; the far-amount bill is NOT falsely marked paid by the leftover."""
    bills = [
        RecurringBill(name="Allowance", amount=100.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
        RecurringBill(name="Rent", amount=1825.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
    ]
    df = make_df([
        ("2026-06-04", "Transfer to PayPal", -95.0),
        ("2026-06-06", "Transfer to PayPal", -100.0),
    ])
    results = statuses_by_name(
        _get_bill_status_for_range(df, date(2026, 6, 1), date(2026, 6, 15), bills)
    )
    assert results["Allowance"]["status"] == "paid"
    assert results["Allowance"]["paid_amount"] == 95.0  # earliest assigned
    assert results["Rent"]["status"] == "pending"


def test_variable_amount_bill_matches_on_unique_keyword():
    """A utility whose amount varies month to month must still match when
    only its own keywords match (no amount proximity required)."""
    bills = [
        RecurringBill(name="MidAmerican", amount=199.37, day_of_month=5,
                      match_criteria=["midamerican energy"]),
    ]
    # Paid amount 40% off the configured amount
    df = make_df([("2026-06-05", "MidAmerican Energy", -120.06)])
    results = statuses_by_name(
        _get_bill_status_for_range(df, date(2026, 6, 1), date(2026, 6, 15), bills)
    )
    assert results["MidAmerican"]["status"] == "paid"
    assert results["MidAmerican"]["paid_amount"] == 120.06


def test_unique_match_takes_earliest_transaction():
    """Unique-keyword bills keep current behavior: first (earliest) match."""
    bills = [
        RecurringBill(name="Rent", amount=1825.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
    ]
    df = make_df([
        ("2026-06-05", "Transfer to PayPal", -1825.0),
        ("2026-06-08", "Transfer to PayPal", -42.38),
    ])
    results = statuses_by_name(
        _get_bill_status_for_range(df, date(2026, 6, 1), date(2026, 6, 15), bills)
    )
    assert results["Rent"]["status"] == "paid"
    assert results["Rent"]["paid_amount"] == 1825.0
    assert results["Rent"]["paid_date"] == "2026-06-05"


def test_assign_bill_payments_no_match_returns_none():
    bills = [
        RecurringBill(name="Netflix", amount=39.57, day_of_month=3,
                      match_criteria=["netflix"]),
    ]
    df = make_df([("2026-06-05", "Transfer to PayPal", -1825.0)])
    df["desc_lower"] = df["description"].str.lower()
    paid = _assign_bill_payments(df, bills)
    assert paid == {0: None}


# --- search_keyword selection ---


def test_search_keyword_prefers_recent_era_keyword():
    """search_keyword is the criteria entry matching the most transactions
    in the last 120 days, not simply the first entry."""
    today = date.today()
    old = (today - timedelta(days=300)).isoformat()
    bill = RecurringBill(
        name="Netflix", amount=39.57, day_of_month=today.day,
        match_criteria=["netflix.com        netflix.com    caus", "netflix"],
    )
    df = make_df([
        (old, "Netflix.com        netflix.com    CAUS", -36.36),
        ((today - timedelta(days=60)).isoformat(), "Netflix", -39.57),
        ((today - timedelta(days=30)).isoformat(), "Netflix", -39.57),
    ])
    assert _best_search_keyword(bill, df) == "netflix"


def test_search_keyword_falls_back_to_first_entry():
    """With no matches in the last 120 days, fall back to the first entry."""
    today = date.today()
    old = (today - timedelta(days=300)).isoformat()
    bill = RecurringBill(
        name="Old Bill", amount=100.0, day_of_month=5,
        match_criteria=["old keyword", "new keyword"],
    )
    df = make_df([(old, "OLD KEYWORD vendor", -100.0)])
    assert _best_search_keyword(bill, df) == "old keyword"


def test_search_keyword_in_range_status_output():
    """The keyword picked by recency flows through to the API payload."""
    bill = RecurringBill(
        name="Netflix", amount=39.57, day_of_month=3,
        match_criteria=["netflix.com        netflix.com    caus", "netflix"],
    )
    today = date.today()
    recent = (today - timedelta(days=20)).isoformat()
    df = make_df([(recent, "Netflix", -39.57)])
    # Use a range around the recent txn's month so the bill is in-window
    r_start = date.fromisoformat(recent).replace(day=1)
    r_end = r_start + timedelta(days=14)
    results = _get_bill_status_for_range(df, r_start, r_end, [bill])
    assert results[0]["search_keyword"] == "netflix"


# --- Half-split grouping (server-side ranges) ---


def test_half_split_bills_grouped_by_day_of_month():
    """Bills due days 1-15 appear only in the first-half range; 16+ only in
    the second-half range (as consumed by compute_monthly_half_runway)."""
    bills = [
        RecurringBill(name="Rent", amount=1825.0, day_of_month=5,
                      match_criteria=["transfer to paypal"]),
        RecurringBill(name="Claude", amount=107.0, day_of_month=16,
                      match_criteria=["claude"]),
        RecurringBill(name="HBO Max", amount=11.65, day_of_month=23,
                      match_criteria=["hbo max"]),
    ]
    df = make_df([])
    half1 = _get_bill_status_for_range(df, date(2026, 7, 1), date(2026, 7, 15), bills)
    half2 = _get_bill_status_for_range(df, date(2026, 7, 16), date(2026, 7, 31), bills)
    assert [b["name"] for b in half1] == ["Rent"]
    assert [b["name"] for b in half2] == ["Claude", "HBO Max"]
    assert all(b["status"] == "pending" for b in half1 + half2)
