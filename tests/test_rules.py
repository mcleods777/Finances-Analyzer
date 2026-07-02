from __future__ import annotations

from finance import db, importer


def _seed(conn, classification, sample_rows) -> int:
    account_id = db.upsert_account(conn, "Test Checking", "checking")
    conn.commit()
    importer.import_rows(conn, account_id, sample_rows, classification)
    return account_id


def test_reapply_rules_updates_uncategorized_rows(conn, classification, sample_rows):
    _seed(conn, classification, sample_rows)

    with conn:
        conn.execute(
            "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Groceries', 'walmart', 0)"
        )
    updated = importer.reapply_rules(conn)

    assert updated == 1
    row = conn.execute(
        "SELECT category FROM transactions WHERE description = 'WALMART GROCERY'"
    ).fetchone()
    assert row["category"] == "Groceries"


def test_reapply_rules_respects_user_edited(conn, classification, sample_rows):
    _seed(conn, classification, sample_rows)

    with conn:
        # User manually set a category on the Walmart row
        conn.execute(
            "UPDATE transactions SET category = 'My Custom Category', user_edited = 1 "
            "WHERE description = 'WALMART GROCERY'"
        )
        conn.execute(
            "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Groceries', 'walmart', 0)"
        )

    importer.reapply_rules(conn)

    row = conn.execute(
        "SELECT category, user_edited FROM transactions WHERE description = 'WALMART GROCERY'"
    ).fetchone()
    assert row["user_edited"] == 1
    assert row["category"] == "My Custom Category"  # not overwritten


def test_reapply_rules_last_matching_rule_wins(conn, classification, sample_rows):
    _seed(conn, classification, sample_rows)

    with conn:
        conn.execute(
            "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Groceries', 'walmart', 0)"
        )
        conn.execute(
            "INSERT INTO categorization_rules (category, keyword, priority) VALUES ('Shopping', 'walmart', 1)"
        )

    importer.reapply_rules(conn)

    row = conn.execute(
        "SELECT category FROM transactions WHERE description = 'WALMART GROCERY'"
    ).fetchone()
    assert row["category"] == "Shopping"  # higher priority (later) rule wins
