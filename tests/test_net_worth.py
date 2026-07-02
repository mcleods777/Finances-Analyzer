from __future__ import annotations

import pandas as pd
import pytest

from finance import db
from finance.blueprints.dashboard import _net_worth_change
from finance.data_processor import compute_daily_balances, compute_net_worth_series
from tests.conftest import make_config


def _txn_df(account_name: str, account_type: str, dates: list[str],
            amounts: list[float], raw_balances: list[float] | None = None) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "description": ["txn"] * n,
            "amount": amounts,
            "account_name": [account_name] * n,
            "account_type": [account_type] * n,
            "raw_balance": raw_balances if raw_balances is not None else [None] * n,
        }
    )


def _snapshot_df(account_name: str, dates: list[str], balances: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "account_name": [account_name] * len(dates),
            "balance": balances,
        }
    )


def _empty_config():
    return make_config(accounts=[], classification=_dummy_classification())


def _dummy_classification():
    from finance.config_loader import ClassificationConfig
    return ClassificationConfig()


def _balance_on(daily: pd.DataFrame, account_name: str, day: str) -> float:
    row = daily[(daily["account_name"] == account_name) & (daily["date"] == pd.Timestamp(day))]
    assert not row.empty, f"no row for {account_name} on {day}"
    return float(row["balance"].iloc[0])


# --- Snapshot-vs-derived precedence and anchoring ---


def test_snapshot_wins_over_derived_from_its_date_onward():
    # Transactions give a cumsum trajectory of 100 -> 100 -> 100 -> 100 (no txns after day 1),
    # but a snapshot on day 3 says the real balance is much higher (e.g. interest/dividend
    # posted outside the CSV feed). The snapshot should win from day 3 onward; day 1-2 should
    # still reflect the transaction-derived balance. A day-4 transaction (unrelated amount
    # already reflected via raw_balance elsewhere) just extends the date range so we can
    # observe the anchored value persists past the snapshot date too.
    txns = _txn_df(
        "Brokerage", "investment",
        ["2026-01-01", "2026-01-04"], [100.0, 0.0],
    )
    snapshots = _snapshot_df("Brokerage", ["2026-01-03"], [500.0])

    daily = compute_daily_balances(txns, _empty_config(), snapshots)

    assert _balance_on(daily, "Brokerage", "2026-01-01") == 100.0
    assert _balance_on(daily, "Brokerage", "2026-01-02") == 100.0
    # Snapshot day and onward: anchored to the snapshot, not the stale derived value
    assert _balance_on(daily, "Brokerage", "2026-01-03") == 500.0
    assert _balance_on(daily, "Brokerage", "2026-01-04") == 500.0


def test_forward_fill_between_two_snapshots():
    snapshots = _snapshot_df(
        "Fidelity 401k",
        ["2026-01-01", "2026-01-10", "2026-01-20"],
        [1000.0, 2000.0, 2500.0],
    )
    empty_txns = pd.DataFrame(columns=["date", "description", "amount", "account_name", "account_type", "raw_balance"])

    daily = compute_daily_balances(empty_txns, _empty_config(), snapshots)

    # Between snapshots: held flat at the earlier snapshot's value
    assert _balance_on(daily, "Fidelity 401k", "2026-01-05") == 1000.0
    # On/after the second snapshot: the new value, held flat until the third
    assert _balance_on(daily, "Fidelity 401k", "2026-01-10") == 2000.0
    assert _balance_on(daily, "Fidelity 401k", "2026-01-15") == 2000.0
    assert _balance_on(daily, "Fidelity 401k", "2026-01-20") == 2500.0


def test_snapshot_only_account_zero_before_first_snapshot():
    # Another account's transactions extend the global date range back before this
    # account's first snapshot, so we can observe the "unknown history" 0-fill.
    other_txns = _txn_df("Main Checking", "checking", ["2026-01-01"], [0.0], raw_balances=[100.0])
    snapshots = _snapshot_df("Fidelity IRA", ["2026-01-15"], [42000.0])

    daily = compute_daily_balances(other_txns, _empty_config(), snapshots)

    assert _balance_on(daily, "Fidelity IRA", "2026-01-01") == 0.0
    assert _balance_on(daily, "Fidelity IRA", "2026-01-14") == 0.0
    assert _balance_on(daily, "Fidelity IRA", "2026-01-15") == 42000.0


def test_derived_only_account_unaffected_by_absent_snapshots():
    # No snapshots at all for this account — behaves exactly like plain transaction cumsum.
    txns = _txn_df(
        "Main Checking", "checking",
        ["2026-01-01", "2026-01-02", "2026-01-03"],
        [100.0, -30.0, 50.0],
    )
    daily = compute_daily_balances(txns, _empty_config(), manual_balances_df=None)

    assert _balance_on(daily, "Main Checking", "2026-01-01") == 100.0
    assert _balance_on(daily, "Main Checking", "2026-01-02") == 70.0
    assert _balance_on(daily, "Main Checking", "2026-01-03") == 120.0


def test_derived_uses_bank_reported_balance_column_when_present():
    txns = _txn_df(
        "Main Checking", "checking",
        ["2026-01-01", "2026-01-03"],
        [100.0, -30.0],
        raw_balances=[1000.0, 970.0],
    )
    daily = compute_daily_balances(txns, _empty_config(), manual_balances_df=None)

    # Day without a transaction (01-02) forward-filled from the last known balance
    assert _balance_on(daily, "Main Checking", "2026-01-02") == 1000.0
    assert _balance_on(daily, "Main Checking", "2026-01-03") == 970.0


# --- Liability sign handling ---


def test_liability_balances_count_as_negative_net_worth_regardless_of_stored_sign():
    # Credit card stored as bank normally reports it: negative for money owed.
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-01"]),
        "account_name": ["Checking", "Credit Card"],
        "account_type": ["checking", "credit_card"],
        "balance": [1000.0, -300.0],
    })
    nw = compute_net_worth_series(daily)

    row = nw.iloc[0]
    assert row["liabilities_total"] == pytest.approx(300.0)
    assert row["assets_total"] == pytest.approx(1000.0)
    assert row["net_worth"] == pytest.approx(700.0)


def test_liability_balance_stored_positive_is_still_treated_as_debt():
    # Some sources might report the owed amount as a positive number; sign
    # handling is applied by account type (abs), not by trusting the stored sign.
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-01"]),
        "account_name": ["Checking", "Loan"],
        "account_type": ["checking", "loan"],
        "balance": [1000.0, 300.0],
    })
    nw = compute_net_worth_series(daily)

    row = nw.iloc[0]
    assert row["liabilities_total"] == pytest.approx(300.0)
    assert row["net_worth"] == pytest.approx(700.0)


def test_investment_and_manual_balance_types_count_as_assets():
    daily = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01"] * 3),
        "account_name": ["Checking", "Brokerage", "Fidelity 401k"],
        "account_type": ["checking", "investment", "manual_balance"],
        "balance": [1000.0, 5000.0, 20000.0],
    })
    nw = compute_net_worth_series(daily)

    row = nw.iloc[0]
    assert row["assets_total"] == pytest.approx(26000.0)
    assert row["liabilities_total"] == pytest.approx(0.0)
    assert row["net_worth"] == pytest.approx(26000.0)


# --- 30d / 90d change math (dashboard blueprint) ---


def _flat_then_step_net_worth(days: int, before: float, after: float, step_at: int) -> pd.DataFrame:
    """A net_worth series that's `before` for the first `step_at` days, then `after`."""
    dates = pd.date_range("2026-01-01", periods=days, freq="D")
    values = [before if i < step_at else after for i in range(days)]
    return pd.DataFrame({"date": dates, "net_worth": values})


def test_net_worth_change_30d_positive_change():
    nw = _flat_then_step_net_worth(days=40, before=10000.0, after=12000.0, step_at=35)
    result = _net_worth_change(nw, 30)

    # 30 days before the last date (day 40) is day 10 -> still `before`
    assert result["change"] == pytest.approx(2000.0)
    assert result["pct"] == pytest.approx(20.0)


def test_net_worth_change_30d_negative_change():
    nw = _flat_then_step_net_worth(days=40, before=10000.0, after=8000.0, step_at=35)
    result = _net_worth_change(nw, 30)

    assert result["change"] == pytest.approx(-2000.0)
    assert result["pct"] == pytest.approx(-20.0)


def test_net_worth_change_no_data_far_enough_back_returns_zero():
    nw = _flat_then_step_net_worth(days=10, before=10000.0, after=12000.0, step_at=5)
    result = _net_worth_change(nw, 30)

    assert result["change"] == 0
    assert result["pct"] == 0


def test_net_worth_change_handles_zero_baseline():
    dates = pd.date_range("2026-01-01", periods=40, freq="D")
    values = [0.0] * 35 + [500.0] * 5
    nw = pd.DataFrame({"date": dates, "net_worth": values})
    result = _net_worth_change(nw, 30)

    assert result["change"] == pytest.approx(500.0)
    # Division by zero baseline is guarded — pct falls back to 0
    assert result["pct"] == 0


# --- Integration through the real DB layer (mirrors data_service.refresh_data) ---


def test_full_pipeline_through_db_layer(conn):
    # Seed one CSV-style account with transactions + one manual-snapshot account,
    # the same way migrate.py / manual_balances.py populate the real DB.
    checking_id = db.upsert_account(conn, name="Main Checking", account_type="checking", source="csv")
    cc_id = db.upsert_account(conn, name="Visa", account_type="credit_card", source="csv")
    with conn:
        conn.execute(
            "INSERT INTO transactions (account_id, date, description, amount, txn_type, "
            "raw_balance, dedup_hash, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (checking_id, "2026-01-01", "Paycheck", 1000.0, "income", 1000.0, "h1", "csv"),
        )
        conn.execute(
            "INSERT INTO transactions (account_id, date, description, amount, txn_type, "
            "raw_balance, dedup_hash, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cc_id, "2026-01-01", "Groceries", -50.0, "expense", -50.0, "h2", "csv"),
        )
        brokerage_id = db.upsert_account(conn, name="Brokerage", account_type="manual_balance", source="manual")
        conn.execute(
            "INSERT INTO balance_snapshots (account_id, date, balance, source) VALUES (?, ?, ?, 'manual')",
            (brokerage_id, "2026-01-01", 10000.0),
        )

    df = db.load_transactions_df(conn)
    snapshots = db.load_balance_snapshots_df(conn, source="manual")
    manual_df = snapshots[["date", "account_name", "balance"]]

    daily = compute_daily_balances(df, _empty_config(), manual_df)
    nw = compute_net_worth_series(daily)

    latest = nw.iloc[-1]
    # 1000 (checking) + 10000 (brokerage) - 50 (credit card debt) = 10950
    assert latest["net_worth"] == pytest.approx(10950.0)
    assert latest["assets_total"] == pytest.approx(11000.0)
    assert latest["liabilities_total"] == pytest.approx(50.0)
