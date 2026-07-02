from __future__ import annotations

import logging
import re

import pandas as pd
import yaml
from flask import Blueprint, jsonify, render_template, request

from finance import db
from finance.config_loader import load_config
from finance.data_service import get_cache, get_config_path, get_db_connection, refresh_data
from finance.importer import reapply_rules

logger = logging.getLogger(__name__)

rules_bp = Blueprint("rules", __name__)

_MERCHANT_TOKEN_RE = re.compile(r"[a-zA-Z]{3,}")


def normalize_merchant(description: str) -> str:
    """
    Normalize a transaction description into a merchant grouping key for the
    uncategorized review queue: lowercase alphabetic tokens of length >= 3,
    whitespace-joined. This naturally drops digits, dates, store/reference
    codes, and punctuation without needing bespoke strip rules, e.g.
    "AMAZON MKTPL*2K3J7" -> "amazon mktpl".
    """
    tokens = _MERCHANT_TOKEN_RE.findall(str(description).lower())
    return " ".join(tokens)


@rules_bp.route("/rules")
def rules_page():
    return render_template("rules.html")


@rules_bp.route("/categories")
def categories_page():
    df = get_cache().get("df")
    if df is None or df.empty:
        return render_template("categories.html", categories=[], error="No data loaded.")

    non_transfer_df = df[df["category"] != "transfer"].copy()

    categories = []

    # Group by subcategory
    grouped = non_transfer_df.groupby("subcategory", dropna=False)

    for subcat, group in grouped:
        cat_name = subcat if pd.notna(subcat) and subcat != "" else "Uncategorized"

        # Sort transactions by date descending
        sorted_group = group.sort_values("date", ascending=False)

        total_amount = float(sorted_group["amount"].sum())
        transaction_count = len(sorted_group)

        # Get the most recent transactions (limit to 10 for display)
        recent = sorted_group.head(10)
        tx_list = []
        for _, row in recent.iterrows():
            tx_list.append({
                "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"]),
                "description": row["description"],
                "amount": float(row["amount"]),
                "account": row.get("account_name", ""),
            })

        categories.append({
            "name": cat_name,
            "total_amount": round(total_amount, 2),
            "transaction_count": transaction_count,
            "recent_transactions": tx_list,
            "has_more": transaction_count > 10,
        })

    # Sort categories: Uncategorized last, then by transaction count descending
    categories.sort(key=lambda c: (c["name"] == "Uncategorized", -c["transaction_count"]))

    category_names = sorted(c["name"] for c in categories if c["name"] != "Uncategorized")

    return render_template(
        "categories.html", categories=categories, category_names=category_names, error=None
    )


# --- Uncategorized review queue API (DB-backed) ---


@rules_bp.route("/api/uncategorized-groups", methods=["GET"])
def api_uncategorized_groups():
    """
    Group uncategorized, non-user-edited transactions by normalized merchant
    for the review queue on /categories. Excludes transfers (they don't need
    subcategories, same convention as the /transactions uncategorized filter).
    """
    try:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT t.description, t.amount FROM transactions t "
                "JOIN accounts a ON a.id = t.account_id "
                "WHERE a.active = 1 AND t.user_edited = 0 "
                "AND (t.category IS NULL OR t.category = '') "
                "AND t.txn_type != 'transfer'"
            ).fetchall()
        finally:
            conn.close()

        groups: dict[str, dict] = {}
        for row in rows:
            key = normalize_merchant(row["description"])
            if not key:
                continue
            g = groups.setdefault(
                key, {"keyword": key, "count": 0, "total_amount": 0.0, "samples": []}
            )
            g["count"] += 1
            g["total_amount"] += float(row["amount"])
            if row["description"] not in g["samples"] and len(g["samples"]) < 5:
                g["samples"].append(row["description"])

        result = sorted(groups.values(), key=lambda g: -g["count"])
        for g in result:
            g["total_amount"] = round(g["total_amount"], 2)

        return jsonify({"groups": result})

    except Exception as e:
        logger.exception("Failed to load uncategorized groups")
        return jsonify({"error": str(e)}), 500


@rules_bp.route("/api/categorize-group", methods=["POST"])
def api_categorize_group():
    """
    Categorize a merchant group surfaced by the uncategorized review queue.
    Body: { "keyword": "amazon mktpl", "category": "Shopping", "create_rule": true }

    create_rule=true: inserts a categorization_rules row (bumped to the
    highest priority, mirroring api_save_rule) and reapplies rules DB-wide
    via reapply_rules() -- which already skips user_edited=1 rows -- so the
    new rule also catches future imports. user_edited stays 0 on affected
    rows since they were categorized by a rule, not a manual edit.

    create_rule=false: one-off bulk update. Only touches transactions that
    are still uncategorized and non-user-edited, matched by the normalized
    merchant containing the keyword (LIKE-style substring match on the
    normalized description, not the raw one). Sets user_edited=1 since
    there's no rule backing the categorization.
    """
    data = request.get_json(silent=True) or {}
    keyword = str(data.get("keyword", "")).strip().lower()
    category = str(data.get("category", "")).strip()
    create_rule = bool(data.get("create_rule", False))

    if not keyword or not category:
        return jsonify({"status": "error", "message": "keyword and category are required"}), 400

    try:
        conn = get_db_connection()
        try:
            if create_rule:
                with conn:
                    priority = db.next_rule_priority(conn)
                    conn.execute(
                        "UPDATE categorization_rules SET priority = ? WHERE category = ?",
                        (priority, category),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO categorization_rules (category, keyword, priority) "
                        "VALUES (?, ?, ?)",
                        (category, keyword, priority),
                    )
                updated = reapply_rules(conn)
            else:
                rows = conn.execute(
                    "SELECT id, description FROM transactions "
                    "WHERE user_edited = 0 AND (category IS NULL OR category = '')"
                ).fetchall()
                matching_ids = [
                    row["id"] for row in rows if keyword in normalize_merchant(row["description"])
                ]
                updated = 0
                if matching_ids:
                    placeholders = ",".join("?" * len(matching_ids))
                    with conn:
                        cur = conn.execute(
                            f"UPDATE transactions SET category = ?, user_edited = 1 "
                            f"WHERE id IN ({placeholders})",
                            (category, *matching_ids),
                        )
                        updated = cur.rowcount
        finally:
            conn.close()

        refresh_data()
        return jsonify({"status": "ok", "updated": updated})

    except Exception as e:
        logger.exception("Failed to categorize group")
        return jsonify({"status": "error", "message": str(e)}), 500


# --- Categorization rules API (DB-backed) ---


@rules_bp.route("/api/rules", methods=["GET"])
def api_get_rules():
    try:
        conn = get_db_connection()
        try:
            rules = db.rules_grouped_by_category(conn)
        finally:
            conn.close()
        return jsonify({"rules": rules})
    except Exception as e:
        logger.exception("Failed to load rules")
        return jsonify({"error": str(e)}), 500


@rules_bp.route("/api/rules", methods=["POST"])
def api_save_rule():
    """
    Save a new categorization rule.
    Body: { "keyword": "starbucks", "category": "Coffee" }
    OR
    Body: { "keywords": ["starbucks", "dunkin"], "category": "Coffee" }
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    category = data.get("category", "").strip()
    if not category:
        return jsonify({"status": "error", "message": "category is required"}), 400

    # Collect keywords: either single 'keyword' or list 'keywords'
    keywords = []
    if "keyword" in data and data["keyword"]:
        keywords.append(data["keyword"].strip().lower())
    if "keywords" in data and isinstance(data["keywords"], list):
        keywords.extend([k.strip().lower() for k in data["keywords"] if k])

    if not keywords:
        return jsonify({"status": "error", "message": "At least one keyword is required"}), 400

    try:
        conn = get_db_connection()
        try:
            with conn:
                # Bump the whole category to the highest priority (mirrors the
                # old YAML behavior of re-appending an edited rule at the end)
                priority = db.next_rule_priority(conn)
                conn.execute(
                    "UPDATE categorization_rules SET priority = ? WHERE category = ?",
                    (priority, category),
                )
                for keyword in keywords:
                    conn.execute(
                        "INSERT OR IGNORE INTO categorization_rules (category, keyword, priority) "
                        "VALUES (?, ?, ?)",
                        (category, keyword, priority),
                    )
            reapply_rules(conn)
        finally:
            conn.close()

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to save rule")
        return jsonify({"status": "error", "message": str(e)}), 500


@rules_bp.route("/api/rules/delete", methods=["POST"])
def api_delete_rule():
    """
    Delete a category entirely OR delete a specific keyword from a category.
    Body: { "category": "Rent", "keyword": "optional" }
    """
    data = request.get_json()
    category = data.get("category", "").strip()
    keyword_to_remove = data.get("keyword", "").strip().lower()

    if not category:
        return jsonify({"status": "error", "message": "category is required"}), 400

    try:
        conn = get_db_connection()
        try:
            exists = conn.execute(
                "SELECT 1 FROM categorization_rules WHERE category = ? LIMIT 1", (category,)
            ).fetchone()
            if exists is None:
                return jsonify({"status": "error", "message": "Category not found"}), 404

            with conn:
                if keyword_to_remove:
                    conn.execute(
                        "DELETE FROM categorization_rules WHERE category = ? AND keyword = ?",
                        (category, keyword_to_remove),
                    )
                else:
                    conn.execute(
                        "DELETE FROM categorization_rules WHERE category = ?", (category,)
                    )
            reapply_rules(conn)
        finally:
            conn.close()

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to delete rule")
        return jsonify({"status": "error", "message": str(e)}), 500


@rules_bp.route("/api/rules/rename", methods=["POST"])
def api_rename_rule():
    """
    Rename a category.
    Body: { "old_category": "Old", "new_category": "New" }
    """
    data = request.get_json()
    old_cat = data.get("old_category", "").strip()
    new_cat = data.get("new_category", "").strip()

    if not old_cat or not new_cat:
        return jsonify({"status": "error", "message": "Both old and new category names are required"}), 400

    try:
        conn = get_db_connection()
        try:
            exists = conn.execute(
                "SELECT 1 FROM categorization_rules WHERE category = ? LIMIT 1", (old_cat,)
            ).fetchone()
            if exists is None:
                return jsonify({"status": "error", "message": "Category not found"}), 404

            with conn:
                # Rename in place to preserve existing priorities
                conn.execute(
                    "UPDATE OR IGNORE categorization_rules SET category = ? WHERE category = ?",
                    (new_cat, old_cat),
                )
                # Drop any leftovers that collided with existing (new_cat, keyword) rows
                conn.execute(
                    "DELETE FROM categorization_rules WHERE category = ?", (old_cat,)
                )
            reapply_rules(conn)
        finally:
            conn.close()

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to rename rule")
        return jsonify({"status": "error", "message": str(e)}), 500


# --- Recurring Bills API (stays YAML-backed by design) ---


@rules_bp.route("/api/recurring-bills", methods=["GET"])
def api_get_recurring_bills():
    config = get_cache().get("config")
    if not config:
        # Try reloading if cache empty
        config = load_config(get_config_path())

    bills = []
    for b in config.recurring_bills:
        bills.append({
            "name": b.name,
            "amount": b.amount,
            "day_of_month": b.day_of_month,
            "match_criteria": b.match_criteria
        })
    return jsonify(bills)


@rules_bp.route("/api/recurring-bills", methods=["POST"])
def api_save_recurring_bill():
    """
    Add or Update a recurring bill.
    Body: { "name": "Netflix", "amount": 15.99, "day_of_month": 15, "match_criteria": ["netflix"] }
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    name = data.get("name", "").strip()
    try:
        amount = float(data.get("amount", 0))
        day = int(data.get("day_of_month", 0))
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid amount or day"}), 400

    match_criteria = data.get("match_criteria", [])
    if not isinstance(match_criteria, list):
        match_criteria = [str(match_criteria)]

    # Clean criteria
    match_criteria = [c.strip().lower() for c in match_criteria if c and c.strip()]

    if not name or not amount or not day:
        return jsonify({"status": "error", "message": "Name, amount, and day are required"}), 400

    try:
        config_path = get_config_path()

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if "recurring_bills" not in config_data or not config_data["recurring_bills"]:
            config_data["recurring_bills"] = []

        # Check if bill exists (by name) -> Update
        found = False
        for bill in config_data["recurring_bills"]:
            if bill["name"] == name:
                bill["amount"] = amount
                bill["day_of_month"] = day
                # Merge criteria? Or overwrite? Let's overwrite for now as it's cleaner in UI
                bill["match_criteria"] = match_criteria
                found = True
                break

        if not found:
            config_data["recurring_bills"].append({
                "name": name,
                "amount": amount,
                "day_of_month": day,
                "match_criteria": match_criteria
            })

        # Write back
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, sort_keys=False)

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to save recurring bill")
        return jsonify({"status": "error", "message": str(e)}), 500


@rules_bp.route("/api/recurring-bills", methods=["DELETE"])
def api_delete_recurring_bill():
    """
    Delete a recurring bill.
    Body: { "name": "Netflix" }
    """
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"status": "error", "message": "Name is required"}), 400

    name = data["name"].strip()

    try:
        config_path = get_config_path()

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if "recurring_bills" in config_data:
            original_len = len(config_data["recurring_bills"])
            config_data["recurring_bills"] = [
                b for b in config_data["recurring_bills"] if b["name"] != name
            ]

            if len(config_data["recurring_bills"]) < original_len:
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(config_data, f, sort_keys=False)
                refresh_data()

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to delete recurring bill")
        return jsonify({"status": "error", "message": str(e)}), 500
