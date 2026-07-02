from __future__ import annotations

import json

from finance import db, migrate
from finance.config_loader import CategorizationRule
from tests.conftest import make_config, make_csv_account


def _write_fixture_data(tmp_path):
    """Synthesize a data dir with one CSV + manual_balances.json (never real user data)."""
    (tmp_path / "Test.csv").write_text(
        "Date,Description,Amount\n"
        "01/05/2026,WALMART GROCERY,-52.30\n"
        "01/06/2026,ACME CORP PAYROLL,1500.00\n"
        "01/07/2026,Xfer To Savings,-200.00\n"
    )
    (tmp_path / "manual_balances.json").write_text(json.dumps([
        {"account": "Brokerage", "date": "2026-01-31", "balance": 50000.0},
        {"account": "Brokerage", "date": "2026-02-28", "balance": 51000.0},
    ]))


def test_startup_migration_seeds_everything(tmp_path, classification):
    _write_fixture_data(tmp_path)
    config = make_config(
        accounts=[make_csv_account("Test.csv")],
        classification=classification,
        rules=[CategorizationRule(category="Groceries", keywords=["walmart"])],
    )

    conn = db.get_connection(str(tmp_path / "finance.db"))
    migrate.run_startup_migration(conn, config, str(tmp_path))

    assert db.count_transactions(conn) == 3
    accounts = {row["name"]: row for row in conn.execute("SELECT * FROM accounts").fetchall()}
    assert accounts["Test Checking"]["source"] == "csv"
    assert accounts["Brokerage"]["source"] == "manual"
    assert accounts["Brokerage"]["type"] == "manual_balance"

    rules = conn.execute("SELECT * FROM categorization_rules").fetchall()
    assert len(rules) == 1 and rules[0]["keyword"] == "walmart"

    snapshots = conn.execute("SELECT * FROM balance_snapshots").fetchall()
    assert len(snapshots) == 2

    # Rules were applied during import
    row = conn.execute(
        "SELECT category FROM transactions WHERE description = 'WALMART GROCERY'"
    ).fetchone()
    assert row["category"] == "Groceries"
    conn.close()


def test_startup_migration_is_idempotent(tmp_path, classification):
    _write_fixture_data(tmp_path)
    config = make_config(
        accounts=[make_csv_account("Test.csv")],
        classification=classification,
        rules=[CategorizationRule(category="Groceries", keywords=["walmart"])],
    )

    conn = db.get_connection(str(tmp_path / "finance.db"))
    migrate.run_startup_migration(conn, config, str(tmp_path))
    first_counts = _table_counts(conn)

    migrate.run_startup_migration(conn, config, str(tmp_path))
    second_counts = _table_counts(conn)

    assert first_counts == second_counts
    assert second_counts["transactions"] == 3
    assert second_counts["accounts"] == 2
    assert second_counts["balance_snapshots"] == 2
    assert second_counts["categorization_rules"] == 1
    conn.close()


def _table_counts(conn) -> dict[str, int]:
    tables = ["accounts", "transactions", "balance_snapshots", "categorization_rules"]
    return {
        t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        for t in tables
    }


def test_loaded_dataframe_shape(tmp_path, classification):
    _write_fixture_data(tmp_path)
    config = make_config(
        accounts=[make_csv_account("Test.csv")],
        classification=classification,
    )

    conn = db.get_connection(str(tmp_path / "finance.db"))
    migrate.run_startup_migration(conn, config, str(tmp_path))

    df = db.load_transactions_df(conn)
    assert list(df.columns) == [
        "date", "description", "amount", "account_name", "account_type",
        "raw_balance", "category", "subcategory",
    ]
    # category column carries income/expense/transfer (the analytics contract)
    assert set(df["category"]) == {"income", "expense", "transfer"}

    manual = db.load_balance_snapshots_df(conn, source="manual")
    assert list(manual.columns) == ["date", "account_name", "balance", "source"]
    assert len(manual) == 2
    conn.close()
