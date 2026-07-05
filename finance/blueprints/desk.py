"""
API endpoints for The Desk (conversational CFO), The Archive, and the
Dossier. Wave 1 is backend-only — the /desk and /archive pages arrive with
Wave 2; these contracts are what that UI renders.

Error conventions (per the briefing endpoints): 400 for bad input, 404 for
unknown ids, 429 when the daily advisor cap is hit, 503 for
unconfigured/no-data/API failures — never 500.
"""

from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, render_template, request

from finance import advisor, db
from finance.data_service import get_cache, get_db_connection

logger = logging.getLogger(__name__)

desk_bp = Blueprint("desk", __name__)

TITLE_MAX_CHARS = 60
INSIGHTS_PAGE_SIZE = 50


def _advisor_config():
    config = get_cache().get("config")
    return config.advisor if config is not None else None


def _conversation_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "model": row["model"],
        "intelligence": row["intelligence"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived": bool(row["archived"]),
    }


def _insight_dict(row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "source": row["source"],
        "text": row["text"],
        "model": row["model"],
        "conversation_id": row["conversation_id"],
    }


def _profile_entry_dict(row) -> dict:
    return {
        "id": row["id"],
        "section": row["section"],
        "text": row["text"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "active": bool(row["active"]),
    }


# --- Pages ---


@desk_bp.route("/desk")
def desk_page():
    """The Desk — conversational CFO chat page."""
    return render_template("desk.html")


@desk_bp.route("/archive")
def archive_page():
    """The Archive — dossier + permanent insight log."""
    return render_template("archive.html")


# --- Chat ---


@desk_bp.route("/api/chat", methods=["POST"])
def api_chat():
    """
    One Desk question -> one advisor turn.

    Body: {conversation_id?, message, model?, intelligence?}. Creates the
    conversation when conversation_id is absent (title = first 60 chars of
    the question). Model/intelligence default to the conversation's stored
    values (or config advisor.default_model / 'standard') and persist per
    conversation when supplied.
    """
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "bad_request", "message": "message is required"}), 400

    advisor_cfg = _advisor_config()
    if advisor_cfg is None or get_cache().get("df") is None:
        return jsonify(
            {
                "error": "no_data_loaded",
                "message": "No transaction data available. Configure CSV imports in config.yaml.",
            }
        ), 503
    if not advisor_cfg.enabled or not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify(
            {
                "error": "advisor_not_configured",
                "message": (
                    "The Desk needs an Anthropic API key. Add ANTHROPIC_API_KEY "
                    "to .env and set advisor.enabled in config.yaml."
                ),
            }
        ), 503

    conn = get_db_connection()
    try:
        conversation_id = data.get("conversation_id")
        conversation = None
        if conversation_id is not None:
            conversation = db.get_conversation(conn, conversation_id)
            if conversation is None:
                return jsonify(
                    {"error": "not_found", "message": "Unknown conversation_id"}
                ), 404

        model = data.get("model") or (
            conversation["model"] if conversation else advisor_cfg.default_model
        )
        intelligence = data.get("intelligence") or (
            conversation["intelligence"] if conversation else "standard"
        )
        if model not in advisor.VALID_MODELS:
            return jsonify(
                {
                    "error": "bad_model",
                    "message": f"model must be one of: {', '.join(advisor.VALID_MODELS)}",
                }
            ), 400
        if intelligence not in advisor.VALID_INTELLIGENCE:
            return jsonify(
                {
                    "error": "bad_intelligence",
                    "message": f"intelligence must be one of: {', '.join(advisor.VALID_INTELLIGENCE)}",
                }
            ), 400

        if conversation is None:
            conversation_id = db.create_conversation(
                conn, title=message[:TITLE_MAX_CHARS], model=model,
                intelligence=intelligence,
            )
        else:
            conversation_id = conversation["id"]
            # The picker persists per conversation.
            if (model, intelligence) != (conversation["model"], conversation["intelligence"]):
                db.update_conversation(
                    conn, conversation_id, model=model, intelligence=intelligence
                )

        try:
            result = advisor.answer(conn, conversation_id, message, model, intelligence)
        except advisor.AdvisorError as exc:
            status = 429 if exc.code == "daily_cap" else 503
            return jsonify({"error": exc.code, "message": exc.message}), status

        return jsonify({"conversation_id": conversation_id, **result})
    except Exception:
        logger.exception("Chat turn failed")
        return jsonify(
            {"error": "chat_failed", "message": "The Desk is unavailable. Retry."}
        ), 503
    finally:
        conn.close()


# --- Conversations ---


@desk_bp.route("/api/conversations")
def api_list_conversations():
    conn = get_db_connection()
    try:
        return jsonify(
            {"conversations": [_conversation_dict(r) for r in db.list_conversations(conn)]}
        )
    finally:
        conn.close()


@desk_bp.route("/api/conversations/<int:conversation_id>")
def api_get_conversation(conversation_id: int):
    """Conversation metadata + messages for render (display_text, not raw blocks)."""
    conn = get_db_connection()
    try:
        conversation = db.get_conversation(conn, conversation_id)
        if conversation is None:
            return jsonify({"error": "not_found", "message": "Unknown conversation"}), 404
        messages = [
            {
                "id": row["id"],
                "role": row["role"],
                "display_text": row["display_text"],
                "created_at": row["created_at"],
            }
            for row in db.list_chat_messages(conn, conversation_id)
            if row["display_text"]
        ]
        return jsonify({**_conversation_dict(conversation), "messages": messages})
    finally:
        conn.close()


@desk_bp.route("/api/conversations/<int:conversation_id>", methods=["PATCH"])
def api_rename_conversation(conversation_id: int):
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "bad_request", "message": "title is required"}), 400
    conn = get_db_connection()
    try:
        if not db.update_conversation(conn, conversation_id, title=title[:TITLE_MAX_CHARS * 2]):
            return jsonify({"error": "not_found", "message": "Unknown conversation"}), 404
        return jsonify({"status": "ok"})
    finally:
        conn.close()


@desk_bp.route("/api/conversations/<int:conversation_id>", methods=["DELETE"])
def api_delete_conversation(conversation_id: int):
    conn = get_db_connection()
    try:
        if not db.delete_conversation(conn, conversation_id):
            return jsonify({"error": "not_found", "message": "Unknown conversation"}), 404
        return jsonify({"status": "ok"})
    finally:
        conn.close()


# --- Insights (The Archive) ---


@desk_bp.route("/api/insights")
def api_list_insights():
    """
    Query params: source (briefing|chat), q (substring), page (1-based).
    Newest first, 50 per page.
    """
    source = request.args.get("source") or None
    if source and source not in ("briefing", "chat"):
        return jsonify(
            {"error": "bad_request", "message": "source must be 'briefing' or 'chat'"}
        ), 400
    query = request.args.get("q") or None
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    conn = get_db_connection()
    try:
        total = db.count_insights(conn, source=source, query=query)
        rows = db.list_insights(
            conn, source=source, query=query,
            limit=INSIGHTS_PAGE_SIZE, offset=(page - 1) * INSIGHTS_PAGE_SIZE,
        )
        return jsonify(
            {
                "insights": [_insight_dict(r) for r in rows],
                "total": total,
                "page": page,
                "page_size": INSIGHTS_PAGE_SIZE,
            }
        )
    finally:
        conn.close()


@desk_bp.route("/api/insights/<int:insight_id>", methods=["DELETE"])
def api_delete_insight(insight_id: int):
    conn = get_db_connection()
    try:
        if not db.delete_insight(conn, insight_id):
            return jsonify({"error": "not_found", "message": "Unknown insight"}), 404
        return jsonify({"status": "ok"})
    finally:
        conn.close()


# --- Profile (the Dossier) ---


@desk_bp.route("/api/profile")
def api_get_profile():
    """Active entries grouped by section (AI-added entries carry source='ai')."""
    conn = get_db_connection()
    try:
        sections = {section: [] for section in db.PROFILE_SECTIONS}
        for row in db.list_profile_entries(conn):
            sections[row["section"]].append(_profile_entry_dict(row))
        return jsonify(sections)
    finally:
        conn.close()


@desk_bp.route("/api/profile", methods=["POST"])
def api_add_profile_entry():
    data = request.get_json(silent=True) or {}
    section = data.get("section")
    text = str(data.get("text") or "").strip()
    if section not in db.PROFILE_SECTIONS:
        return jsonify(
            {
                "error": "bad_request",
                "message": f"section must be one of: {', '.join(db.PROFILE_SECTIONS)}",
            }
        ), 400
    if not text:
        return jsonify({"error": "bad_request", "message": "text is required"}), 400
    conn = get_db_connection()
    try:
        entry_id = db.insert_profile_entry(conn, section, text, source="user")
        return jsonify(_profile_entry_dict(db.get_profile_entry(conn, entry_id))), 201
    finally:
        conn.close()


@desk_bp.route("/api/profile/<int:entry_id>", methods=["PATCH"])
def api_update_profile_entry(entry_id: int):
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    active = data.get("active")
    if text is not None:
        text = str(text).strip()
        if not text:
            return jsonify({"error": "bad_request", "message": "text cannot be empty"}), 400
    if active is not None and not isinstance(active, bool):
        return jsonify({"error": "bad_request", "message": "active must be a boolean"}), 400
    if text is None and active is None:
        return jsonify(
            {"error": "bad_request", "message": "Provide text and/or active"}
        ), 400
    conn = get_db_connection()
    try:
        if not db.update_profile_entry(conn, entry_id, text=text, active=active):
            return jsonify({"error": "not_found", "message": "Unknown profile entry"}), 404
        return jsonify(_profile_entry_dict(db.get_profile_entry(conn, entry_id)))
    finally:
        conn.close()


@desk_bp.route("/api/profile/<int:entry_id>", methods=["DELETE"])
def api_delete_profile_entry(entry_id: int):
    """Delete = soft-deactivate (entries stay recoverable in the DB)."""
    conn = get_db_connection()
    try:
        entry = db.get_profile_entry(conn, entry_id)
        if entry is None:
            return jsonify({"error": "not_found", "message": "Unknown profile entry"}), 404
        db.update_profile_entry(conn, entry_id, active=False)
        return jsonify({"status": "ok"})
    finally:
        conn.close()
