from __future__ import annotations

import json
import logging
import os
import sqlite3

from finance import db, importer
from finance.config_loader import AccountConfig, AppConfig
from finance.csv_reader import read_account_csv

logger = logging.getLogger(__name__)

MANUAL_BALANCES_FILE = "manual_balances.json"


def _column_mapping_json(account: AccountConfig) -> str:
    """Serialize an account's CSV column mapping (config.yaml shape) to JSON."""
    return json.dumps({
        "file": account.file,
        "columns": {
            "date": account.columns.date,
            "description": account.columns.description,
            "amount": account.columns.amount,
            "debit": account.columns.debit,
            "credit": account.columns.credit,
        },
        "date_format": account.date_format,
        "amount_sign": account.amount_sign,
        "balance_column": account.balance_column,
        "opening_balance": account.opening_balance,
        "opening_date": account.opening_date,
    })


def _config_tombstones(conn: sqlite3.Connection) -> set[str]:
    """Config files whose accounts were deleted/merged away via the UI — never re-seed."""
    return {row["file"] for row in conn.execute("SELECT file FROM config_tombstones")}


def _accounts_by_config_file(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """
    Map of column_mapping "file" -> accounts row. This is the stable identity
    of a config.yaml-seeded account: matching on it (instead of name) means a
    rename via the UI survives restarts without the old name being re-created.
    """
    result: dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT * FROM accounts WHERE column_mapping IS NOT NULL"):
        try:
            file = json.loads(row["column_mapping"]).get("file")
        except (TypeError, ValueError):
            continue
        if file:
            result.setdefault(file, row)
    return result


def _find_config_account(
    conn: sqlite3.Connection, account: AccountConfig, by_file: dict[str, sqlite3.Row]
) -> sqlite3.Row | None:
    """Existing accounts row for a config account: by file identity, then by name."""
    existing = by_file.get(account.file)
    if existing is not None:
        return existing
    return db.get_account_by_name(conn, account.name)


def seed_accounts_from_config(conn: sqlite3.Connection, config: AppConfig) -> dict[str, int]:
    """
    Ensure an accounts row exists per configured CSV account. Returns name -> id.

    Matching order: config-file identity (column_mapping "file") first — so
    accounts renamed via the UI are recognized and NOT re-created under their
    config.yaml name — then legacy match by name. Tombstoned files (accounts
    deleted via the UI) are skipped entirely.
    """
    ids: dict[str, int] = {}
    tombstones = _config_tombstones(conn)
    by_file = _accounts_by_config_file(conn)
    with conn:
        for account in config.accounts:
            if account.file in tombstones:
                continue
            existing = _find_config_account(conn, account, by_file)
            if existing is not None:
                ids[account.name] = int(existing["id"])
                continue
            ids[account.name] = db.upsert_account(
                conn,
                name=account.name,
                account_type=account.type,
                source="csv",
                column_mapping=_column_mapping_json(account),
            )
    return ids


def seed_rules_from_config(conn: sqlite3.Connection, config: AppConfig) -> int:
    """
    Seed categorization_rules from YAML rules — only when the table is empty,
    so rules later deleted via the UI are not resurrected on restart.
    Priority preserves YAML order (later rules win, as before).
    """
    row = conn.execute("SELECT COUNT(*) AS c FROM categorization_rules").fetchone()
    if row["c"] > 0:
        return 0
    seeded = 0
    with conn:
        for priority, rule in enumerate(config.categorization_rules):
            for keyword in rule.keywords:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO categorization_rules (category, keyword, priority) "
                    "VALUES (?, ?, ?)",
                    (rule.category, keyword.lower(), priority),
                )
                seeded += cur.rowcount
    if seeded:
        logger.info("Seeded %d categorization rules from config.yaml", seeded)
    return seeded


def import_manual_balances(conn: sqlite3.Connection, data_dir: str) -> int:
    """
    Import data/manual_balances.json into balance_snapshots (source='manual') —
    only when no manual snapshots exist yet, so snapshots later deleted via the
    UI are not resurrected on restart.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM balance_snapshots WHERE source = 'manual'"
    ).fetchone()
    if row["c"] > 0:
        return 0

    filepath = os.path.join(data_dir, MANUAL_BALANCES_FILE)
    if not os.path.exists(filepath):
        return 0
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read %s: %s", filepath, e)
        return 0
    if not isinstance(entries, list):
        return 0

    imported = 0
    with conn:
        for entry in entries:
            account = str(entry.get("account", "")).strip()
            entry_date = str(entry.get("date", "")).strip()
            balance = entry.get("balance")
            if not account or not entry_date or balance is None:
                continue
            account_id = db.upsert_account(
                conn, name=account, account_type="manual_balance", source="manual"
            )
            conn.execute(
                "INSERT OR REPLACE INTO balance_snapshots (account_id, date, balance, source) "
                "VALUES (?, ?, ?, 'manual')",
                (account_id, entry_date, float(balance)),
            )
            imported += 1
    if imported:
        logger.info("Imported %d manual balance entries into balance_snapshots", imported)
    return imported


def sync_csv_files(conn: sqlite3.Connection, config: AppConfig, data_dir: str) -> list[importer.ImportResult]:
    """
    Import every configured data/*.csv through the unified importer.
    Idempotent: dedup makes re-runs no-ops.
    """
    results = []
    tombstones = _config_tombstones(conn)
    by_file = _accounts_by_config_file(conn)
    for account in config.accounts:
        if account.file in tombstones:
            continue  # account was deleted/merged away via the UI — don't resurrect
        filepath = os.path.join(data_dir, account.file)
        if not os.path.exists(filepath):
            logger.warning("Skipping missing CSV: %s", filepath)
            continue
        try:
            rows = read_account_csv(account, data_dir)
        except Exception as e:
            logger.error("Failed to read %s: %s", account.file, e)
            continue
        existing = _find_config_account(conn, account, by_file)
        if existing is not None:
            account_id = int(existing["id"])
        else:
            account_id = db.upsert_account(
                conn,
                name=account.name,
                account_type=account.type,
                source="csv",
                column_mapping=_column_mapping_json(account),
            )
            conn.commit()
        results.append(
            importer.import_rows(
                conn,
                account_id=account_id,
                rows=rows,
                classification=config.classification,
                filename=account.file,
                source="csv",
            )
        )
    return results


def run_startup_migration(conn: sqlite3.Connection, config: AppConfig, data_dir: str) -> None:
    """
    Bring the DB up to date from the file-based world. Idempotent:
    - accounts upserted by name
    - CSV transactions deduped by dedup_hash
    - rules seeded only when the rules table is empty
    - manual balances imported only when no manual snapshots exist
    """
    db.init_db(conn)
    seed_accounts_from_config(conn, config)
    seed_rules_from_config(conn, config)
    sync_csv_files(conn, config, data_dir)
    import_manual_balances(conn, data_dir)
