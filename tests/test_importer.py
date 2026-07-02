from __future__ import annotations

from finance import db, importer


def _make_account(conn) -> int:
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    return account_id


def test_import_inserts_rows(conn, classification, sample_rows):
    account_id = _make_account(conn)
    result = importer.import_rows(conn, account_id, sample_rows, classification, filename="test.csv")
    assert result.imported == 3
    assert result.duplicates == 0
    assert db.count_transactions(conn) == 3


def test_import_same_rows_twice_inserts_zero(conn, classification, sample_rows):
    account_id = _make_account(conn)
    first = importer.import_rows(conn, account_id, sample_rows, classification, filename="test.csv")
    second = importer.import_rows(conn, account_id, sample_rows, classification, filename="test.csv")

    assert first.imported == 3
    assert second.imported == 0
    assert second.duplicates == 3
    assert db.count_transactions(conn) == 3


def test_dedup_hash_normalizes_description_whitespace_and_case(conn):
    h1 = importer.compute_dedup_hash(1, "2026-01-05", -52.3, "WALMART   GROCERY")
    h2 = importer.compute_dedup_hash(1, "2026-01-05", -52.30, "walmart grocery")
    assert h1 == h2
    h3 = importer.compute_dedup_hash(2, "2026-01-05", -52.3, "walmart grocery")
    assert h1 != h3


def test_import_classifies_txn_type(conn, classification, sample_rows):
    account_id = _make_account(conn)
    importer.import_rows(conn, account_id, sample_rows, classification)

    rows = {
        row["description"]: row["txn_type"]
        for row in conn.execute("SELECT description, txn_type FROM transactions").fetchall()
    }
    assert rows["WALMART GROCERY"] == "expense"
    assert rows["ACME CORP PAYROLL"] == "income"
    assert rows["Xfer To Savings"] == "transfer"


def test_import_applies_rules_and_auto_transfer_category(conn, classification, sample_rows):
    with conn:
        conn.execute(
            "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Groceries', 'walmart', 0)"
        )
    account_id = _make_account(conn)
    importer.import_rows(conn, account_id, sample_rows, classification)

    rows = {
        row["description"]: row["category"]
        for row in conn.execute("SELECT description, category FROM transactions").fetchall()
    }
    assert rows["WALMART GROCERY"] == "Groceries"
    assert rows["Xfer To Savings"] == "Transfer"  # auto-category for unmatched transfers
    assert rows["ACME CORP PAYROLL"] is None


def test_imports_audit_row_recorded(conn, classification, sample_rows):
    account_id = _make_account(conn)
    importer.import_rows(conn, account_id, sample_rows, classification, filename="test.csv")

    audit = conn.execute("SELECT * FROM imports").fetchall()
    assert len(audit) == 1
    assert audit[0]["filename"] == "test.csv"
    assert audit[0]["row_count"] == 3
    assert audit[0]["duplicate_count"] == 0
