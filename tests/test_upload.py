from __future__ import annotations

import io
import json
import os

import pytest
from flask import Flask

import finance.blueprints.accounts as accounts_module
from finance import db

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


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    return tmp_path


@pytest.fixture
def app(data_dir, monkeypatch):
    """
    A minimal Flask app wired to only accounts_bp, with the module's
    data_dir/db/config/refresh hooks monkeypatched to a tmp_path sandbox —
    mirrors how finance/data_service.py wires the real app, without
    depending on other blueprints that sibling wave agents may be editing.
    """
    monkeypatch.setattr(accounts_module, "get_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(accounts_module, "get_config_path", lambda: str(data_dir / "config.yaml"))
    monkeypatch.setattr(accounts_module, "refresh_data", lambda: {})

    def _get_db_connection():
        connection = db.get_connection(db.get_db_path(str(data_dir)))
        db.init_db(connection)
        return connection

    monkeypatch.setattr(accounts_module, "get_db_connection", _get_db_connection)

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
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _connect(data_dir):
    connection = db.get_connection(db.get_db_path(str(data_dir)))
    db.init_db(connection)
    return connection


def _make_mapped_account(data_dir, name="Test Checking", account_type="checking"):
    conn = _connect(data_dir)
    mapping = json.dumps({
        "columns": {"date": "Date", "description": "Description", "amount": "Amount",
                    "debit": None, "credit": None},
        "date_format": "%m/%d/%Y",
        "amount_sign": "standard",
        "balance_column": None,
    })
    account_id = db.upsert_account(
        conn, name=name, account_type=account_type, source="csv", column_mapping=mapping
    )
    conn.commit()
    conn.close()
    return account_id


def _csv_bytes():
    return (
        "Date,Description,Amount\n"
        "01/05/2026,COFFEE SHOP,-4.50\n"
        "01/06/2026,PAYCHECK,1500.00\n"
    ).encode("utf-8")


# --- Upload to an existing account ---


def test_upload_to_existing_account_dedup(client, data_dir):
    account_id = _make_mapped_account(data_dir)

    resp1 = client.post(
        f"/api/accounts/{account_id}/upload",
        data={"file": (io.BytesIO(_csv_bytes()), "test.csv")},
        content_type="multipart/form-data",
    )
    assert resp1.status_code == 200
    body1 = resp1.get_json()
    assert body1 == {"imported": 2, "duplicates": 0, "errors": []}

    # Same file again -> everything is a duplicate, nothing new inserted
    resp2 = client.post(
        f"/api/accounts/{account_id}/upload",
        data={"file": (io.BytesIO(_csv_bytes()), "test.csv")},
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 200
    body2 = resp2.get_json()
    assert body2 == {"imported": 0, "duplicates": 2, "errors": []}

    conn = _connect(data_dir)
    assert db.count_transactions(conn, account_id) == 2
    # One imports audit row per upload call
    assert conn.execute("SELECT COUNT(*) AS c FROM imports").fetchone()["c"] == 2
    conn.close()

    # A raw copy of the uploaded file was saved for audit
    uploads = os.listdir(os.path.join(str(data_dir), "uploads"))
    assert any(name.endswith("_test.csv") for name in uploads)


def test_upload_to_missing_account_404(client, data_dir):
    resp = client.post(
        "/api/accounts/999/upload",
        data={"file": (io.BytesIO(_csv_bytes()), "test.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 404


def test_upload_to_account_without_mapping_400(client, data_dir):
    conn = _connect(data_dir)
    account_id = db.upsert_account(conn, name="No Mapping", account_type="checking", source="csv")
    conn.commit()
    conn.close()

    resp = client.post(
        f"/api/accounts/{account_id}/upload",
        data={"file": (io.BytesIO(_csv_bytes()), "test.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_upload_no_file_returns_400(client, data_dir):
    account_id = _make_mapped_account(data_dir)
    resp = client.post(f"/api/accounts/{account_id}/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_upload_malformed_csv_returns_400_and_inserts_nothing(client, data_dir):
    account_id = _make_mapped_account(data_dir)
    bad_csv = b"Date,Description,TotallyWrongColumn\n01/05/2026,ITEM,5.00\n"

    resp = client.post(
        f"/api/accounts/{account_id}/upload",
        data={"file": (io.BytesIO(bad_csv), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "error" in body
    assert body["errors"]

    conn = _connect(data_dir)
    assert db.count_transactions(conn, account_id) == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM imports").fetchone()["c"] == 0
    conn.close()


def test_upload_empty_file_returns_400(client, data_dir):
    account_id = _make_mapped_account(data_dir)
    resp = client.post(
        f"/api/accounts/{account_id}/upload",
        data={"file": (io.BytesIO(b""), "empty.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400

    conn = _connect(data_dir)
    assert db.count_transactions(conn, account_id) == 0
    conn.close()


# --- New account from CSV ---


def test_create_account_from_csv_and_import(client, data_dir):
    resp = client.post(
        "/api/accounts",
        data={
            "file": (io.BytesIO(_csv_bytes()), "new.csv"),
            "name": "New Checking",
            "type": "checking",
            "date_col": "Date",
            "description_col": "Description",
            "amount_col": "Amount",
            "date_format": "%m/%d/%Y",
            "amount_sign": "standard",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["imported"] == 2
    assert body["duplicates"] == 0
    assert body["name"] == "New Checking"
    account_id = body["account_id"]

    conn = _connect(data_dir)
    account = db.get_account_by_name(conn, "New Checking")
    assert account is not None
    assert account["source"] == "csv"
    mapping = json.loads(account["column_mapping"])
    assert mapping["columns"]["date"] == "Date"
    assert mapping["columns"]["amount"] == "Amount"
    assert db.count_transactions(conn, account_id) == 2
    conn.close()


def test_create_account_with_debit_credit_columns(client, data_dir):
    csv_bytes = (
        "Date,Description,Debit,Credit\n"
        "01/05/2026,GROCERY STORE,52.30,\n"
        "01/06/2026,DEPOSIT,,250.00\n"
    ).encode("utf-8")

    resp = client.post(
        "/api/accounts",
        data={
            "file": (io.BytesIO(csv_bytes), "dc.csv"),
            "name": "DC Account",
            "type": "checking",
            "date_col": "Date",
            "description_col": "Description",
            "debit_col": "Debit",
            "credit_col": "Credit",
            "date_format": "%m/%d/%Y",
            "amount_sign": "standard",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["imported"] == 2


def test_create_account_duplicate_name_400(client, data_dir):
    _make_mapped_account(data_dir, name="Dup Account")
    resp = client.post(
        "/api/accounts",
        data={
            "file": (io.BytesIO(_csv_bytes()), "new.csv"),
            "name": "Dup Account",
            "type": "checking",
            "date_col": "Date",
            "description_col": "Description",
            "amount_col": "Amount",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_create_account_missing_fields_400(client, data_dir):
    resp = client.post(
        "/api/accounts",
        data={
            "file": (io.BytesIO(_csv_bytes()), "new.csv"),
            "name": "",
            "type": "checking",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"]

    # Nothing should have been created
    conn = _connect(data_dir)
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
    conn.close()


def test_create_account_malformed_csv_creates_no_account(client, data_dir):
    bad_csv = b"Date,Description,TotallyWrongColumn\n01/05/2026,ITEM,5.00\n"
    resp = client.post(
        "/api/accounts",
        data={
            "file": (io.BytesIO(bad_csv), "bad.csv"),
            "name": "Should Not Exist",
            "type": "checking",
            "date_col": "Date",
            "description_col": "Description",
            "amount_col": "Amount",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400

    conn = _connect(data_dir)
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
    conn.close()


# --- Preview endpoint ---


def test_preview_endpoint_detects_columns(client, data_dir):
    resp = client.post(
        "/api/accounts/preview",
        data={"file": (io.BytesIO(_csv_bytes()), "preview.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["columns"] == ["Date", "Description", "Amount"]
    assert len(body["rows"]) == 2
    assert body["filename"] == "preview.csv"
    assert body["detected_mapping"]["date"] == "Date"
    assert body["detected_mapping"]["description"] == "Description"
    assert body["detected_mapping"]["amount"] == "Amount"
    assert body["detected_date_format"] == "%m/%d/%Y"


def test_preview_endpoint_detects_debit_credit(client, data_dir):
    csv_bytes = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/05/2026,GROCERY STORE,52.30,,999.00\n"
    ).encode("utf-8")
    resp = client.post(
        "/api/accounts/preview",
        data={"file": (io.BytesIO(csv_bytes), "dc.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["detected_mapping"]["debit"] == "Debit"
    assert body["detected_mapping"]["credit"] == "Credit"
    assert body["detected_mapping"]["balance"] == "Balance"
    assert body["detected_mapping"]["amount"] is None


def test_preview_no_file_400(client, data_dir):
    resp = client.post("/api/accounts/preview", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


# --- Recent imports list ---


def test_recent_imports_list(client, data_dir):
    account_id = _make_mapped_account(data_dir)
    client.post(
        f"/api/accounts/{account_id}/upload",
        data={"file": (io.BytesIO(_csv_bytes()), "test.csv")},
        content_type="multipart/form-data",
    )

    resp = client.get(f"/api/accounts/{account_id}/imports")
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["filename"] == "test.csv"
    assert body[0]["row_count"] == 2
    assert body[0]["duplicate_count"] == 0


def test_accounts_page_marks_upload_capable_accounts(client, data_dir):
    _make_mapped_account(data_dir, name="Mapped Account")
    conn = _connect(data_dir)
    db.upsert_account(conn, name="Unmapped Account", account_type="checking", source="csv")
    conn.commit()
    conn.close()

    resp = client.get("/accounts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Mapped Account" in html
    assert "Unmapped Account" in html
