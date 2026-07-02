from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request

from finance.data_service import get_data_dir, get_db_connection, refresh_data
from finance.manual_balances import (
    delete_balance_entry,
    get_all_entries,
    save_balance_entry,
)

logger = logging.getLogger(__name__)

accounts_bp = Blueprint("accounts", __name__)


@accounts_bp.route("/accounts")
def accounts_page():
    """
    Accounts overview: every DB account with its latest known balance.
    (Upload and Plaid link UI arrive in a later wave.)
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.name, a.type, a.source,
                   (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS txn_count,
                   (SELECT s.balance FROM balance_snapshots s
                     WHERE s.account_id = a.id ORDER BY s.date DESC, s.id DESC LIMIT 1) AS snapshot_balance,
                   (SELECT s.date FROM balance_snapshots s
                     WHERE s.account_id = a.id ORDER BY s.date DESC, s.id DESC LIMIT 1) AS snapshot_date,
                   (SELECT t.raw_balance FROM transactions t
                     WHERE t.account_id = a.id AND t.raw_balance IS NOT NULL
                     ORDER BY t.date DESC, t.id DESC LIMIT 1) AS txn_balance,
                   (SELECT t.date FROM transactions t
                     WHERE t.account_id = a.id AND t.raw_balance IS NOT NULL
                     ORDER BY t.date DESC, t.id DESC LIMIT 1) AS txn_balance_date,
                   (SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
                     WHERE t.account_id = a.id) AS amount_sum
            FROM accounts a
            WHERE a.active = 1
            ORDER BY a.source, a.name
            """
        ).fetchall()
    finally:
        conn.close()

    accounts = []
    for row in rows:
        # Prefer the freshest source: latest snapshot vs latest bank-provided
        # transaction balance; fall back to summed transaction amounts.
        balance = None
        as_of = None
        if row["snapshot_balance"] is not None and (
            row["txn_balance_date"] is None
            or (row["snapshot_date"] or "") >= (row["txn_balance_date"] or "")
        ):
            balance, as_of = row["snapshot_balance"], row["snapshot_date"]
        elif row["txn_balance"] is not None:
            balance, as_of = row["txn_balance"], row["txn_balance_date"]
        elif row["txn_count"]:
            balance = row["amount_sum"]

        accounts.append({
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "source": row["source"],
            "txn_count": row["txn_count"],
            "balance": balance,
            "as_of": as_of,
        })

    return render_template("accounts.html", accounts=accounts, error=None)


# --- Manual balance API routes ---


@accounts_bp.route("/api/manual-balances")
def api_manual_balances():
    """Return all manual balance entries for the history table."""
    entries = get_all_entries(get_data_dir())
    return jsonify(entries)


@accounts_bp.route("/api/manual-balance", methods=["POST"])
def api_save_manual_balance():
    """Save a new manual balance entry and refresh data."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    account = data.get("account", "").strip()
    entry_date = data.get("date", "").strip()
    balance = data.get("balance")

    if not account or not entry_date or balance is None:
        return jsonify({"status": "error", "message": "account, date, and balance are required"}), 400

    try:
        balance = float(balance)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "balance must be a number"}), 400

    try:
        save_balance_entry(get_data_dir(), account, entry_date, balance)
        refresh_data()
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.exception("Failed to save manual balance")
        return jsonify({"status": "error", "message": str(e)}), 500


@accounts_bp.route("/api/manual-balance", methods=["DELETE"])
def api_delete_manual_balance():
    """Delete a manual balance entry and refresh data."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    account = data.get("account", "").strip()
    entry_date = data.get("date", "").strip()

    if not account or not entry_date:
        return jsonify({"status": "error", "message": "account and date are required"}), 400

    try:
        deleted = delete_balance_entry(get_data_dir(), account, entry_date)
        if deleted:
            refresh_data()
            return jsonify({"status": "ok"})
        else:
            return jsonify({"status": "error", "message": "Entry not found"}), 404
    except Exception as e:
        logger.exception("Failed to delete manual balance")
        return jsonify({"status": "error", "message": str(e)}), 500


@accounts_bp.route("/api/manual-balance/bulk", methods=["POST"])
def api_bulk_manual_balance():
    """
    Bulk import manual balance entries.
    Accepts JSON: { "entries": [{"account": "...", "date": "YYYY-MM-DD", "balance": 123.45}, ...] }
    Or a text body parsed line-by-line: "account, date, balance" per line.
    """
    data = request.get_json()
    if not data or "entries" not in data:
        return jsonify({"status": "error", "message": "Expected JSON with 'entries' array"}), 400

    entries = data["entries"]
    if not isinstance(entries, list) or not entries:
        return jsonify({"status": "error", "message": "'entries' must be a non-empty array"}), 400

    saved = 0
    errors = []
    data_dir = get_data_dir()

    for i, entry in enumerate(entries):
        account = str(entry.get("account", "")).strip()
        entry_date = str(entry.get("date", "")).strip()
        balance = entry.get("balance")

        if not account or not entry_date:
            errors.append(f"Row {i + 1}: missing account or date")
            continue

        try:
            balance = float(balance)
        except (TypeError, ValueError):
            errors.append(f"Row {i + 1}: invalid balance '{balance}'")
            continue

        save_balance_entry(data_dir, account, entry_date, balance)
        saved += 1

    refresh_data()
    result = {"status": "ok", "saved": saved}
    if errors:
        result["errors"] = errors
    return jsonify(result)
