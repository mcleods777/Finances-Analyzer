from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from finance import plaid_sync
from finance.data_service import get_db_connection

logger = logging.getLogger(__name__)

plaid_bp = Blueprint("plaid", __name__)

_NOT_CONFIGURED_MESSAGE = (
    "Plaid is not configured. Copy .env.example to .env and add your "
    "PLAID_CLIENT_ID and PLAID_SECRET."
)


@plaid_bp.route("/api/plaid/link-token", methods=["POST"])
def api_link_token():
    """Create a Plaid Link token so the browser can open the Link flow."""
    if not plaid_sync.is_configured():
        return jsonify({"status": "error", "message": _NOT_CONFIGURED_MESSAGE}), 400
    try:
        link_token = plaid_sync.create_link_token()
        return jsonify({"status": "ok", "link_token": link_token})
    except Exception as exc:
        logger.exception("Failed to create Plaid link token")
        return jsonify({"status": "error", "message": plaid_sync.describe_error(exc)}), 502


@plaid_bp.route("/api/plaid/exchange", methods=["POST"])
def api_exchange():
    """
    Exchange a Link public_token, store the item, create its accounts, and
    run an initial sync (best-effort — the item stays linked if sync fails).
    """
    if not plaid_sync.is_configured():
        return jsonify({"status": "error", "message": _NOT_CONFIGURED_MESSAGE}), 400

    data = request.get_json(silent=True) or {}
    public_token = str(data.get("public_token", "")).strip()
    if not public_token:
        return jsonify({"status": "error", "message": "public_token is required"}), 400
    institution_name = (str(data.get("institution_name", "")).strip() or None)

    try:
        result = plaid_sync.exchange_public_token(public_token, institution_name)
    except Exception as exc:
        logger.exception("Plaid public token exchange failed")
        return jsonify({"status": "error", "message": plaid_sync.describe_error(exc)}), 502

    # Initial sync so the new accounts show data right away; errors are stored
    # on the item and surfaced via /api/plaid/status, never raised here.
    sync_result = None
    conn = get_db_connection()
    try:
        item = conn.execute(
            "SELECT * FROM plaid_items WHERE item_id = ?", (result["item_id"],)
        ).fetchone()
        if item is not None:
            sync_result = plaid_sync.sync_item(conn, item)
    except Exception:
        logger.exception("Initial Plaid sync failed for item %s", result["item_id"])
    finally:
        conn.close()

    return jsonify({"status": "ok", "item": result, "sync": sync_result})


@plaid_bp.route("/api/plaid/sync", methods=["POST"])
def api_sync():
    """Sync one item (JSON: {"item_id": ...}) or all linked items."""
    if not plaid_sync.is_configured():
        return jsonify({"status": "error", "message": _NOT_CONFIGURED_MESSAGE}), 400

    data = request.get_json(silent=True) or {}
    item_id = str(data.get("item_id", "")).strip() or None

    conn = get_db_connection()
    try:
        if item_id is not None:
            item = conn.execute(
                "SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                return jsonify({"status": "error", "message": "Unknown item_id"}), 404
            results = [plaid_sync.sync_item(conn, item)]
        else:
            results = plaid_sync.sync_all(conn)
    except Exception as exc:
        logger.exception("Plaid sync failed")
        return jsonify({"status": "error", "message": plaid_sync.describe_error(exc)}), 502
    finally:
        conn.close()

    ok = all(r.get("error") is None for r in results)
    return jsonify({"status": "ok" if ok else "partial", "results": results})


@plaid_bp.route("/api/plaid/status")
def api_status():
    """Configuration state plus per-item institution/last-sync/error details."""
    configured = plaid_sync.is_configured()
    items: list[dict] = []
    try:
        conn = get_db_connection()
        try:
            items = plaid_sync.get_status(conn)
        finally:
            conn.close()
    except Exception:
        # Status must never crash the Accounts page.
        logger.exception("Failed to load Plaid status")
    return jsonify({"configured": configured, "items": items})
