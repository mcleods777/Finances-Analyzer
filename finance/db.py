from __future__ import annotations

import json
import logging
import os
import sqlite3

import pandas as pd

logger = logging.getLogger(__name__)

DB_FILENAME = "finance.db"

SCHEMA_VERSION = 3

# The Dossier's four sections (see the CFO-desk design doc).
PROFILE_SECTIONS = ("goal", "weakness", "debt", "note")

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
    plaid_item_id TEXT,
    column_mapping TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    hidden INTEGER NOT NULL DEFAULT 0,
    exclude_from_net_worth INTEGER NOT NULL DEFAULT 0,
    institution TEXT
);

-- Config-file identities (column_mapping "file") of config.yaml accounts the
-- user deleted or merged away via the UI; the startup migration skips these
-- so it never resurrects them (see finance/migrate.py).
CREATE TABLE IF NOT EXISTS config_tombstones (
    file TEXT PRIMARY KEY
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

-- v3: The Desk (conversational CFO), The Archive, and the Dossier.

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    model TEXT NOT NULL,
    intelligence TEXT NOT NULL DEFAULT 'standard',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived INTEGER NOT NULL DEFAULT 0
);

-- content_json holds the exact API-shape content blocks, verbatim
-- (incl. tool_use / tool_result / thinking blocks), so full histories can
-- be resent to the stateless API. display_text is the extracted prose for
-- fast rendering (empty for tool_result turns).
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    display_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    usage_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
    ON chat_messages(conversation_id, id);

-- Append-only insight log (The Archive): every briefing plus every
-- chat-surfaced insight. Both humans and the models read this.
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    fingerprints_json TEXT,
    model TEXT,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_insights_created ON insights(created_at);

-- The Dossier: the advisor's memory of the user. Soft delete via active.
CREATE TABLE IF NOT EXISTS profile_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    active INTEGER NOT NULL DEFAULT 1
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
    _ensure_v2_account_columns(conn)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        if row["version"] < 3:
            _backfill_insights_from_briefing_state(conn)
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def _backfill_insights_from_briefing_state(conn: sqlite3.Connection) -> None:
    """
    v3 backfill (best-effort, additive): import recent_briefings from the
    briefing_state.json next to the database file into the insights table
    (source='briefing'), oldest first, so the Archive starts with whatever
    briefing history survived. Runs once (version-gated) and never raises —
    a missing/corrupt state file just means an empty starting Archive.
    """
    try:
        db_row = conn.execute("PRAGMA database_list").fetchone()
        db_file = db_row["file"] if db_row else None
        if not db_file:
            return
        state_path = os.path.join(os.path.dirname(db_file), "briefing_state.json")
        if not os.path.exists(state_path):
            return
        with open(state_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        briefings = raw.get("recent_briefings") if isinstance(raw, dict) else None
        if not isinstance(briefings, list):
            return
        imported = 0
        for entry in reversed(briefings):  # oldest first
            if not isinstance(entry, dict):
                continue
            prose = (entry.get("prose") or "").strip()
            if not prose:
                continue
            fingerprints = [
                p.get("fingerprint")
                for p in (entry.get("patterns") or [])
                if isinstance(p, dict) and p.get("fingerprint")
            ]
            insert_insight(
                conn,
                source="briefing",
                text=prose,
                fingerprints=fingerprints,
                created_at=entry.get("rendered_at"),
            )
            imported += 1
        if imported:
            logger.info("Backfilled %d briefing(s) into the insights archive", imported)
    except Exception:
        logger.exception("Briefing-state insight backfill failed (skipped)")


def _ensure_v2_account_columns(conn: sqlite3.Connection) -> None:
    """
    v2 account-management columns (additive, idempotent ALTERs). Checked on
    every init rather than keyed off schema_version alone so a database left
    in a partial state still heals; plaid_item_id may already exist because
    plaid_sync.ensure_schema added it ad hoc on v1 databases.
    """
    account_cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    added = False
    for column, ddl in (
        ("plaid_item_id", "ALTER TABLE accounts ADD COLUMN plaid_item_id TEXT"),
        ("hidden", "ALTER TABLE accounts ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"),
        ("exclude_from_net_worth",
         "ALTER TABLE accounts ADD COLUMN exclude_from_net_worth INTEGER NOT NULL DEFAULT 0"),
        ("institution", "ALTER TABLE accounts ADD COLUMN institution TEXT"),
    ):
        if column not in account_cols:
            conn.execute(ddl)
            added = True
    # Backfill institution from the linked Plaid item where possible.
    conn.execute(
        "UPDATE accounts SET institution = "
        "  (SELECT i.institution_name FROM plaid_items i WHERE i.item_id = accounts.plaid_item_id) "
        "WHERE institution IS NULL AND plaid_item_id IS NOT NULL"
    )
    if added:
        logger.info("Added v2 account-management columns to accounts table")


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


def excluded_net_worth_account_names(conn: sqlite3.Connection) -> set[str]:
    """Names of accounts flagged exclude_from_net_worth (net-worth math skips them)."""
    rows = conn.execute(
        "SELECT name FROM accounts WHERE exclude_from_net_worth = 1"
    ).fetchall()
    return {row["name"] for row in rows}


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
        WHERE a.active = 1 AND a.hidden = 0
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
        WHERE a.hidden = 0
    """
    params: tuple = ()
    if source is not None:
        query += " AND s.source = ?"
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


# --- Conversation helpers (The Desk) ---


def create_conversation(
    conn: sqlite3.Connection, title: str, model: str, intelligence: str
) -> int:
    cur = conn.execute(
        "INSERT INTO conversations (title, model, intelligence) VALUES (?, ?, ?)",
        (title, model, intelligence),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_conversation(conn: sqlite3.Connection, conversation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()


def list_conversations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Most recently active first (the Desk's left rail order)."""
    return conn.execute(
        "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC"
    ).fetchall()


def update_conversation(
    conn: sqlite3.Connection,
    conversation_id: int,
    title: str | None = None,
    model: str | None = None,
    intelligence: str | None = None,
    archived: bool | None = None,
) -> bool:
    """Update the given fields (and updated_at). Returns False if the id is unknown."""
    sets, params = [], []
    for column, value in (
        ("title", title), ("model", model), ("intelligence", intelligence),
        ("archived", None if archived is None else int(archived)),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    sets.append("updated_at = datetime('now')")
    cur = conn.execute(
        f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?",
        (*params, conversation_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_conversation(conn: sqlite3.Connection, conversation_id: int) -> bool:
    """Delete a conversation (messages cascade; insights keep the text, FK nulls)."""
    cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    conn.commit()
    return cur.rowcount > 0


def insert_chat_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    role: str,
    content: list,
    display_text: str = "",
    usage: dict | None = None,
) -> int:
    """Store one API-shape message turn (content blocks serialized verbatim)."""
    cur = conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content_json, display_text, usage_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            conversation_id,
            role,
            json.dumps(content),
            display_text,
            json.dumps(usage) if usage is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_chat_messages(conn: sqlite3.Connection, conversation_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()


# --- Insight helpers (The Archive — append-only) ---


def insert_insight(
    conn: sqlite3.Connection,
    source: str,
    text: str,
    fingerprints: list[str] | None = None,
    model: str | None = None,
    conversation_id: int | None = None,
    created_at: str | None = None,
) -> int:
    """Append one insight. `created_at` is only passed by the v3 backfill."""
    cur = conn.execute(
        "INSERT INTO insights (created_at, source, text, fingerprints_json, model, conversation_id) "
        "VALUES (COALESCE(?, datetime('now')), ?, ?, ?, ?, ?)",
        (
            created_at,
            source,
            text,
            json.dumps(fingerprints) if fingerprints is not None else None,
            model,
            conversation_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_insights(
    conn: sqlite3.Connection,
    source: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Newest first, optional source filter and case-insensitive substring search."""
    sql = "SELECT * FROM insights WHERE 1=1"
    params: list = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    if query:
        sql += " AND text LIKE ? ESCAPE '\\'"
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def count_insights(
    conn: sqlite3.Connection, source: str | None = None, query: str | None = None
) -> int:
    sql = "SELECT COUNT(*) AS c FROM insights WHERE 1=1"
    params: list = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    if query:
        sql += " AND text LIKE ? ESCAPE '\\'"
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    return int(conn.execute(sql, params).fetchone()["c"])


def delete_insight(conn: sqlite3.Connection, insight_id: int) -> bool:
    """User curation: the one non-append operation on the Archive."""
    cur = conn.execute("DELETE FROM insights WHERE id = ?", (insight_id,))
    conn.commit()
    return cur.rowcount > 0


# --- Profile helpers (the Dossier) ---


def insert_profile_entry(
    conn: sqlite3.Connection, section: str, text: str, source: str = "user"
) -> int:
    if section not in PROFILE_SECTIONS:
        raise ValueError(f"Unknown profile section: {section!r}")
    cur = conn.execute(
        "INSERT INTO profile_entries (section, text, source) VALUES (?, ?, ?)",
        (section, text, source),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_profile_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM profile_entries WHERE id = ?", (entry_id,)
    ).fetchone()


def list_profile_entries(
    conn: sqlite3.Connection, active_only: bool = True
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM profile_entries"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY section, id"
    return conn.execute(sql).fetchall()


def update_profile_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    text: str | None = None,
    active: bool | None = None,
) -> bool:
    """Update text and/or the active flag (soft delete). Returns False if unknown."""
    sets, params = [], []
    if text is not None:
        sets.append("text = ?")
        params.append(text)
    if active is not None:
        sets.append("active = ?")
        params.append(int(active))
    sets.append("updated_at = datetime('now')")
    cur = conn.execute(
        f"UPDATE profile_entries SET {', '.join(sets)} WHERE id = ?",
        (*params, entry_id),
    )
    conn.commit()
    return cur.rowcount > 0


def count_transactions(conn: sqlite3.Connection, account_id: int | None = None) -> int:
    if account_id is None:
        row = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE account_id = ?", (account_id,)
        ).fetchone()
    return int(row["c"])
