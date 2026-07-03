from __future__ import annotations

import os
import re
from datetime import date

import pandas as pd
import pytest
from flask import Flask

import finance.blueprints.rules as rules_module
import finance.blueprints.transactions as transactions_module
from finance import db, importer
from finance.blueprints.transactions import _normalize_uncategorized
from finance.config_loader import AppConfig, ClassificationConfig, PayPeriodConfig

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- App/client fixtures (mirrors tests/test_categorization.py's monkeypatch pattern) ---


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


def _row(data_dir, description, amount=None):
    conn = _connect(data_dir)
    if amount is None:
        row = conn.execute("SELECT * FROM transactions WHERE description = ?", (description,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM transactions WHERE description = ? AND amount = ?", (description, amount)
        ).fetchone()
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


# --- Fixtures for /api/transactions/similar ---


@pytest.fixture
def dining_seeded(data_dir, classification):
    """
    Two uncategorized Firebirds rows, one already-categorized (non-user_edited)
    Texas Roadhouse row, one user_edited Texas Roadhouse row, and a transfer
    row whose description also contains "firebirds" (to prove transfer
    exclusion).
    """
    conn = _connect(data_dir)
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    rows = _make_rows(
        [
            ("2026-02-01", "FIREBIRDS WOOD FIRED GRILL #12", -45.00),
            ("2026-02-03", "FIREBIRDS WOOD FIRED GRILL #12", -38.50),
            ("2026-02-05", "TEXAS ROADHOUSE 0221", -60.00),
            ("2026-02-06", "TEXAS ROADHOUSE 0221", -55.00),
            ("2026-02-07", "FIREBIRDS TRANSFER TO SAVINGS", -100.00),
        ]
    )
    importer.import_rows(conn, account_id, rows, classification)
    conn.execute(
        "UPDATE transactions SET category = 'Dining Out' "
        "WHERE description = 'TEXAS ROADHOUSE 0221' AND amount = -60.00"
    )
    conn.execute(
        "UPDATE transactions SET category = 'Kept As Is', user_edited = 1 "
        "WHERE description = 'TEXAS ROADHOUSE 0221' AND amount = -55.00"
    )
    conn.commit()
    conn.close()
    return account_id


def test_similar_counts_uncategorized_matches(client, dining_seeded):
    resp = client.get("/api/transactions/similar?keyword=firebirds")
    assert resp.status_code == 200
    body = resp.get_json()

    # The two uncategorized "FIREBIRDS WOOD FIRED GRILL" rows match; the
    # transfer row ("FIREBIRDS TRANSFER TO SAVINGS") is excluded entirely.
    assert body["total"] == 2
    assert body["uncategorized"] == 2
    assert body["categorized"] == 0
    assert body["user_edited"] == 0
    assert len(body["samples"]) == 2
    for sample in body["samples"]:
        assert set(sample.keys()) == {"date", "description", "amount"}


def test_similar_splits_categorized_and_user_edited(client, dining_seeded):
    resp = client.get("/api/transactions/similar?keyword=texas roadhouse")
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["total"] == 2
    assert body["uncategorized"] == 0
    assert body["categorized"] == 2
    assert body["user_edited"] == 1  # the "Kept As Is" row is still counted (informational)


def test_similar_excludes_transfers(client, dining_seeded):
    resp = client.get("/api/transactions/similar?keyword=firebirds")
    body = resp.get_json()
    descriptions = [s["description"] for s in body["samples"]]
    assert "FIREBIRDS TRANSFER TO SAVINGS" not in descriptions
    assert body["total"] == 2


def test_similar_is_case_insensitive(client, dining_seeded):
    resp = client.get("/api/transactions/similar?keyword=FiReBiRdS")
    body = resp.get_json()
    assert body["total"] == 2


def test_similar_requires_keyword(client, dining_seeded):
    resp = client.get("/api/transactions/similar?keyword=")
    assert resp.status_code == 400

    resp = client.get("/api/transactions/similar")
    assert resp.status_code == 400


def test_similar_no_matches_returns_zeroed_counts(client, dining_seeded):
    resp = client.get("/api/transactions/similar?keyword=nonexistentmerchant")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"total": 0, "uncategorized": 0, "categorized": 0, "user_edited": 0, "samples": []}


# --- Fixtures for bulk-category create_rule ---


@pytest.fixture
def bulk_rule_seeded(data_dir, classification):
    """
    Two selected+uncategorized Firebirds rows, one selected+uncategorized
    Texas Roadhouse row (a second distinct merchant keyword in the same
    selection), one NOT-selected uncategorized Firebirds row (to prove
    reapply_rules picks it up), and one NOT-selected user_edited Texas
    Roadhouse row (to prove it stays protected).
    """
    conn = _connect(data_dir)
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    rows = _make_rows(
        [
            ("2026-03-01", "FIREBIRDS WOOD FIRED GRILL #1", -45.00),
            ("2026-03-02", "FIREBIRDS WOOD FIRED GRILL #2", -38.50),
            ("2026-03-03", "FIREBIRDS WOOD FIRED GRILL #3", -20.00),  # not selected
            ("2026-03-04", "TEXAS ROADHOUSE 0221", -60.00),
            ("2026-03-05", "TEXAS ROADHOUSE 0221", -55.00),  # not selected, user_edited
        ]
    )
    importer.import_rows(conn, account_id, rows, classification)
    conn.execute(
        "UPDATE transactions SET category = 'Kept As Is', user_edited = 1 "
        "WHERE description = 'TEXAS ROADHOUSE 0221' AND amount = -55.00"
    )
    conn.commit()
    conn.close()
    return account_id


def _selected_ids(data_dir):
    row_a = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #1", -45.00)
    row_b = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #2", -38.50)
    row_d = _row(data_dir, "TEXAS ROADHOUSE 0221", -60.00)
    return [row_a["id"], row_b["id"], row_d["id"]]


def test_bulk_category_create_rule_creates_one_rule_per_distinct_keyword(client, data_dir, bulk_rule_seeded):
    ids = _selected_ids(data_dir)

    resp = client.post(
        "/api/transactions/bulk-category",
        json={"ids": ids, "category": "Dining Out", "create_rule": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["updated"] == 3

    assert sorted(body["rules_created"]) == ["firebirds wood fired grill", "texas roadhouse"]
    assert body["rules_skipped"] == []
    assert body["reapplied"] >= 1

    # Selected rows: direct category + user_edited=1
    for desc, amount in [
        ("FIREBIRDS WOOD FIRED GRILL #1", -45.00),
        ("FIREBIRDS WOOD FIRED GRILL #2", -38.50),
        ("TEXAS ROADHOUSE 0221", -60.00),
    ]:
        row = _row(data_dir, desc, amount)
        assert row["category"] == "Dining Out"
        assert row["user_edited"] == 1

    # Non-selected uncategorized Firebirds row picked up by reapply_rules
    unselected = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #3", -20.00)
    assert unselected["category"] == "Dining Out"
    assert unselected["user_edited"] == 0

    # user_edited row stays protected, even though it matches the new rule
    protected = _row(data_dir, "TEXAS ROADHOUSE 0221", -55.00)
    assert protected["category"] == "Kept As Is"
    assert protected["user_edited"] == 1

    # Rules actually exist in the DB
    conn = _connect(data_dir)
    rules = {r["keyword"]: r["category"] for r in conn.execute("SELECT * FROM categorization_rules").fetchall()}
    conn.close()
    assert rules["firebirds wood fired grill"] == "Dining Out"
    assert rules["texas roadhouse"] == "Dining Out"


def test_bulk_category_create_rule_skips_existing_keyword_conflict(client, data_dir, bulk_rule_seeded):
    conn = _connect(data_dir)
    conn.execute(
        "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Old Category', 'firebirds wood fired grill', 0)"
    )
    conn.commit()
    conn.close()

    row_a = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #1", -45.00)
    row_b = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #2", -38.50)

    resp = client.post(
        "/api/transactions/bulk-category",
        json={"ids": [row_a["id"], row_b["id"]], "category": "Dining Out", "create_rule": True},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rules_created"] == []
    assert body["rules_skipped"] == ["firebirds wood fired grill"]

    # The direct category set on the selected rows still happens, even though
    # the rule for their keyword was skipped.
    updated_a = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #1", -45.00)
    assert updated_a["category"] == "Dining Out"
    assert updated_a["user_edited"] == 1

    # The pre-existing rule under 'Old Category' was never stacked/overwritten
    conn = _connect(data_dir)
    rule = conn.execute(
        "SELECT category FROM categorization_rules WHERE keyword = 'firebirds wood fired grill'"
    ).fetchone()
    conn.close()
    assert rule["category"] == "Old Category"


def test_bulk_category_create_rule_false_regression(client, data_dir, bulk_rule_seeded):
    ids = _selected_ids(data_dir)

    resp = client.post(
        "/api/transactions/bulk-category",
        json={"ids": ids, "category": "Dining Out"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["updated"] == 3
    # No rule-related keys at all when create_rule is omitted/false
    assert "rules_created" not in body
    assert "rules_skipped" not in body
    assert "reapplied" not in body

    # No rules were created, and non-selected rows are unaffected
    conn = _connect(data_dir)
    rule_count = conn.execute("SELECT COUNT(*) AS n FROM categorization_rules").fetchone()["n"]
    conn.close()
    assert rule_count == 0

    unselected = _row(data_dir, "FIREBIRDS WOOD FIRED GRILL #3", -20.00)
    assert unselected["category"] is None
    assert unselected["user_edited"] == 0


def test_bulk_category_create_rule_explicit_false_same_as_regression(client, data_dir, bulk_rule_seeded):
    ids = _selected_ids(data_dir)
    resp = client.post(
        "/api/transactions/bulk-category",
        json={"ids": ids, "category": "Dining Out", "create_rule": False},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "rules_created" not in body


# --- nan-render fix: server-side normalization helper ---


def test_normalize_uncategorized_coerces_nan_subcategory_to_none():
    rows = [
        {"subcategory": float("nan")},
        {"subcategory": "Coffee"},
        {"subcategory": None},
        {"subcategory": ""},
    ]
    _normalize_uncategorized(rows)
    assert rows[0]["subcategory"] is None
    assert rows[1]["subcategory"] == "Coffee"
    assert rows[2]["subcategory"] is None
    assert rows[3]["subcategory"] == ""


def test_transactions_route_renders_uncategorized_not_literal_nan(monkeypatch, data_dir):
    """
    End-to-end proof at the render layer: a DataFrame row with a NaN
    subcategory (the shape load_transactions_df/to_dict actually produces)
    must never render the literal text "nan" -- it should render "Uncategorized".
    """
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-02-01"]),
            "description": ["MYSTERY MERCHANT 123"],
            "amount": [-10.0],
            "account_name": ["Test Checking"],
            "account_type": ["checking"],
            "raw_balance": [float("nan")],
            "category": ["expense"],
            "subcategory": [float("nan")],
        }
    )

    monkeypatch.setattr(transactions_module, "get_cache", lambda: {"df": df})
    monkeypatch.setattr(
        transactions_module,
        "load_config",
        lambda path: AppConfig(
            pay_period=PayPeriodConfig(start_date=date(2026, 1, 5), frequency_days=14),
            accounts=[],
            classification=ClassificationConfig(income_keywords=[], expense_keywords=[], transfer_keywords=[]),
            categorization_rules=[],
        ),
    )

    def _get_db_connection():
        connection = db.get_connection(db.get_db_path(str(data_dir)))
        db.init_db(connection)
        return connection

    monkeypatch.setattr(transactions_module, "get_db_connection", _get_db_connection)

    flask_app = Flask(
        __name__,
        template_folder=os.path.join(REPO_ROOT, "templates"),
        static_folder=os.path.join(REPO_ROOT, "static"),
    )
    flask_app.template_filter("normalize_merchant")(rules_module.normalize_merchant)
    flask_app.template_filter("currency")(lambda v: "N/A" if v is None else f"${v:,.2f}")
    flask_app.template_filter("to_short_date")(lambda v: v or "")
    flask_app.register_blueprint(transactions_module.transactions_bp)
    client = flask_app.test_client()

    resp = client.get("/transactions")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # No element should ever render the bare literal text "nan"
    assert re.search(r">\s*nan\s*<", html, re.IGNORECASE) is None
    assert "Uncategorized" in html
