from __future__ import annotations

"""
Plaid live bank sync.

Encapsulates all Plaid API use: Link token creation, public-token exchange,
the /transactions/sync cursor loop, account linking, and balance snapshots.

Credentials come from .env (PLAID_CLIENT_ID / PLAID_SECRET / PLAID_ENV) loaded
via python-dotenv. When unconfigured, is_configured() returns False and the
rest of the app keeps working in CSV-only mode.

Conventions bridged here:
- Plaid amounts are positive for money OUT; ours are positive for money IN,
  so every amount is negated on the way in.
- Descriptions prefer merchant_name, falling back to the raw name.
- Categories come from our DB rules (applied by the importer); when no rule
  matches, Plaid's personal_finance_category.primary is used as a readable
  fallback.
- Rows the user edited (user_edited=1) are never modified or removed by sync.
"""

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timezone

import pandas as pd
from dotenv import load_dotenv

from finance import data_service, db, importer

logger = logging.getLogger(__name__)

# Load .env from the project root once at import time (no-op if missing).
load_dotenv(os.path.join(data_service.get_base_dir(), ".env"))

_PLACEHOLDER_PREFIX = "your-"

# Plaid (type, subtype) -> our account types.
_DEPOSITORY_SUBTYPE_MAP = {
    "checking": "checking",
    "savings": "savings",
    "money market": "savings",
    "cd": "savings",
}
_TYPE_MAP = {
    "credit": "credit",
    "loan": "loan",
    "investment": "investment",
    "brokerage": "investment",
}

# Plaid error codes that mean the user must re-authenticate via Link.
_RECONNECT_ERROR_CODES = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT"}


# --- Configuration / client ---


def is_configured() -> bool:
    """True when real-looking Plaid credentials are present in the environment."""
    client_id = os.environ.get("PLAID_CLIENT_ID", "").strip()
    secret = os.environ.get("PLAID_SECRET", "").strip()
    return bool(
        client_id
        and secret
        and not client_id.startswith(_PLACEHOLDER_PREFIX)
        and not secret.startswith(_PLACEHOLDER_PREFIX)
    )


def _get_client():
    """Build a real Plaid API client from env credentials (imported lazily)."""
    import plaid
    from plaid.api import plaid_api

    env_name = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    host = plaid.Environment.Production if env_name == "production" else plaid.Environment.Sandbox
    configuration = plaid.Configuration(
        host=host,
        api_key={
            "clientId": os.environ["PLAID_CLIENT_ID"],
            "secret": os.environ["PLAID_SECRET"],
        },
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


# --- Schema (additive, idempotent — owned by this module) ---


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive columns this module needs; safe to call repeatedly."""
    item_cols = {row["name"] for row in conn.execute("PRAGMA table_info(plaid_items)")}
    if "last_error" not in item_cols:
        conn.execute("ALTER TABLE plaid_items ADD COLUMN last_error TEXT")
    account_cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "plaid_item_id" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN plaid_item_id TEXT")
    conn.commit()


# --- Error handling ---


def describe_error(exc: Exception) -> str:
    """Human-readable one-liner for a Plaid failure (never includes tokens)."""
    code, message = _extract_plaid_error(exc)
    if code in _RECONNECT_ERROR_CODES:
        return f"Reconnect needed — bank login expired ({code})"
    if code:
        return f"{code}: {message}" if message else code
    return str(exc) or exc.__class__.__name__


def _extract_plaid_error(exc: Exception) -> tuple[str | None, str | None]:
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        try:
            payload = json.loads(body)
            return payload.get("error_code"), payload.get("error_message")
        except (ValueError, AttributeError):
            pass
    return None, None


def error_needs_reconnect(last_error: str | None) -> bool:
    if not last_error:
        return False
    return any(code in last_error for code in _RECONNECT_ERROR_CODES)


# --- Link token / exchange ---


def create_link_token(client=None) -> str:
    """Create a Plaid Link token for the transactions product."""
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    if client is None:
        client = _get_client()
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="finance-tracker-local"),
        client_name="Finance Tracker",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    return str(response.link_token)


def exchange_public_token(
    public_token: str,
    institution_name: str | None = None,
    conn: sqlite3.Connection | None = None,
    client=None,
) -> dict:
    """
    Exchange a Link public_token for an access token, store the plaid_items
    row, and create accounts rows (source='plaid') for the item's accounts.
    Returns {"item_id", "institution_name", "accounts"}.
    """
    from plaid.model.accounts_get_request import AccountsGetRequest
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )

    if client is None:
        client = _get_client()

    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    access_token = str(exchange.access_token)
    item_id = str(exchange.item_id)

    accounts_response = client.accounts_get(AccountsGetRequest(access_token=access_token))
    plaid_accounts = list(getattr(accounts_response, "accounts", []) or [])

    own_conn = conn is None
    if own_conn:
        conn = data_service.get_db_connection()
    try:
        ensure_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO plaid_items (item_id, access_token, institution_name) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "institution_name = COALESCE(excluded.institution_name, institution_name), "
                "last_error = NULL",
                (item_id, access_token, institution_name),
            )
        created = _upsert_plaid_accounts(conn, item_id, plaid_accounts, institution_name)
    finally:
        if own_conn:
            conn.close()

    logger.info(
        "Linked Plaid item %s (%s): %d account(s)",
        item_id, institution_name or "unknown institution", created,
    )
    return {"item_id": item_id, "institution_name": institution_name, "accounts": created}


def map_account_type(plaid_type: str | None, plaid_subtype: str | None) -> str:
    """Map Plaid account type/subtype onto our checking/savings/credit/loan/investment."""
    ptype = _enum_str(plaid_type)
    subtype = _enum_str(plaid_subtype)
    if ptype == "depository":
        return _DEPOSITORY_SUBTYPE_MAP.get(subtype, "checking")
    return _TYPE_MAP.get(ptype, "checking")


def _enum_str(value) -> str:
    """Plaid SDK enums expose .value; plain strings pass through."""
    return str(getattr(value, "value", value) or "").strip().lower()


def _upsert_plaid_accounts(
    conn: sqlite3.Connection,
    item_id: str,
    plaid_accounts: list,
    institution_name: str | None = None,
) -> int:
    """Create accounts rows for any not-yet-known Plaid accounts. Returns count seen."""
    count = 0
    with conn:
        for acct in plaid_accounts:
            plaid_account_id = str(acct.account_id)
            count += 1
            existing = conn.execute(
                "SELECT id FROM accounts WHERE plaid_account_id = ?", (plaid_account_id,)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE accounts SET plaid_item_id = ? WHERE id = ?",
                    (item_id, existing["id"]),
                )
                continue

            base_name = str(getattr(acct, "name", None) or getattr(acct, "official_name", None) or "Account")
            if institution_name:
                base_name = f"{institution_name} {base_name}"
            name = base_name
            # Account names are UNIQUE; disambiguate if taken by another account.
            if db.get_account_by_name(conn, name) is not None:
                name = f"{base_name} ({plaid_account_id[-4:]})"

            account_type = map_account_type(
                getattr(acct, "type", None), getattr(acct, "subtype", None)
            )
            conn.execute(
                "INSERT INTO accounts (name, type, source, plaid_account_id, plaid_item_id) "
                "VALUES (?, ?, 'plaid', ?, ?)",
                (name, account_type, plaid_account_id, item_id),
            )
            logger.info("Created Plaid account '%s' (%s)", name, account_type)
    return count


# --- Transaction sync ---


def _fetch_sync_page(client, access_token: str, cursor: str | None):
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    if cursor:
        request = TransactionsSyncRequest(access_token=access_token, cursor=cursor)
    else:
        request = TransactionsSyncRequest(access_token=access_token)
    return client.transactions_sync(request)


def _txn_fields(txn) -> dict:
    """Normalize one Plaid transaction to our conventions (sign negated)."""
    merchant = getattr(txn, "merchant_name", None)
    name = getattr(txn, "name", None)
    description = str(merchant or name or "Unknown")
    amount = -float(txn.amount)  # Plaid: positive = money out; ours: positive = money in
    raw_date = txn.date
    date_iso = raw_date.strftime("%Y-%m-%d") if hasattr(raw_date, "strftime") else str(raw_date)[:10]
    pfc = getattr(txn, "personal_finance_category", None)
    primary = getattr(pfc, "primary", None) if pfc is not None else None
    fallback_category = str(primary).replace("_", " ").title() if primary else None
    return {
        "transaction_id": str(txn.transaction_id),
        "plaid_account_id": str(txn.account_id),
        "date": date_iso,
        "description": description,
        "amount": amount,
        "fallback_category": fallback_category,
    }


def _plaid_account_map(conn: sqlite3.Connection) -> dict[str, int]:
    """plaid_account_id -> our account id."""
    rows = conn.execute(
        "SELECT id, plaid_account_id FROM accounts WHERE plaid_account_id IS NOT NULL"
    ).fetchall()
    return {row["plaid_account_id"]: int(row["id"]) for row in rows}


def _load_classification():
    from finance.config_loader import load_config

    return load_config(data_service.get_config_path()).classification


def sync_item(
    conn: sqlite3.Connection,
    item: sqlite3.Row | dict,
    classification=None,
    client=None,
    refresh: bool = True,
) -> dict:
    """
    Run the /transactions/sync cursor loop for one plaid_items row, apply
    added/modified/removed to the transactions table (through the importer's
    dedup for adds), snapshot balances, and persist the cursor.

    Returns {"item_id", "added", "modified", "removed", "error"}. Errors are
    stored on the item (last_error) and returned — never raised.
    """
    ensure_schema(conn)
    item_id = str(item["item_id"])
    access_token = str(item["access_token"])
    cursor = item["sync_cursor"] or None

    if client is None:
        client = _get_client()
    if classification is None:
        classification = _load_classification()

    added: list = []
    modified: list = []
    removed: list = []
    plaid_accounts: list = []
    try:
        while True:
            response = _fetch_sync_page(client, access_token, cursor)
            added.extend(response.added)
            modified.extend(response.modified)
            removed.extend(response.removed)
            page_accounts = getattr(response, "accounts", None)
            if page_accounts:
                plaid_accounts = list(page_accounts)
            cursor = str(response.next_cursor)
            if not response.has_more:
                break
    except Exception as exc:  # noqa: BLE001 — every Plaid failure becomes a stored error
        error = describe_error(exc)
        with conn:
            conn.execute(
                "UPDATE plaid_items SET last_error = ? WHERE item_id = ?", (error, item_id)
            )
        logger.error("Plaid sync failed for item %s: %s", item_id, error)
        return {"item_id": item_id, "added": 0, "modified": 0, "removed": 0, "error": error}

    # Make sure every referenced account exists (covers accounts added after link).
    if plaid_accounts:
        _upsert_plaid_accounts(conn, item_id, plaid_accounts)
    account_map = _plaid_account_map(conn)
    rules = db.list_rules(conn)

    n_modified, leftover = _apply_modified(conn, modified, account_map, rules, classification)
    n_added = _apply_added(conn, added + leftover, account_map, rules, classification)
    n_removed = _apply_removed(conn, removed)
    _snapshot_balances(conn, plaid_accounts, account_map)

    with conn:
        conn.execute(
            "UPDATE plaid_items SET sync_cursor = ?, last_synced_at = ?, last_error = NULL "
            "WHERE item_id = ?",
            (cursor, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), item_id),
        )

    logger.info(
        "Plaid sync for item %s: %d added, %d modified, %d removed",
        item_id, n_added, n_modified, n_removed,
    )
    if refresh:
        data_service.refresh_data()
    return {
        "item_id": item_id,
        "added": n_added,
        "modified": n_modified,
        "removed": n_removed,
        "error": None,
    }


def _apply_added(
    conn: sqlite3.Connection,
    txns: list,
    account_map: dict[str, int],
    rules: list,
    classification,
) -> int:
    """Insert added transactions through the importer (dedup + rules), then tag rows."""
    by_account: dict[int, list[dict]] = {}
    for txn in txns:
        fields = _txn_fields(txn)
        account_id = account_map.get(fields["plaid_account_id"])
        if account_id is None:
            logger.warning(
                "Skipping Plaid transaction for unknown account %s", fields["plaid_account_id"]
            )
            continue
        by_account.setdefault(account_id, []).append(fields)

    imported = 0
    for account_id, rows in by_account.items():
        frame = pd.DataFrame(
            {
                "date": [r["date"] for r in rows],
                "description": [r["description"] for r in rows],
                "amount": [r["amount"] for r in rows],
            }
        )
        result = importer.import_rows(
            conn, account_id, frame, classification, source="plaid"
        )
        imported += result.imported

        # Tag rows with their plaid_transaction_id and apply the Plaid
        # personal-finance-category fallback where our rules matched nothing.
        with conn:
            for r in rows:
                dedup_hash = importer.compute_dedup_hash(
                    account_id, r["date"], r["amount"], r["description"]
                )
                conn.execute(
                    "UPDATE transactions SET plaid_transaction_id = ? "
                    "WHERE dedup_hash = ? AND plaid_transaction_id IS NULL",
                    (r["transaction_id"], dedup_hash),
                )
                if r["fallback_category"]:
                    conn.execute(
                        "UPDATE transactions SET category = ? "
                        "WHERE dedup_hash = ? AND category IS NULL AND user_edited = 0",
                        (r["fallback_category"], dedup_hash),
                    )
    return imported


def _apply_modified(
    conn: sqlite3.Connection,
    txns: list,
    account_map: dict[str, int],
    rules: list,
    classification,
) -> tuple[int, list]:
    """
    Update modified transactions by plaid_transaction_id, skipping user-edited
    rows. Transactions we've never seen are returned as leftovers to be added.
    """
    updated = 0
    leftover: list = []
    with conn:
        for txn in txns:
            fields = _txn_fields(txn)
            row = conn.execute(
                "SELECT id, account_id, user_edited FROM transactions "
                "WHERE plaid_transaction_id = ?",
                (fields["transaction_id"],),
            ).fetchone()
            if row is None:
                leftover.append(txn)
                continue
            if row["user_edited"]:
                logger.info(
                    "Skipping Plaid modification of user-edited transaction %s", row["id"]
                )
                continue

            account_id = account_map.get(fields["plaid_account_id"], int(row["account_id"]))
            dedup_hash = importer.compute_dedup_hash(
                account_id, fields["date"], fields["amount"], fields["description"]
            )
            txn_type = importer.classify_txn_type(
                fields["description"], fields["amount"], classification
            )
            category = (
                importer.categorize_row(fields["description"], txn_type, rules)
                or fields["fallback_category"]
            )
            try:
                conn.execute(
                    "UPDATE transactions SET date = ?, description = ?, amount = ?, "
                    "txn_type = ?, category = ?, dedup_hash = ? WHERE id = ?",
                    (fields["date"], fields["description"], fields["amount"],
                     txn_type, category, dedup_hash, row["id"]),
                )
                updated += 1
            except sqlite3.IntegrityError:
                # New values collide with an existing row's dedup hash; keep the old row.
                logger.warning(
                    "Modified Plaid transaction %s collides with an existing row; skipped",
                    fields["transaction_id"],
                )
    return updated, leftover


def _apply_removed(conn: sqlite3.Connection, txns: list) -> int:
    """Delete removed transactions by plaid_transaction_id, sparing user-edited rows."""
    deleted = 0
    with conn:
        for txn in txns:
            transaction_id = str(getattr(txn, "transaction_id", txn))
            cur = conn.execute(
                "DELETE FROM transactions WHERE plaid_transaction_id = ? AND user_edited = 0",
                (transaction_id,),
            )
            deleted += cur.rowcount
    return deleted


def _snapshot_balances(
    conn: sqlite3.Connection, plaid_accounts: list, account_map: dict[str, int]
) -> int:
    """Insert today's balance snapshot (source='plaid') for each synced account."""
    today = date.today().strftime("%Y-%m-%d")
    written = 0
    with conn:
        for acct in plaid_accounts:
            plaid_account_id = str(acct.account_id)
            account_id = account_map.get(plaid_account_id)
            balances = getattr(acct, "balances", None)
            current = getattr(balances, "current", None) if balances is not None else None
            if account_id is None or current is None:
                continue
            conn.execute(
                "INSERT INTO balance_snapshots (account_id, date, balance, source) "
                "VALUES (?, ?, ?, 'plaid') "
                "ON CONFLICT(account_id, date, source) DO UPDATE SET balance = excluded.balance",
                (account_id, today, float(current)),
            )
            written += 1
    return written


def sync_all(
    conn: sqlite3.Connection | None = None,
    classification=None,
    client=None,
    refresh: bool = True,
) -> list[dict]:
    """Sync every linked Plaid item. Per-item errors are captured, never raised."""
    own_conn = conn is None
    if own_conn:
        conn = data_service.get_db_connection()
    try:
        ensure_schema(conn)
        items = conn.execute("SELECT * FROM plaid_items ORDER BY id").fetchall()
        results = [
            sync_item(conn, item, classification=classification, client=client, refresh=False)
            for item in items
        ]
    finally:
        if own_conn:
            conn.close()

    if refresh and any(r["error"] is None for r in results):
        data_service.refresh_data()
    return results


# --- Status ---


def get_status(conn: sqlite3.Connection) -> list[dict]:
    """Per-item status for the Accounts page: institution, last sync, errors."""
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT i.item_id, i.institution_name, i.last_synced_at, i.last_error,
               (SELECT COUNT(*) FROM accounts a
                 WHERE a.plaid_item_id = i.item_id AND a.active = 1) AS account_count
        FROM plaid_items i
        ORDER BY i.institution_name, i.id
        """
    ).fetchall()
    return [
        {
            "item_id": row["item_id"],
            "institution_name": row["institution_name"] or "Unknown institution",
            "last_synced_at": row["last_synced_at"],
            "account_count": row["account_count"],
            "last_error": row["last_error"],
            "needs_reconnect": error_needs_reconnect(row["last_error"]),
        }
        for row in rows
    ]
