from __future__ import annotations

import logging
import os
import sqlite3

import pandas as pd

logger = logging.getLogger(__name__)

DB_FILENAME = "finance.db"

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'csv',
    plaid_account_id TEXT,
    column_mapping TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    category TEXT,
    txn_type TEXT,
    raw_balance REAL,
    dedup_hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'csv',
    plaid_transaction_id TEXT,
    user_edited INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_dedup_hash
    ON transactions(dedup_hash);
CREATE INDEX IF NOT EXISTS idx_transactions_account_date
    ON transactions(account_id, date);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    balance REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    UNIQUE(account_id, date, source)
);

CREATE TABLE IF NOT EXISTS categorization_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    keyword TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    UNIQUE(category, keyword)
);

CREATE TABLE IF NOT EXISTS plaid_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE,
    access_token TEXT NOT NULL,
    institution_name TEXT,
    sync_cursor TEXT,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id),
    filename TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    row_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0
);
"""


def get_db_path(data_dir: str) -> str:
    """Return the path of the SQLite database inside a data directory."""
    return os.path.join(data_dir, DB_FILENAME)


def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Open a SQLite connection with WAL mode and foreign keys enabled.
    Rows are returned as sqlite3.Row (dict-like access).
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if missing and record the schema version. Idempotent."""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        # Future migrations go here, keyed off row["version"].
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


# --- Account helpers ---


def get_account_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()


def upsert_account(
    conn: sqlite3.Connection,
    name: str,
    account_type: str,
    source: str = "csv",
    plaid_account_id: str | None = None,
    column_mapping: str | None = None,
) -> int:
    """Insert an account if it doesn't exist (matched by name). Returns the account id."""
    existing = get_account_by_name(conn, name)
    if existing is not None:
        return int(existing["id"])
    cur = conn.execute(
        "INSERT INTO accounts (name, type, source, plaid_account_id, column_mapping) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, account_type, source, plaid_account_id, column_mapping),
    )
    return int(cur.lastrowid)


def list_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY name").fetchall()


# --- Categorization rule helpers ---


def list_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rules ordered by priority (ascending) then id — later rules win ties on apply."""
    return conn.execute(
        "SELECT * FROM categorization_rules ORDER BY priority, id"
    ).fetchall()


def rules_grouped_by_category(conn: sqlite3.Connection) -> list[dict]:
    """
    Group rules into the YAML-era shape:
    [{"category": "Groceries", "keywords": ["walmart", ...]}, ...]
    ordered by category priority (max priority of its keywords).
    """
    grouped: dict[str, dict] = {}
    for row in list_rules(conn):
        entry = grouped.setdefault(
            row["category"], {"category": row["category"], "keywords": [], "_priority": 0}
        )
        entry["keywords"].append(row["keyword"])
        entry["_priority"] = max(entry["_priority"], row["priority"])
    result = sorted(grouped.values(), key=lambda e: e["_priority"])
    for entry in result:
        del entry["_priority"]
    return result


def next_rule_priority(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(priority), -1) + 1 AS p FROM categorization_rules").fetchone()
    return int(row["p"])


# --- DataFrame loaders (analytics layer consumes these) ---

TRANSACTIONS_DF_COLUMNS = [
    "date", "description", "amount", "account_name", "account_type",
    "raw_balance", "category", "subcategory",
]


def load_transactions_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Load all transactions joined with account metadata, in the standardized
    in-memory shape the analytics layer consumes:

        date (datetime64), description, amount, account_name, account_type,
        raw_balance, category (income/expense/transfer), subcategory

    Note the naming translation: DB `txn_type` -> DataFrame `category`,
    DB `category` -> DataFrame `subcategory` (this preserves the pre-DB
    in-memory contract used by routes/analytics/templates).
    """
    query = """
        SELECT t.date, t.description, t.amount,
               a.name AS account_name, a.type AS account_type,
               t.raw_balance, t.txn_type AS category, t.category AS subcategory
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE a.active = 1
        ORDER BY t.date
    """
    df = pd.read_sql_query(query, conn)
    if df.empty:
        return pd.DataFrame(columns=TRANSACTIONS_DF_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_balance_snapshots_df(
    conn: sqlite3.Connection, source: str | None = None
) -> pd.DataFrame:
    """
    Load balance snapshots joined with account names.

    Returns DataFrame with columns: [date, account_name, balance, source]
    sorted by date ascending (matches the old manual_balances shape).
    """
    query = """
        SELECT s.date, a.name AS account_name, s.balance, s.source
        FROM balance_snapshots s
        JOIN accounts a ON a.id = s.account_id
    """
    params: tuple = ()
    if source is not None:
        query += " WHERE s.source = ?"
        params = (source,)
    query += " ORDER BY s.date"
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return pd.DataFrame(columns=["date", "account_name", "balance", "source"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_accounts_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """Accounts table as a DataFrame."""
    return pd.read_sql_query(
        "SELECT id, name, type, source, plaid_account_id, active FROM accounts ORDER BY name",
        conn,
    )


def count_transactions(conn: sqlite3.Connection, account_id: int | None = None) -> int:
    if account_id is None:
        row = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE account_id = ?", (account_id,)
        ).fetchone()
    return int(row["c"])
