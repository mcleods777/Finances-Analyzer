from __future__ import annotations

import json
import os

import pandas as pd
import pytest
from flask import Flask

import finance.blueprints.accounts as accounts_module
import finance.blueprints.plaid as plaid_module
from finance import account_ops, db, importer, migrate, plaid_sync
from finance.data_processor import compute_net_worth_series
from tests.conftest import make_config, make_csv_account

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_YAML = """
pay_period:
  start_date: "2026-01-01"
  frequency_days: 14
accounts: []
classification:
  income_keywords: ["payroll"]
  expense_keywords: []
  transfer_keywords: ["transfer"]
"""


# --- Helpers ---


def _add_txn(conn, account_id, date, amount, description,
             source="csv", user_edited=0, category=None):
    dedup_hash = importer.compute_dedup_hash(account_id, date, amount, description)
    conn.execute(
        "INSERT INTO transactions (account_id, date, description, amount, category, "
        "txn_type, dedup_hash, source, user_edited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, date, description, amount, category,
         "expense" if amount < 0 else "income", dedup_hash, source, user_edited),
    )
    conn.commit()


def _add_snapshot(conn, account_id, date, balance, source="manual"):
    conn.execute(
        "INSERT INTO balance_snapshots (account_id, date, balance, source) VALUES (?, ?, ?, ?)",
        (account_id, date, balance, source),
    )
    conn.commit()


def _make_account(conn, name, account_type="checking", source="csv",
                  plaid_account_id=None, plaid_item_id=None, column_mapping=None):
    cur = conn.execute(
        "INSERT INTO accounts (name, type, source, plaid_account_id, plaid_item_id, column_mapping) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, account_type, source, plaid_account_id, plaid_item_id, column_mapping),
    )
    conn.commit()
    return int(cur.lastrowid)


def _make_plaid_item(conn, item_id="item-1", access_token="access-token-1",
                     institution_name="Test Bank"):
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token, institution_name) VALUES (?, ?, ?)",
        (item_id, access_token, institution_name),
    )
    conn.commit()


def _txns(conn, account_id):
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? ORDER BY date, id", (account_id,)
    ).fetchall()


# --- Schema migration (v1 -> v2) ---


def test_schema_v2_columns_and_institution_backfill(tmp_path):
    # Build a v1-shaped database by hand, then run init_db to migrate it.
    import sqlite3

    db_path = str(tmp_path / "v1.db")
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'csv',
            plaid_account_id TEXT,
            column_mapping TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE plaid_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL UNIQUE,
            access_token TEXT NOT NULL,
            institution_name TEXT,
            sync_cursor TEXT,
            last_synced_at TEXT
        );
        ALTER TABLE accounts ADD COLUMN plaid_item_id TEXT;
        INSERT INTO plaid_items (item_id, access_token, institution_name)
            VALUES ('item-9', 'tok', 'Backfill Bank');
        INSERT INTO accounts (name, type, source, plaid_account_id, plaid_item_id)
            VALUES ('Linked', 'checking', 'plaid', 'pa-9', 'item-9');
        """
    )
    raw.commit()
    raw.close()

    conn = db.get_connection(db_path)
    db.init_db(conn)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert {"hidden", "exclude_from_net_worth", "institution", "plaid_item_id"} <= cols
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 2
    row = conn.execute("SELECT * FROM accounts WHERE name = 'Linked'").fetchone()
    assert row["institution"] == "Backfill Bank"
    assert row["hidden"] == 0 and row["exclude_from_net_worth"] == 0
    conn.close()


# --- Rename persistence / migrate resurrection ---


def test_rename_survives_migrate_rerun(tmp_path, classification):
    (tmp_path / "Test.csv").write_text(
        "Date,Description,Amount\n"
        "01/05/2026,WALMART GROCERY,-52.30\n"
        "01/06/2026,ACME CORP PAYROLL,1500.00\n"
    )
    config = make_config(accounts=[make_csv_account("Test.csv")], classification=classification)
    conn = db.get_connection(str(tmp_path / "finance.db"))
    migrate.run_startup_migration(conn, config, str(tmp_path))
    account = db.get_account_by_name(conn, "Test Checking")

    # Rename via the same UPDATE the PATCH endpoint issues
    with conn:
        conn.execute("UPDATE accounts SET name = ? WHERE id = ?",
                     ("Primary Checking", account["id"]))

    # Simulate an app restart: migration must NOT resurrect "Test Checking"
    migrate.run_startup_migration(conn, config, str(tmp_path))

    names = [r["name"] for r in conn.execute("SELECT name FROM accounts").fetchall()]
    assert names == ["Primary Checking"]
    assert db.count_transactions(conn, int(account["id"])) == 2
    assert db.count_transactions(conn) == 2  # nothing re-imported elsewhere
    conn.close()


def test_migrate_does_not_resurrect_deleted_account(tmp_path, classification):
    (tmp_path / "Test.csv").write_text(
        "Date,Description,Amount\n01/05/2026,WALMART GROCERY,-52.30\n"
    )
    config = make_config(accounts=[make_csv_account("Test.csv")], classification=classification)
    conn = db.get_connection(str(tmp_path / "finance.db"))
    migrate.run_startup_migration(conn, config, str(tmp_path))
    account = db.get_account_by_name(conn, "Test Checking")

    account_ops.delete_account(conn, int(account["id"]), remove_remote=lambda tok: True)
    migrate.run_startup_migration(conn, config, str(tmp_path))

    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
    assert db.count_transactions(conn) == 0
    conn.close()


def test_merged_away_config_account_not_resurrected(tmp_path, classification):
    (tmp_path / "A.csv").write_text("Date,Description,Amount\n01/05/2026,ALPHA,-1.00\n")
    (tmp_path / "B.csv").write_text("Date,Description,Amount\n01/06/2026,BETA,-2.00\n")
    config = make_config(
        accounts=[make_csv_account("A.csv", name="Acct A"), make_csv_account("B.csv", name="Acct B")],
        classification=classification,
    )
    conn = db.get_connection(str(tmp_path / "finance.db"))
    migrate.run_startup_migration(conn, config, str(tmp_path))
    a = db.get_account_by_name(conn, "Acct A")
    b = db.get_account_by_name(conn, "Acct B")

    # Both accounts have config mappings; target keeps its own, so A's config
    # file must be tombstoned by the merge.
    account_ops.merge_accounts(conn, int(a["id"]), int(b["id"]))
    migrate.run_startup_migration(conn, config, str(tmp_path))

    assert db.get_account_by_name(conn, "Acct A") is None
    assert db.count_transactions(conn) == 2  # both txns live on Acct B
    conn.close()


# --- Merge semantics ---


def test_merge_moves_and_recomputes_hashes(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE SHOP")
    _add_txn(conn, src, "2026-01-06", 500.00, "PAYCHECK")

    result = account_ops.merge_accounts(conn, src, tgt)

    assert result == {"moved": 2, "duplicates_skipped": 0, "snapshots_moved": 0}
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE id = ?", (src,)).fetchone()["c"] == 0
    rows = _txns(conn, tgt)
    assert len(rows) == 2
    for row in rows:
        expected = importer.compute_dedup_hash(
            tgt, row["date"], row["amount"], row["description"]
        )
        assert row["dedup_hash"] == expected


def test_merge_one_to_one_overlap_two_identical_pairs(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    # Two genuinely identical purchases in the target (differing descriptions
    # keep their dedup hashes distinct) and two matching rows in the source.
    _add_txn(conn, tgt, "2026-01-05", -10.00, "COFFEE A")
    _add_txn(conn, tgt, "2026-01-05", -10.00, "COFFEE B")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE C")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE D")

    result = account_ops.merge_accounts(conn, src, tgt)

    assert result["duplicates_skipped"] == 2
    assert result["moved"] == 0
    assert len(_txns(conn, tgt)) == 2  # both target originals survive, once each


def test_merge_one_target_two_identical_sources(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    _add_txn(conn, tgt, "2026-01-05", -10.00, "COFFEE A")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE B")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE C")

    result = account_ops.merge_accounts(conn, src, tgt)

    assert result["duplicates_skipped"] == 1
    assert result["moved"] == 1
    assert len(_txns(conn, tgt)) == 2


def test_merge_hash_collision_duplicate_skipped_gracefully(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    _add_txn(conn, tgt, "2026-01-05", -10.00, "SAME DESC")
    # First source row absorbs the overlap; the second becomes a mover whose
    # recomputed hash collides with the target's identical row.
    _add_txn(conn, src, "2026-01-05", -10.00, "OTHER DESC")
    _add_txn(conn, src, "2026-01-05", -10.00, "SAME DESC")

    result = account_ops.merge_accounts(conn, src, tgt)

    # OTHER DESC moves (no target slot left after SAME matched), SAME collides.
    assert result["moved"] + result["duplicates_skipped"] == 2
    assert result["duplicates_skipped"] >= 1
    descs = {r["description"] for r in _txns(conn, tgt)}
    assert "SAME DESC" in descs


def test_merge_preserves_user_edited_category_on_matched_target(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    _add_txn(conn, tgt, "2026-01-05", -25.00, "GROCERY STORE PLAID", source="plaid")
    _add_txn(conn, src, "2026-01-05", -25.00, "GROCERY STORE CSV",
             source="csv", user_edited=1, category="Groceries")

    result = account_ops.merge_accounts(conn, src, tgt)

    assert result["duplicates_skipped"] == 1
    row = _txns(conn, tgt)[0]
    assert row["category"] == "Groceries"
    assert row["user_edited"] == 1


def test_merge_prefers_cross_source_pairs(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    # Target has one csv and one plaid row with the same (date, amount);
    # the csv source row should absorb into the plaid target row.
    _add_txn(conn, tgt, "2026-01-05", -25.00, "TARGET CSV ROW", source="csv")
    _add_txn(conn, tgt, "2026-01-05", -25.00, "TARGET PLAID ROW", source="plaid")
    _add_txn(conn, src, "2026-01-05", -25.00, "SOURCE CSV ROW",
             source="csv", user_edited=1, category="Marker")

    account_ops.merge_accounts(conn, src, tgt)

    plaid_row = conn.execute(
        "SELECT * FROM transactions WHERE description = 'TARGET PLAID ROW'"
    ).fetchone()
    assert plaid_row["category"] == "Marker"  # edit landed on the cross-source row


def test_merge_plaid_link_inherited_and_sync_routes_to_target(conn):
    _make_plaid_item(conn, item_id="item-1")
    src = _make_account(conn, "Plaid Source", source="plaid",
                        plaid_account_id="pa-1", plaid_item_id="item-1")
    conn.execute("UPDATE accounts SET institution = 'Test Bank' WHERE id = ?", (src,))
    conn.commit()
    tgt = _make_account(conn, "CSV Target")
    _add_txn(conn, src, "2026-01-05", -10.00, "BANK TXN", source="plaid")

    account_ops.merge_accounts(conn, src, tgt)

    target = conn.execute("SELECT * FROM accounts WHERE id = ?", (tgt,)).fetchone()
    assert target["plaid_account_id"] == "pa-1"
    assert target["plaid_item_id"] == "item-1"
    assert target["institution"] == "Test Bank"
    assert target["source"] == "plaid"
    # Future syncs route by plaid_account_id lookup — must resolve to the target.
    assert plaid_sync._plaid_account_map(conn) == {"pa-1": tgt}


def test_merge_snapshots_move_and_dedupe(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    _add_snapshot(conn, tgt, "2026-01-31", 1000.0)
    _add_snapshot(conn, src, "2026-01-31", 999.0)   # same date+source: dropped
    _add_snapshot(conn, src, "2026-02-28", 1100.0)  # moves

    result = account_ops.merge_accounts(conn, src, tgt)

    assert result["snapshots_moved"] == 1
    snaps = conn.execute(
        "SELECT date, balance FROM balance_snapshots WHERE account_id = ? ORDER BY date",
        (tgt,),
    ).fetchall()
    assert [(s["date"], s["balance"]) for s in snaps] == [
        ("2026-01-31", 1000.0),  # target's snapshot wins
        ("2026-02-28", 1100.0),
    ]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM balance_snapshots WHERE account_id = ?", (src,)
    ).fetchone()["c"] == 0


def test_merge_moves_imports_and_column_mapping(conn):
    mapping = json.dumps({"columns": {"date": "Date"}})
    src = _make_account(conn, "Source", column_mapping=mapping)
    tgt = _make_account(conn, "Target")
    conn.execute("INSERT INTO imports (account_id, filename, row_count) VALUES (?, 'f.csv', 3)", (src,))
    conn.commit()

    account_ops.merge_accounts(conn, src, tgt)

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM imports WHERE account_id = ?", (tgt,)
    ).fetchone()["c"] == 1
    target = conn.execute("SELECT * FROM accounts WHERE id = ?", (tgt,)).fetchone()
    assert target["column_mapping"] == mapping  # inherited (target had none)


def test_merge_preview_counts_and_samples(conn):
    src = _make_account(conn, "Source")
    tgt = _make_account(conn, "Target")
    _add_txn(conn, tgt, "2026-01-05", -10.00, "COFFEE A")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE B")
    _add_txn(conn, src, "2026-01-06", -20.00, "LUNCH")
    _add_snapshot(conn, src, "2026-01-31", 500.0)

    preview = account_ops.merge_preview(conn, src, tgt)

    assert preview["moving"] == 1
    assert preview["overlaps"] == 1
    assert preview["snapshots_moving"] == 1
    assert preview["sample_overlaps"] == [{
        "date": "2026-01-05", "amount": -10.0,
        "desc_source": "COFFEE B", "desc_target": "COFFEE A",
    }]
    # Preview is a dry run: nothing changed
    assert len(_txns(conn, src)) == 2 and len(_txns(conn, tgt)) == 1


# --- Delete ---


def test_delete_account_cascades_with_counts(conn):
    acct = _make_account(conn, "Doomed")
    _add_txn(conn, acct, "2026-01-05", -10.00, "A")
    _add_txn(conn, acct, "2026-01-06", -20.00, "B")
    _add_snapshot(conn, acct, "2026-01-31", 100.0)
    conn.execute("INSERT INTO imports (account_id, filename, row_count) VALUES (?, 'f.csv', 2)", (acct,))
    conn.commit()

    result = account_ops.delete_account(conn, acct, remove_remote=lambda tok: True)

    assert result["deleted"] == {"transactions": 2, "snapshots": 1, "imports": 1}
    assert result["unlinked_item"] is None
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
    assert db.count_transactions(conn) == 0


def test_delete_last_plaid_account_removes_item(conn):
    _make_plaid_item(conn, item_id="item-1", access_token="tok-1")
    acct = _make_account(conn, "Only Linked", source="plaid",
                         plaid_account_id="pa-1", plaid_item_id="item-1")
    removed_tokens = []

    result = account_ops.delete_account(conn, acct, remove_remote=removed_tokens.append)

    assert result["unlinked_item"] == {"item_id": "item-1", "institution_name": "Test Bank"}
    assert conn.execute("SELECT COUNT(*) AS c FROM plaid_items").fetchone()["c"] == 0
    assert removed_tokens == ["tok-1"]


def test_delete_plaid_account_keeps_item_with_siblings(conn):
    _make_plaid_item(conn, item_id="item-1")
    a = _make_account(conn, "Linked A", source="plaid",
                      plaid_account_id="pa-1", plaid_item_id="item-1")
    _make_account(conn, "Linked B", source="plaid",
                  plaid_account_id="pa-2", plaid_item_id="item-1")

    result = account_ops.delete_account(conn, a, remove_remote=lambda tok: True)

    assert result["unlinked_item"] is None
    assert conn.execute("SELECT COUNT(*) AS c FROM plaid_items").fetchone()["c"] == 1


# --- Unlink ---


def test_unlink_keep_data_detaches_accounts(conn):
    _make_plaid_item(conn, item_id="item-1", access_token="tok-1")
    a = _make_account(conn, "Linked A", source="plaid",
                      plaid_account_id="pa-1", plaid_item_id="item-1")
    _add_txn(conn, a, "2026-01-05", -10.00, "KEEP ME", source="plaid")
    removed_tokens = []

    result = account_ops.unlink_item(conn, "item-1", keep_data=True,
                                     remove_remote=removed_tokens.append)

    assert result["keep_data"] is True and result["accounts"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM plaid_items").fetchone()["c"] == 0
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (a,)).fetchone()
    assert row["plaid_account_id"] is None and row["plaid_item_id"] is None
    assert row["source"] == "csv"
    assert db.count_transactions(conn, a) == 1
    assert removed_tokens == ["tok-1"]


def test_unlink_without_keep_data_deletes_accounts(conn):
    _make_plaid_item(conn, item_id="item-1", access_token="tok-1")
    a = _make_account(conn, "Linked A", source="plaid",
                      plaid_account_id="pa-1", plaid_item_id="item-1")
    _add_txn(conn, a, "2026-01-05", -10.00, "GONE", source="plaid")
    _add_snapshot(conn, a, "2026-01-31", 100.0, source="plaid")

    result = account_ops.unlink_item(conn, "item-1", keep_data=False,
                                     remove_remote=lambda tok: True)

    assert result["deleted"] == {"transactions": 1, "snapshots": 1, "imports": 0}
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM plaid_items").fetchone()["c"] == 0


# --- Hidden accounts / net-worth exclusion ---


def test_hidden_account_filtered_from_dataframe_loaders(conn):
    shown = _make_account(conn, "Shown")
    hidden = _make_account(conn, "Hidden")
    _add_txn(conn, shown, "2026-01-05", -10.00, "VISIBLE TXN")
    _add_txn(conn, hidden, "2026-01-05", -20.00, "HIDDEN TXN")
    _add_snapshot(conn, shown, "2026-01-31", 100.0)
    _add_snapshot(conn, hidden, "2026-01-31", 200.0)
    conn.execute("UPDATE accounts SET hidden = 1 WHERE id = ?", (hidden,))
    conn.commit()

    df = db.load_transactions_df(conn)
    assert set(df["account_name"]) == {"Shown"}

    snaps = db.load_balance_snapshots_df(conn, source="manual")
    assert set(snaps["account_name"]) == {"Shown"}

    # Importing into a hidden account still works (it's only filtered from analytics)
    _add_txn(conn, hidden, "2026-01-06", -5.00, "STILL IMPORTABLE")
    assert db.count_transactions(conn, hidden) == 2


def test_exclude_from_net_worth_respected_in_series(conn):
    dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
    daily = pd.DataFrame({
        "date": list(dates) * 2,
        "account_name": ["Counted", "Counted", "Excluded", "Excluded"],
        "account_type": ["checking"] * 4,
        "balance": [100.0, 110.0, 1000.0, 1000.0],
    })

    full = compute_net_worth_series(daily)
    filtered = compute_net_worth_series(daily, excluded_accounts={"Excluded"})

    assert list(full["net_worth"]) == [1100.0, 1110.0]
    assert list(filtered["net_worth"]) == [100.0, 110.0]
    assert "Excluded" not in filtered.columns
    assert list(filtered["assets_total"]) == [100.0, 110.0]

    assert db.excluded_net_worth_account_names(conn) == set()
    _make_account(conn, "Excluded")
    conn.execute("UPDATE accounts SET exclude_from_net_worth = 1 WHERE name = 'Excluded'")
    conn.commit()
    assert db.excluded_net_worth_account_names(conn) == {"Excluded"}


# --- HTTP endpoints ---


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    return tmp_path


@pytest.fixture
def app(data_dir, monkeypatch):
    """Accounts + Plaid blueprints wired to a tmp_path sandbox (as in test_upload)."""
    def _get_db_connection():
        connection = db.get_connection(db.get_db_path(str(data_dir)))
        db.init_db(connection)
        return connection

    for module in (accounts_module, plaid_module):
        monkeypatch.setattr(module, "get_db_connection", _get_db_connection)
        monkeypatch.setattr(module, "refresh_data", lambda: {})
    monkeypatch.setattr(accounts_module, "get_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(accounts_module, "get_config_path", lambda: str(data_dir / "config.yaml"))
    # Never touch the real Plaid API from tests
    monkeypatch.setattr(plaid_sync, "remove_item", lambda token, client=None: True)

    flask_app = Flask(
        __name__,
        template_folder=os.path.join(REPO_ROOT, "templates"),
        static_folder=os.path.join(REPO_ROOT, "static"),
    )

    @flask_app.template_filter("currency")
    def currency_filter(value):
        if value is None:
            return "N/A"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"

    flask_app.register_blueprint(accounts_module.accounts_bp)
    flask_app.register_blueprint(plaid_module.plaid_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _connect(data_dir):
    connection = db.get_connection(db.get_db_path(str(data_dir)))
    db.init_db(connection)
    return connection


def test_patch_account_rename_and_flags(client, data_dir):
    conn = _connect(data_dir)
    acct = _make_account(conn, "Old Name")
    conn.close()

    resp = client.patch(f"/api/accounts/{acct}", json={
        "name": "New Name", "type": "savings", "institution": "My Bank",
        "hidden": True, "exclude_from_net_worth": True,
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "New Name"
    assert body["type"] == "savings"
    assert body["institution"] == "My Bank"
    assert body["hidden"] is True
    assert body["exclude_from_net_worth"] is True

    conn = _connect(data_dir)
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (acct,)).fetchone()
    conn.close()
    assert row["name"] == "New Name" and row["hidden"] == 1


def test_patch_account_validation_errors(client, data_dir):
    conn = _connect(data_dir)
    a = _make_account(conn, "Account A")
    _make_account(conn, "Account B")
    conn.close()

    assert client.patch(f"/api/accounts/{a}", json={"name": ""}).status_code == 400
    assert client.patch(f"/api/accounts/{a}", json={"name": "Account B"}).status_code == 400
    assert client.patch(f"/api/accounts/{a}", json={"type": "bogus"}).status_code == 400
    assert client.patch(f"/api/accounts/{a}", json={"hidden": "yes"}).status_code == 400
    assert client.patch("/api/accounts/999", json={"name": "X"}).status_code == 404
    assert client.patch(f"/api/accounts/{a}", data="{}",
                        content_type="application/json").status_code == 400


def test_merge_endpoints(client, data_dir):
    conn = _connect(data_dir)
    src = _make_account(conn, "Merge Src")
    tgt = _make_account(conn, "Merge Tgt")
    _add_txn(conn, tgt, "2026-01-05", -10.00, "COFFEE A")
    _add_txn(conn, src, "2026-01-05", -10.00, "COFFEE B")
    _add_txn(conn, src, "2026-01-06", -20.00, "LUNCH")
    conn.close()

    preview = client.post(f"/api/accounts/{src}/merge-preview", json={"target_id": tgt})
    assert preview.status_code == 200
    pbody = preview.get_json()
    assert pbody["moving"] == 1 and pbody["overlaps"] == 1
    assert len(pbody["sample_overlaps"]) == 1

    assert client.post(f"/api/accounts/{src}/merge-preview", json={}).status_code == 400
    assert client.post(f"/api/accounts/{src}/merge-preview",
                       json={"target_id": src}).status_code == 400
    assert client.post(f"/api/accounts/{src}/merge-preview",
                       json={"target_id": 999}).status_code == 404

    merged = client.post(f"/api/accounts/{src}/merge", json={"target_id": tgt})
    assert merged.status_code == 200
    assert merged.get_json() == {"moved": 1, "duplicates_skipped": 1, "snapshots_moved": 0}

    conn = _connect(data_dir)
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 1
    conn.close()


def test_delete_endpoint_requires_confirm(client, data_dir):
    conn = _connect(data_dir)
    acct = _make_account(conn, "Doomed")
    _add_txn(conn, acct, "2026-01-05", -10.00, "A")
    conn.close()

    assert client.delete(f"/api/accounts/{acct}", json={}).status_code == 400
    assert client.delete(f"/api/accounts/{acct}", json={"confirm": "yes"}).status_code == 400

    resp = client.delete(f"/api/accounts/{acct}", json={"confirm": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["deleted"] == {"transactions": 1, "snapshots": 0, "imports": 0}
    assert client.delete(f"/api/accounts/{acct}", json={"confirm": True}).status_code == 404


def test_unlink_endpoint_both_paths(client, data_dir):
    conn = _connect(data_dir)
    _make_plaid_item(conn, item_id="item-keep", access_token="tok-1")
    keep_acct = _make_account(conn, "Keep Me", source="plaid",
                              plaid_account_id="pa-1", plaid_item_id="item-keep")
    _add_txn(conn, keep_acct, "2026-01-05", -10.00, "KEEP", source="plaid")
    _make_plaid_item(conn, item_id="item-del", access_token="tok-2",
                     institution_name="Del Bank")
    del_acct = _make_account(conn, "Delete Me", source="plaid",
                             plaid_account_id="pa-2", plaid_item_id="item-del")
    _add_txn(conn, del_acct, "2026-01-05", -10.00, "GONE", source="plaid")
    conn.close()

    resp = client.delete("/api/plaid/items/item-keep", json={"keep_data": True})
    assert resp.status_code == 200
    assert resp.get_json()["keep_data"] is True

    resp = client.delete("/api/plaid/items/item-del", json={"keep_data": False})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"]["transactions"] == 1

    assert client.delete("/api/plaid/items/nope", json={}).status_code == 404
    assert client.delete("/api/plaid/items/x", json={"keep_data": "y"}).status_code == 400

    conn = _connect(data_dir)
    assert conn.execute("SELECT COUNT(*) AS c FROM plaid_items").fetchone()["c"] == 0
    kept = conn.execute("SELECT * FROM accounts WHERE id = ?", (keep_acct,)).fetchone()
    assert kept is not None and kept["plaid_item_id"] is None
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE id = ?", (del_acct,)).fetchone()["c"] == 0
    assert db.count_transactions(conn, keep_acct) == 1
    conn.close()


def test_accounts_page_shows_hidden_section_and_menus(client, data_dir):
    conn = _connect(data_dir)
    _make_account(conn, "Visible Account")
    hidden = _make_account(conn, "Hidden Account")
    conn.execute("UPDATE accounts SET hidden = 1 WHERE id = ?", (hidden,))
    conn.commit()
    conn.close()

    resp = client.get("/accounts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Visible Account" in html
    assert "Hidden accounts (1)" in html
    assert "Hidden Account" in html
    assert "account-menu-btn" in html
    assert "accounts_manage.js" in html
