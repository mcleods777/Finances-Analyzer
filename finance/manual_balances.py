"""
Manual balance snapshots.

Historically stored in data/manual_balances.json; now backed by the
balance_snapshots table in data/finance.db (source='manual'). The original
JSON file is imported once by the startup migration (finance/migrate.py) and
kept on disk untouched as a legacy artifact.

The public API surface (data_dir-based, dict entries with "account"/"date"/
"balance" keys) is preserved because the routes depend on it.
"""

from __future__ import annotations

import logging

import pandas as pd

from finance import db

logger = logging.getLogger(__name__)


def _connect(data_dir: str):
    conn = db.get_connection(db.get_db_path(data_dir))
    db.init_db(conn)
    return conn


def load_manual_balances(data_dir: str) -> pd.DataFrame:
    """
    Load manual balance snapshots.

    Returns DataFrame with columns: [date, account_name, balance]
    Sorted by date ascending.
    """
    conn = _connect(data_dir)
    try:
        df = db.load_balance_snapshots_df(conn, source="manual")
    finally:
        conn.close()
    if df.empty:
        return pd.DataFrame(columns=["date", "account_name", "balance"])
    return df[["date", "account_name", "balance"]].reset_index(drop=True)


def save_balance_entry(
    data_dir: str, account: str, entry_date: str, balance: float
) -> None:
    """
    Save a balance snapshot for account+date.
    If an entry for the same account+date already exists, it's updated.
    """
    conn = _connect(data_dir)
    try:
        with conn:
            account_id = db.upsert_account(
                conn, name=account, account_type="manual_balance", source="manual"
            )
            conn.execute(
                "INSERT OR REPLACE INTO balance_snapshots (account_id, date, balance, source) "
                "VALUES (?, ?, ?, 'manual')",
                (account_id, entry_date, float(balance)),
            )
    finally:
        conn.close()
    logger.info("Saved balance: %s on %s = $%.2f", account, entry_date, balance)


def delete_balance_entry(data_dir: str, account: str, entry_date: str) -> bool:
    """
    Remove a specific balance snapshot by account + date.
    Returns True if an entry was removed, False if not found.
    """
    conn = _connect(data_dir)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM balance_snapshots "
                "WHERE source = 'manual' AND date = ? "
                "AND account_id = (SELECT id FROM accounts WHERE name = ?)",
                (entry_date, account),
            )
            deleted = cur.rowcount > 0
    finally:
        conn.close()
    if deleted:
        logger.info("Deleted balance: %s on %s", account, entry_date)
    return deleted


def get_manual_account_names(data_dir: str) -> list[str]:
    """Return distinct account names that have manual snapshots, sorted."""
    conn = _connect(data_dir)
    try:
        rows = conn.execute(
            "SELECT DISTINCT a.name FROM balance_snapshots s "
            "JOIN accounts a ON a.id = s.account_id "
            "WHERE s.source = 'manual' ORDER BY a.name"
        ).fetchall()
    finally:
        conn.close()
    return [row["name"] for row in rows]


def get_all_entries(data_dir: str) -> list[dict]:
    """
    Return all manual balance entries as a list of dicts
    ({"account", "date", "balance"}), sorted by date descending
    (most recent first) for display.
    """
    conn = _connect(data_dir)
    try:
        rows = conn.execute(
            "SELECT a.name AS account, s.date, s.balance "
            "FROM balance_snapshots s JOIN accounts a ON a.id = s.account_id "
            "WHERE s.source = 'manual' "
            "ORDER BY s.date DESC, a.name DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def save_bulk_entries(
    data_dir: str, account: str, entries_list: list[dict]
) -> int:
    """
    Save multiple balance entries for a single account at once.
    Each entry in entries_list should have {"date": "YYYY-MM-DD", "balance": float}.
    Entries with the same account+date are updated; new ones are appended.
    Returns the number of entries saved.
    """
    conn = _connect(data_dir)
    saved = 0
    try:
        with conn:
            account_id = db.upsert_account(
                conn, name=account, account_type="manual_balance", source="manual"
            )
            for item in entries_list:
                entry_date = str(item.get("date", "")).strip()
                balance = item.get("balance")
                if not entry_date or balance is None:
                    continue
                try:
                    balance = float(balance)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO balance_snapshots (account_id, date, balance, source) "
                    "VALUES (?, ?, ?, 'manual')",
                    (account_id, entry_date, balance),
                )
                saved += 1
    finally:
        conn.close()
    if saved > 0:
        logger.info("Bulk saved %d entries for %s", saved, account)
    return saved
