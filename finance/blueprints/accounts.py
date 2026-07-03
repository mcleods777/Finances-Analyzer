from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request
from werkzeug.utils import secure_filename

from finance import account_ops, csv_reader, db, importer
from finance.config_loader import load_config
from finance.data_service import get_config_path, get_data_dir, get_db_connection, refresh_data
from finance.manual_balances import (
    delete_balance_entry,
    get_all_entries,
    save_balance_entry,
)

logger = logging.getLogger(__name__)

accounts_bp = Blueprint("accounts", __name__)

# --- Upload constants ---

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # ~10MB
UPLOAD_SUBDIR = "uploads"
VALID_ACCOUNT_TYPES = {"checking", "savings", "credit_card", "credit", "investment", "loan"}
# UI-facing alias -> internal account.type stored in the DB
_TYPE_ALIASES = {"credit": "credit_card"}
# Types accepted when editing an existing account (manual_balance rows exist
# from the manual-balances migration and must stay editable).
EDITABLE_ACCOUNT_TYPES = VALID_ACCOUNT_TYPES | {"manual_balance"}


@accounts_bp.route("/accounts")
def accounts_page():
    """
    Accounts overview: every DB account with its latest known balance,
    plus per-account upload capability (accounts with a stored column
    mapping) consumed by partials/upload_section.html.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.name, a.type, a.source, a.column_mapping,
                   a.hidden, a.exclude_from_net_worth, a.institution,
                   a.plaid_account_id, a.plaid_item_id,
                   (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS txn_count,
                   (SELECT COUNT(*) FROM balance_snapshots s WHERE s.account_id = a.id) AS snapshot_count,
                   (SELECT COUNT(*) FROM imports i WHERE i.account_id = a.id) AS import_count,
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
            "institution": row["institution"],
            "hidden": bool(row["hidden"]),
            "exclude_from_net_worth": bool(row["exclude_from_net_worth"]),
            "is_plaid_linked": row["plaid_account_id"] is not None,
            "txn_count": row["txn_count"],
            "snapshot_count": row["snapshot_count"],
            "import_count": row["import_count"],
            "balance": balance,
            "as_of": as_of,
            "has_mapping": row["column_mapping"] is not None,
        })

    visible_accounts = [a for a in accounts if not a["hidden"]]
    hidden_accounts = [a for a in accounts if a["hidden"]]
    return render_template(
        "accounts.html",
        accounts=visible_accounts,
        hidden_accounts=hidden_accounts,
        error=None,
    )


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


# --- CSV upload API routes ---


def _uploads_dir() -> str:
    path = os.path.join(get_data_dir(), UPLOAD_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _read_upload_file(file_storage) -> tuple[bytes, str]:
    """
    Read and size-check an uploaded file. Returns (raw_bytes, safe_filename).
    Raises ValueError on missing/empty/oversized files.
    """
    filename = secure_filename(file_storage.filename or "") or "upload.csv"
    raw = file_storage.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File too large ({len(raw)} bytes); max upload size is {MAX_UPLOAD_BYTES} bytes"
        )
    if not raw.strip():
        raise ValueError("Uploaded file is empty")
    return raw, filename


def _save_upload_copy(raw: bytes, filename: str) -> str:
    """Persist a raw copy of an uploaded file under data/uploads/ for audit."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest_path = os.path.join(_uploads_dir(), f"{ts}_{filename}")
    with open(dest_path, "wb") as f:
        f.write(raw)
    return dest_path


def _get_uploaded_file():
    """Pull the 'file' field out of a multipart request. Returns (FileStorage, error_response)."""
    if "file" not in request.files:
        return None, (jsonify({"error": "No file part named 'file' in the request"}), 400)
    file_storage = request.files["file"]
    if not file_storage or not file_storage.filename:
        return None, (jsonify({"error": "No file selected"}), 400)
    return file_storage, None


@accounts_bp.route("/api/accounts/<int:account_id>/upload", methods=["POST"])
def api_upload_to_account(account_id: int):
    """
    Upload a CSV of transactions to an existing account. Parses with the
    account's stored column mapping, normalizes (finance.csv_reader),
    imports via finance.importer (dedup), saves a raw copy under
    data/uploads/ for audit, and refreshes the analytics cache.

    Response: {"imported": int, "duplicates": int, "errors": [str, ...]}
    """
    file_storage, err = _get_uploaded_file()
    if err:
        return err

    try:
        raw, filename = _read_upload_file(file_storage)
    except ValueError as e:
        return jsonify({"error": str(e), "errors": [str(e)]}), 400

    conn = get_db_connection()
    try:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account is None:
            return jsonify({"error": f"Account {account_id} not found"}), 404
        if not account["column_mapping"]:
            msg = "Account has no stored column mapping; recreate it via the Add Account flow"
            return jsonify({"error": msg, "errors": [msg]}), 400

        try:
            mapping = json.loads(account["column_mapping"])
        except (TypeError, json.JSONDecodeError):
            msg = "Account has a corrupt column mapping"
            return jsonify({"error": msg, "errors": [msg]}), 400

        cols = mapping.get("columns", {})
        try:
            df = csv_reader.read_csv_any_encoding(io.BytesIO(raw))
            normalized = csv_reader.normalize_dataframe(
                df,
                date_col=cols.get("date"),
                description_col=cols.get("description"),
                amount_col=cols.get("amount"),
                debit_col=cols.get("debit"),
                credit_col=cols.get("credit"),
                date_format=mapping.get("date_format"),
                amount_sign=mapping.get("amount_sign", "standard"),
                balance_col=mapping.get("balance_column"),
            )
        except Exception as e:
            logger.warning("Malformed upload for account %s (%s): %s", account_id, filename, e)
            msg = f"Could not parse CSV: {e}"
            return jsonify({"error": msg, "errors": [msg]}), 400

        if normalized.empty:
            msg = "No valid rows found in file"
            return jsonify({"error": msg, "errors": [msg]}), 400

        config = load_config(get_config_path())
        result = importer.import_rows(
            conn, account_id, normalized, config.classification,
            filename=filename, source="csv", record_empty_audit=True,
        )
        _save_upload_copy(raw, filename)
    finally:
        conn.close()

    refresh_data()
    return jsonify({"imported": result.imported, "duplicates": result.duplicates, "errors": []})


@accounts_bp.route("/api/accounts/preview", methods=["POST"])
def api_accounts_preview():
    """
    Parse an uploaded CSV without importing it, for the "Add account" flow.
    Returns detected columns, a small row preview, and a best-guess column +
    date-format mapping for the user to confirm/adjust before submitting.
    """
    file_storage, err = _get_uploaded_file()
    if err:
        return err

    try:
        raw, filename = _read_upload_file(file_storage)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        preview = csv_reader.preview_csv(io.BytesIO(raw))
    except Exception as e:
        return jsonify({"error": f"Could not parse CSV: {e}"}), 400

    preview["filename"] = filename
    return jsonify(preview)


@accounts_bp.route("/api/accounts", methods=["POST"])
def api_create_account():
    """
    Create a new CSV-backed account from an uploaded file plus a confirmed
    column mapping, then import the file through the same normalize+dedup
    path as the existing-account upload. Multipart form fields:
      file, name, type, date_col, description_col,
      amount_col OR (debit_col + credit_col), date_format (optional),
      amount_sign ('standard'/'inverted'), balance_col (optional)

    Response: {"account_id", "name", "imported", "duplicates", "errors": []}
    """
    file_storage, err = _get_uploaded_file()
    if err:
        return err

    form = request.form
    name = (form.get("name") or "").strip()
    account_type = (form.get("type") or "").strip()
    date_col = (form.get("date_col") or "").strip() or None
    description_col = (form.get("description_col") or "").strip() or None
    amount_col = (form.get("amount_col") or "").strip() or None
    debit_col = (form.get("debit_col") or "").strip() or None
    credit_col = (form.get("credit_col") or "").strip() or None
    date_format = (form.get("date_format") or "").strip() or None
    amount_sign = (form.get("amount_sign") or "standard").strip() or "standard"
    balance_col = (form.get("balance_col") or "").strip() or None

    field_errors = []
    if not name:
        field_errors.append("Account name is required")
    if account_type not in VALID_ACCOUNT_TYPES:
        field_errors.append(f"Account type must be one of: {', '.join(sorted(VALID_ACCOUNT_TYPES))}")
    if not date_col or not description_col:
        field_errors.append("Date and description columns are required")
    if not amount_col and not (debit_col and credit_col):
        field_errors.append("Provide either an amount column or both debit and credit columns")
    if amount_sign not in ("standard", "inverted"):
        field_errors.append("amount_sign must be 'standard' or 'inverted'")
    if field_errors:
        return jsonify({"error": "; ".join(field_errors), "errors": field_errors}), 400

    account_type = _TYPE_ALIASES.get(account_type, account_type)

    try:
        raw, filename = _read_upload_file(file_storage)
    except ValueError as e:
        return jsonify({"error": str(e), "errors": [str(e)]}), 400

    try:
        df = csv_reader.read_csv_any_encoding(io.BytesIO(raw))
        normalized = csv_reader.normalize_dataframe(
            df,
            date_col=date_col,
            description_col=description_col,
            amount_col=amount_col,
            debit_col=debit_col,
            credit_col=credit_col,
            date_format=date_format,
            amount_sign=amount_sign,
            balance_col=balance_col,
        )
    except Exception as e:
        logger.warning("Malformed new-account upload (%s): %s", filename, e)
        msg = f"Could not parse CSV: {e}"
        return jsonify({"error": msg, "errors": [msg]}), 400

    if normalized.empty:
        msg = "No valid rows found in file"
        return jsonify({"error": msg, "errors": [msg]}), 400

    mapping_json = json.dumps({
        "columns": {
            "date": date_col,
            "description": description_col,
            "amount": amount_col,
            "debit": debit_col,
            "credit": credit_col,
        },
        "date_format": date_format,
        "amount_sign": amount_sign,
        "balance_column": balance_col,
    })

    conn = get_db_connection()
    try:
        if db.get_account_by_name(conn, name) is not None:
            msg = f"An account named '{name}' already exists"
            return jsonify({"error": msg, "errors": [msg]}), 400

        account_id = db.upsert_account(
            conn, name=name, account_type=account_type, source="csv", column_mapping=mapping_json,
        )
        conn.commit()

        config = load_config(get_config_path())
        result = importer.import_rows(
            conn, account_id, normalized, config.classification,
            filename=filename, source="csv", record_empty_audit=True,
        )
        _save_upload_copy(raw, filename)
    finally:
        conn.close()

    refresh_data()
    return jsonify({
        "account_id": account_id,
        "name": name,
        "imported": result.imported,
        "duplicates": result.duplicates,
        "errors": [],
    }), 201


# --- Account management API routes (edit / hide / merge / delete) ---


def _account_response(row) -> dict:
    """Public JSON shape for one accounts row."""
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "source": row["source"],
        "institution": row["institution"],
        "hidden": bool(row["hidden"]),
        "exclude_from_net_worth": bool(row["exclude_from_net_worth"]),
        "is_plaid_linked": row["plaid_account_id"] is not None,
    }


def _parse_account_patch(data: dict, conn, account) -> tuple[dict, list[str]]:
    """Validate a PATCH body against one account. Returns (updates, errors)."""
    updates: dict = {}
    errors: list[str] = []

    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name:
            errors.append("Account name cannot be empty")
        elif name != account["name"]:
            existing = db.get_account_by_name(conn, name)
            if existing is not None and existing["id"] != account["id"]:
                errors.append(f"An account named '{name}' already exists")
            else:
                updates["name"] = name

    if "type" in data:
        account_type = _TYPE_ALIASES.get(str(data["type"] or "").strip(),
                                         str(data["type"] or "").strip())
        if account_type not in EDITABLE_ACCOUNT_TYPES:
            errors.append(
                f"Account type must be one of: {', '.join(sorted(EDITABLE_ACCOUNT_TYPES))}"
            )
        else:
            updates["type"] = account_type

    if "institution" in data:
        institution = data["institution"]
        updates["institution"] = (str(institution).strip() or None) if institution is not None else None

    for flag in ("hidden", "exclude_from_net_worth"):
        if flag in data:
            value = data[flag]
            if not isinstance(value, bool):
                errors.append(f"{flag} must be true or false")
            else:
                updates[flag] = 1 if value else 0

    return updates, errors


@accounts_bp.route("/api/accounts/<int:account_id>", methods=["PATCH"])
def api_update_account(account_id: int):
    """
    Edit an account: {name?, type?, institution?, hidden?, exclude_from_net_worth?}.
    Renames survive restarts (the startup migration matches config accounts by
    their config-file identity, not by name). Returns the updated account.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    conn = get_db_connection()
    try:
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            return jsonify({"error": f"Account {account_id} not found"}), 404

        updates, errors = _parse_account_patch(data, conn, account)
        if errors:
            return jsonify({"error": "; ".join(errors), "errors": errors}), 400

        if updates:
            assignments = ", ".join(f"{col} = ?" for col in updates)
            with conn:
                conn.execute(
                    f"UPDATE accounts SET {assignments} WHERE id = ?",
                    (*updates.values(), account_id),
                )
        updated = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    finally:
        conn.close()

    refresh_data()
    return jsonify(_account_response(updated))


def _merge_ids_or_error(account_id: int):
    """Parse {target_id} from the request body. Returns (target_id, error_response)."""
    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get("target_id"))
    except (TypeError, ValueError):
        return None, (jsonify({"error": "target_id is required"}), 400)
    if target_id == account_id:
        return None, (jsonify({"error": "Cannot merge an account into itself"}), 400)
    return target_id, None


@accounts_bp.route("/api/accounts/<int:account_id>/merge-preview", methods=["POST"])
def api_merge_preview(account_id: int):
    """
    Dry-run for merging this account into {target_id}:
    {moving, overlaps, snapshots_moving, sample_overlaps: [up to 5]}.
    """
    target_id, err = _merge_ids_or_error(account_id)
    if err:
        return err

    conn = get_db_connection()
    try:
        preview = account_ops.merge_preview(conn, account_id, target_id)
    except account_ops.AccountNotFound as e:
        return jsonify({"error": str(e)}), 404
    except account_ops.AccountOpsError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
    return jsonify(preview)


@accounts_bp.route("/api/accounts/<int:account_id>/merge", methods=["POST"])
def api_merge_account(account_id: int):
    """
    Merge this account into {target_id} atomically.
    Returns {moved, duplicates_skipped, snapshots_moved}.
    """
    target_id, err = _merge_ids_or_error(account_id)
    if err:
        return err

    conn = get_db_connection()
    try:
        result = account_ops.merge_accounts(conn, account_id, target_id)
    except account_ops.AccountNotFound as e:
        return jsonify({"error": str(e)}), 404
    except account_ops.AccountOpsError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

    refresh_data()
    return jsonify(result)


@accounts_bp.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def api_delete_account(account_id: int):
    """
    Delete an account and all its data. Requires JSON {"confirm": true}.
    Response includes deleted counts; if the account was the last one on a
    Plaid item, the item is removed too and reported as unlinked_item.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": "Deletion requires JSON body {\"confirm\": true}"}), 400

    conn = get_db_connection()
    try:
        result = account_ops.delete_account(conn, account_id)
    except account_ops.AccountNotFound as e:
        return jsonify({"error": str(e)}), 404
    finally:
        conn.close()

    refresh_data()
    return jsonify(result)


@accounts_bp.route("/api/accounts/<int:account_id>/imports")
def api_account_imports(account_id: int):
    """Recent import audit rows for one account (most recent first)."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, filename, imported_at, row_count, duplicate_count "
            "FROM imports WHERE account_id = ? ORDER BY imported_at DESC, id DESC LIMIT 20",
            (account_id,),
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(row) for row in rows])
