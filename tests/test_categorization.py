from __future__ import annotations

import os

import pandas as pd
import pytest
from flask import Flask

import finance.blueprints.rules as rules_module
import finance.blueprints.transactions as transactions_module
from finance import db, importer
from finance.blueprints.rules import normalize_merchant

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- App/client fixtures (mirrors tests/test_upload.py's monkeypatch pattern) ---


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def app(data_dir, monkeypatch):
    """
    Minimal Flask app wired to only transactions_bp + rules_bp, with each
    module's get_db_connection/refresh_data monkeypatched to a tmp_path
    sandbox DB. Never touches the real data/finance.db.
    """

    def _get_db_connection():
        connection = db.get_connection(db.get_db_path(str(data_dir)))
        db.init_db(connection)
        return connection

    for module in (transactions_module, rules_module):
        monkeypatch.setattr(module, "get_db_connection", _get_db_connection)
        monkeypatch.setattr(module, "refresh_data", lambda: {})

    flask_app = Flask(
        __name__,
        template_folder=os.path.join(REPO_ROOT, "templates"),
        static_folder=os.path.join(REPO_ROOT, "static"),
    )
    flask_app.register_blueprint(transactions_module.transactions_bp)
    flask_app.register_blueprint(rules_module.rules_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _connect(data_dir):
    connection = db.get_connection(db.get_db_path(str(data_dir)))
    db.init_db(connection)
    return connection


def _row(data_dir, description):
    conn = _connect(data_dir)
    row = conn.execute("SELECT * FROM transactions WHERE description = ?", (description,)).fetchone()
    conn.close()
    return row


def _make_rows(pairs):
    """pairs: list of (date_str, description, amount) -> standardized csv_reader rows."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime([p[0] for p in pairs]),
            "description": [p[1] for p in pairs],
            "amount": [p[2] for p in pairs],
            "account_name": ["Test Checking"] * len(pairs),
            "account_type": ["checking"] * len(pairs),
            "raw_balance": [None] * len(pairs),
        }
    )


@pytest.fixture
def seeded(data_dir, classification, sample_rows):
    """Seed the shared conftest sample_rows (Walmart/Payroll/Xfer) into the tmp DB."""
    conn = _connect(data_dir)
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    importer.import_rows(conn, account_id, sample_rows, classification)
    conn.close()
    return account_id


@pytest.fixture
def merchant_seeded(data_dir, classification):
    """
    Three Amazon-marketplace-style rows sharing a merchant, one of which is
    already user-edited to a different category (so tests can assert the
    review-queue/rule flows never clobber it).
    """
    conn = _connect(data_dir)
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    rows = _make_rows(
        [
            ("2026-02-01", "AMAZON MKTPL*2K3J7", -20.00),
            ("2026-02-02", "AMAZON MKTPL*9X1Y2", -15.00),
            ("2026-02-04", "AMAZON MKTPL*ZZZ99", -5.00),
        ]
    )
    importer.import_rows(conn, account_id, rows, classification)
    conn.execute(
        "UPDATE transactions SET category = 'Kept As Is', user_edited = 1 "
        "WHERE description = 'AMAZON MKTPL*ZZZ99'"
    )
    conn.commit()
    conn.close()
    return account_id


# --- Inline single-transaction category edit: /api/transactions/<id>/category ---


def test_set_category_marks_user_edited(client, data_dir, seeded):
    row = _row(data_dir, "WALMART GROCERY")
    resp = client.post(f"/api/transactions/{row['id']}/category", json={"category": "Groceries"})

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"

    updated = _row(data_dir, "WALMART GROCERY")
    assert updated["category"] == "Groceries"
    assert updated["user_edited"] == 1


def test_set_category_survives_reapply_rules(client, data_dir, seeded):
    row = _row(data_dir, "WALMART GROCERY")
    resp = client.post(f"/api/transactions/{row['id']}/category", json={"category": "My Custom Category"})
    assert resp.status_code == 200

    conn = _connect(data_dir)
    conn.execute(
        "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Groceries', 'walmart', 0)"
    )
    conn.commit()
    importer.reapply_rules(conn)
    conn.close()

    updated = _row(data_dir, "WALMART GROCERY")
    assert updated["category"] == "My Custom Category"  # not overwritten by the rule
    assert updated["user_edited"] == 1


def test_set_category_requires_nonempty_category(client, data_dir, seeded):
    row = _row(data_dir, "WALMART GROCERY")
    resp = client.post(f"/api/transactions/{row['id']}/category", json={"category": "   "})
    assert resp.status_code == 400


def test_set_category_404_for_unknown_id(client, data_dir, seeded):
    resp = client.post("/api/transactions/999999/category", json={"category": "Anything"})
    assert resp.status_code == 404


# --- Bulk category endpoint: /api/transactions/bulk-category ---


def test_bulk_category_updates_all_ids_and_marks_user_edited(client, data_dir, seeded):
    conn = _connect(data_dir)
    ids = [r["id"] for r in conn.execute("SELECT id FROM transactions").fetchall()]
    conn.close()
    assert len(ids) == 3

    resp = client.post("/api/transactions/bulk-category", json={"ids": ids, "category": "Reviewed"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["updated"] == len(ids)

    conn = _connect(data_dir)
    rows = conn.execute("SELECT category, user_edited FROM transactions").fetchall()
    conn.close()
    assert all(r["category"] == "Reviewed" for r in rows)
    assert all(r["user_edited"] == 1 for r in rows)


def test_bulk_category_only_updates_selected_ids(client, data_dir, seeded):
    conn = _connect(data_dir)
    target = conn.execute(
        "SELECT id FROM transactions WHERE description = 'WALMART GROCERY'"
    ).fetchone()
    other_before = conn.execute(
        "SELECT category FROM transactions WHERE description = 'ACME CORP PAYROLL'"
    ).fetchone()
    conn.close()

    resp = client.post("/api/transactions/bulk-category", json={"ids": [target["id"]], "category": "Groceries"})
    assert resp.status_code == 200
    assert resp.get_json()["updated"] == 1

    updated_target = _row(data_dir, "WALMART GROCERY")
    unaffected = _row(data_dir, "ACME CORP PAYROLL")
    assert updated_target["category"] == "Groceries"
    assert unaffected["category"] == other_before["category"]
    assert unaffected["user_edited"] == 0


def test_bulk_category_requires_ids_and_category(client, data_dir, seeded):
    assert client.post("/api/transactions/bulk-category", json={"ids": [], "category": "X"}).status_code == 400
    conn = _connect(data_dir)
    any_id = conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()["id"]
    conn.close()
    assert client.post("/api/transactions/bulk-category", json={"ids": [any_id], "category": ""}).status_code == 400


# --- Review queue: /api/uncategorized-groups + /api/categorize-group ---


def test_uncategorized_groups_excludes_user_edited(client, data_dir, merchant_seeded):
    resp = client.get("/api/uncategorized-groups")
    assert resp.status_code == 200
    groups = {g["keyword"]: g for g in resp.get_json()["groups"]}

    assert "amazon mktpl" in groups
    g = groups["amazon mktpl"]
    assert g["count"] == 2  # the user-edited "Kept As Is" row is excluded
    assert round(g["total_amount"], 2) == -35.00
    assert len(g["samples"]) == 2


def test_categorize_group_with_create_rule_adds_rule_and_applies(client, data_dir, merchant_seeded):
    resp = client.post(
        "/api/categorize-group",
        json={"keyword": "amazon mktpl", "category": "Shopping", "create_rule": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["updated"] >= 2

    conn = _connect(data_dir)
    rule = conn.execute(
        "SELECT * FROM categorization_rules WHERE category = 'Shopping' AND keyword = 'amazon mktpl'"
    ).fetchone()
    conn.close()
    assert rule is not None

    row1 = _row(data_dir, "AMAZON MKTPL*2K3J7")
    row2 = _row(data_dir, "AMAZON MKTPL*9X1Y2")
    row3 = _row(data_dir, "AMAZON MKTPL*ZZZ99")

    assert row1["category"] == "Shopping"
    assert row1["user_edited"] == 0
    assert row2["category"] == "Shopping"
    assert row2["user_edited"] == 0

    # user_edited row is never touched, regardless of keyword match
    assert row3["category"] == "Kept As Is"
    assert row3["user_edited"] == 1


def test_categorize_group_without_rule_only_bulk_updates_uncategorized(client, data_dir, classification):
    conn = _connect(data_dir)
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    rows = _make_rows(
        [
            ("2026-03-01", "TARGET STORE 4471", -30.00),
            ("2026-03-02", "TARGET STORE 9981", -12.50),
        ]
    )
    importer.import_rows(conn, account_id, rows, classification)
    # Pre-categorize one row directly so it's no longer "uncategorized"
    conn.execute("UPDATE transactions SET category = 'Shopping' WHERE description = 'TARGET STORE 4471'")
    conn.commit()
    conn.close()

    resp = client.post(
        "/api/categorize-group",
        json={"keyword": "target store", "category": "Groceries", "create_rule": False},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["updated"] == 1  # only the still-uncategorized row

    already_categorized = _row(data_dir, "TARGET STORE 4471")
    newly_categorized = _row(data_dir, "TARGET STORE 9981")

    assert already_categorized["category"] == "Shopping"
    assert already_categorized["user_edited"] == 0  # untouched by the direct bulk path

    assert newly_categorized["category"] == "Groceries"
    assert newly_categorized["user_edited"] == 1  # direct bulk (no rule) marks it user-edited

    conn = _connect(data_dir)
    rule = conn.execute("SELECT * FROM categorization_rules WHERE keyword = 'target store'").fetchone()
    conn.close()
    assert rule is None  # no standing rule was created


def test_categorize_group_requires_keyword_and_category(client, data_dir, merchant_seeded):
    resp = client.post("/api/categorize-group", json={"keyword": "", "category": "Shopping"})
    assert resp.status_code == 400
    resp = client.post("/api/categorize-group", json={"keyword": "amazon", "category": ""})
    assert resp.status_code == 400


# --- Merchant normalization ---


def test_normalize_merchant_strips_store_codes():
    assert normalize_merchant("AMAZON MKTPL*2K3J7") == "amazon mktpl"


def test_normalize_merchant_strips_numbers_and_dates():
    assert normalize_merchant("PURCHASE 01/15 TARGET STORE #4471") == "purchase target store"


def test_normalize_merchant_lowercases_and_collapses_whitespace():
    assert normalize_merchant("  Starbucks   Coffee  ") == "starbucks coffee"


def test_normalize_merchant_drops_short_tokens():
    assert normalize_merchant("CVS PHARM #12 IA") == "cvs pharm"


def test_normalize_merchant_empty_for_pure_noise():
    assert normalize_merchant("4471 01/02 00") == ""
