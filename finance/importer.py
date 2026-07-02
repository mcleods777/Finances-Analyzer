from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass

import pandas as pd

from finance.config_loader import ClassificationConfig
from finance import db

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ImportResult:
    account_id: int
    filename: str | None
    imported: int
    duplicates: int


def normalize_description(description: str) -> str:
    """Lowercase and collapse all whitespace runs to single spaces."""
    return _WHITESPACE_RE.sub(" ", str(description).strip().lower())


def compute_dedup_hash(account_id: int, date_iso: str, amount: float, description: str) -> str:
    """
    Stable dedup hash for a transaction:
    sha256 of "account_id|date-iso|amount-2dp|normalized_description".
    """
    key = f"{account_id}|{date_iso}|{float(amount):.2f}|{normalize_description(description)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def classify_txn_type(description: str, amount: float, classification: ClassificationConfig) -> str:
    """
    Classify a single transaction as income/expense/transfer using the same
    priority order as data_processor.classify_transactions:
    sign default -> transfer keywords -> income keywords -> expense keywords.
    """
    desc_lower = str(description).lower()
    txn_type = "income" if amount > 0 else "expense"
    for keyword in classification.transfer_keywords:
        if keyword in desc_lower:
            txn_type = "transfer"
            break
    for keyword in classification.income_keywords:
        if keyword in desc_lower:
            txn_type = "income"
            break
    for keyword in classification.expense_keywords:
        if keyword in desc_lower:
            txn_type = "expense"
            break
    return txn_type


def apply_rules_to_description(description: str, rules: list) -> str | None:
    """
    Return the category for a description given ordered rules
    (rows with .keyword/.category, ordered by priority ascending).
    The LAST matching rule wins — mirrors the YAML loop semantics.
    """
    desc_lower = str(description).lower()
    matched: str | None = None
    for rule in rules:
        if rule["keyword"] in desc_lower:
            matched = rule["category"]
    return matched


def categorize_row(description: str, txn_type: str, rules: list) -> str | None:
    """Category for one row: rule match, else 'Transfer' for transfer rows, else None."""
    category = apply_rules_to_description(description, rules)
    if category is None and txn_type == "transfer":
        # Mirror classify_transactions: transfers without a rule match
        # get the "Transfer" subcategory automatically.
        category = "Transfer"
    return category


def import_rows(
    conn: sqlite3.Connection,
    account_id: int,
    rows: pd.DataFrame,
    classification: ClassificationConfig,
    filename: str | None = None,
    source: str = "csv",
    record_empty_audit: bool = False,
) -> ImportResult:
    """
    Insert normalized transaction rows for one account, deduplicating on
    dedup_hash. `rows` uses the standardized csv_reader columns:
    date, description, amount, [account_name, account_type,] raw_balance.

    Everything runs in a single SQLite transaction; an imports audit row is
    recorded (skipped when nothing was imported unless record_empty_audit).
    Returns an ImportResult with imported/duplicate counts.
    """
    rules = db.list_rules(conn)
    imported = 0
    duplicates = 0

    try:
        with conn:  # one transaction per file/batch
            for row in rows.itertuples(index=False):
                date_iso = pd.Timestamp(row.date).strftime("%Y-%m-%d")
                amount = float(row.amount)
                description = str(row.description)
                raw_balance = getattr(row, "raw_balance", None)
                if raw_balance is not None and pd.isna(raw_balance):
                    raw_balance = None

                dedup_hash = compute_dedup_hash(account_id, date_iso, amount, description)
                txn_type = classify_txn_type(description, amount, classification)
                category = categorize_row(description, txn_type, rules)

                cur = conn.execute(
                    "INSERT OR IGNORE INTO transactions "
                    "(account_id, date, description, amount, category, txn_type, "
                    " raw_balance, dedup_hash, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (account_id, date_iso, description, amount, category, txn_type,
                     raw_balance, dedup_hash, source),
                )
                if cur.rowcount:
                    imported += 1
                else:
                    duplicates += 1

            if imported > 0 or record_empty_audit:
                conn.execute(
                    "INSERT INTO imports (account_id, filename, row_count, duplicate_count) "
                    "VALUES (?, ?, ?, ?)",
                    (account_id, filename, imported, duplicates),
                )
    except Exception:
        logger.exception("Import failed for account %s (%s); batch rolled back", account_id, filename)
        raise

    logger.info(
        "Imported %d rows (%d duplicates skipped) for account %s (%s)",
        imported, duplicates, account_id, filename or source,
    )
    return ImportResult(account_id=account_id, filename=filename, imported=imported, duplicates=duplicates)


def reapply_rules(conn: sqlite3.Connection) -> int:
    """
    Re-run categorization rules against all transactions, skipping rows the
    user has manually edited (user_edited=1). Returns the number of updated rows.
    """
    rules = db.list_rules(conn)
    updated = 0
    with conn:
        txns = conn.execute(
            "SELECT id, description, category, txn_type FROM transactions WHERE user_edited = 0"
        ).fetchall()
        for txn in txns:
            category = categorize_row(txn["description"], txn["txn_type"], rules)
            if category != txn["category"]:
                conn.execute(
                    "UPDATE transactions SET category = ? WHERE id = ?",
                    (category, txn["id"]),
                )
                updated += 1
    if updated:
        logger.info("Re-applied rules: %d transactions re-categorized", updated)
    return updated
