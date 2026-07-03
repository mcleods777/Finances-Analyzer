from __future__ import annotations

import logging
import sqlite3

import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from finance import db
from finance.blueprints.rules import normalize_merchant
from finance.config_loader import load_config
from finance.data_service import get_cache, get_config_path, get_db_connection, refresh_data
from finance.importer import compute_dedup_hash, reapply_rules

logger = logging.getLogger(__name__)

transactions_bp = Blueprint("transactions", __name__)


def _normalize_uncategorized(rows: list[dict]) -> None:
    """
    pandas NaN survives DataFrame.to_dict(orient="records") as a bare
    float('nan'), which is truthy in Python -- so template guards like
    `{% if t.subcategory %}` render the literal string "nan" instead of
    falling through to the "Uncategorized" branch. Coerce a NaN subcategory
    to None in place so every render-layer check downstream (row chip,
    expanded detail panel) treats an uncategorized row as falsy, consistently.
    """
    for row in rows:
        if pd.isna(row.get("subcategory")):
            row["subcategory"] = None


def _attach_transaction_ids(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """
    Attach each row's real transactions.id by recomputing its dedup_hash
    (account_id + date + amount + description) and looking it up against the
    DB. The in-memory DataFrame contract (finance/db.py load_transactions_df)
    intentionally doesn't expose id, so we resolve it here instead of
    changing that shared loader.
    """
    account_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM accounts").fetchall()}
    hash_to_id = {
        r["dedup_hash"]: r["id"] for r in conn.execute("SELECT id, dedup_hash FROM transactions").fetchall()
    }
    for t in rows:
        account_id = account_ids.get(t.get("account_name"))
        if account_id is None:
            t["id"] = None
            continue
        date_val = t["date"]
        date_iso = date_val.strftime("%Y-%m-%d") if hasattr(date_val, "strftime") else str(date_val)
        dedup_hash = compute_dedup_hash(account_id, date_iso, t["amount"], t["description"])
        t["id"] = hash_to_id.get(dedup_hash)


@transactions_bp.route("/transactions")
def transactions():
    df = get_cache().get("df")
    if df is None:
        return render_template("transactions.html", transactions=[], error="No data loaded.")

    if df.empty:
        return render_template("transactions.html", transactions=[], error=None)

    # Filter by date if params present
    start_str = request.args.get("start")
    end_str = request.args.get("end")

    filtered_df = df.copy()

    if start_str:
        try:
            start_date = pd.to_datetime(start_str)
            filtered_df = filtered_df[filtered_df["date"] >= start_date]
        except Exception:
            pass

    if end_str:
        try:
            end_date = pd.to_datetime(end_str)
            filtered_df = filtered_df[filtered_df["date"] <= end_date]
        except Exception:
            pass

    # Filter by search term
    search_query = request.args.get("search", "").strip().lower()
    if search_query:
        # Use regex=False to handle special characters (like *) literally
        filtered_df = filtered_df[
            filtered_df["description"].str.lower().str.contains(search_query, na=False, regex=False)
        ]

    # Filter by status
    status_filter = request.args.get("status", "all")
    show_transfers = request.args.get("show_transfers", "0") == "1"
    if status_filter == "uncategorized":
        # Show items where subcategory is null/empty
        filtered_df = filtered_df[filtered_df["subcategory"].isna() | (filtered_df["subcategory"] == "")]
        if not show_transfers:
            filtered_df = filtered_df[filtered_df["category"] != "transfer"]
    elif status_filter == "categorized":
        filtered_df = filtered_df[filtered_df["subcategory"].notna() & (filtered_df["subcategory"] != "")]
        if not show_transfers:
            filtered_df = filtered_df[filtered_df["category"] != "transfer"]
    elif status_filter == "transfer":
        filtered_df = filtered_df[filtered_df["category"] == "transfer"]
    else:
        # "all" — hide transfers by default
        if not show_transfers:
            filtered_df = filtered_df[filtered_df["category"] != "transfer"]

    # Filter by category (supports single or multi-select via comma-separated values)
    category_filter = request.args.get("category", "")
    if category_filter:
        cat_list = [c.strip() for c in category_filter.split(",") if c.strip()]
        if len(cat_list) == 1 and cat_list[0] == "Uncategorized":
            filtered_df = filtered_df[filtered_df["subcategory"].isna() | (filtered_df["subcategory"] == "")]
        else:
            include_uncat = "Uncategorized" in cat_list
            named_cats = [c for c in cat_list if c != "Uncategorized"]
            mask = filtered_df["subcategory"].isin(named_cats) if named_cats else pd.Series(False, index=filtered_df.index)
            if include_uncat:
                mask = mask | filtered_df["subcategory"].isna() | (filtered_df["subcategory"] == "")
            filtered_df = filtered_df[mask]

    # Filter by account (supports multi-select via comma-separated values)
    account_filter = request.args.get("account", "")
    if account_filter:
        acct_list = [a.strip() for a in account_filter.split(",") if a.strip()]
        filtered_df = filtered_df[filtered_df["account_name"].isin(acct_list)]

    # Collect available subcategories for the filter dropdown (from full df, not filtered)
    all_subcategories = sorted(
        df[df["subcategory"].notna() & (df["subcategory"] != "")]
        ["subcategory"].unique().tolist()
    )

    # Collect available account names for the filter dropdown (from full df)
    all_accounts = sorted(df["account_name"].dropna().unique().tolist())

    # Sort
    sort_by = request.args.get("sort", "date_desc")
    sort_map = {
        "date_asc": ("date", True),
        "date_desc": ("date", False),
        "amount_asc": ("amount", True),
        "amount_desc": ("amount", False),
        "description_asc": ("description", True),
        "description_desc": ("description", False),
        "type_asc": ("category", True),
        "type_desc": ("category", False),
        "category_asc": ("subcategory", True),
        "category_desc": ("subcategory", False),
        "account_asc": ("account_name", True),
        "account_desc": ("account_name", False),
    }
    col, asc = sort_map.get(sort_by, ("date", False))
    # For subcategory sort, put nulls last
    if col == "subcategory":
        filtered_df = filtered_df.copy()
        filtered_df["_sort_key"] = filtered_df["subcategory"].fillna("zzz")
        transactions = filtered_df.sort_values("_sort_key", ascending=asc).drop(columns=["_sort_key"]).to_dict(orient="records")
    else:
        transactions = filtered_df.sort_values(col, ascending=asc).to_dict(orient="records")

    # Coerce NaN subcategory -> None so the template's truthiness checks
    # never render the literal string "nan" (see _normalize_uncategorized docstring)
    _normalize_uncategorized(transactions)

    # Attach real DB ids for inline/bulk category editing (see docstring above)
    conn = get_db_connection()
    try:
        _attach_transaction_ids(conn, transactions)
    finally:
        conn.close()

    # Match recurring bills
    config = load_config(get_config_path())
    recurring_bills = config.recurring_bills

    for t in transactions:
        desc = t["description"].lower()
        for bill in recurring_bills:
            for criterion in bill.match_criteria:
                if criterion in desc:
                    t["recurring_bill"] = bill.name
                    break
            if "recurring_bill" in t:
                break

    # Calculate uncategorized stats for sidebar (exclude transfers — they don't need subcategories)
    non_transfer_df = df[df["category"] != "transfer"]
    uncategorized_df = non_transfer_df[non_transfer_df["subcategory"].isna() | (non_transfer_df["subcategory"] == "")]
    uncategorized_count = len(uncategorized_df)

    common_uncategorized = []
    if not uncategorized_df.empty:
        from collections import Counter
        import re

        # Stop words to ignore (common banking terms & noise)
        STOP_WORDS = {
            "purchase", "pos", "card", "withdrawal", "payment", "transaction",
            "dept", "to", "from", "at", "on", "the", "and", "check", "debit",
            "credit", "dd", "tst", "atm", "recur", "recurring", "bill", "invoice",
            # Locations / States / Countries / Filler from Statements
            "des", "moines", "west", "east", "south", "north", "dsm", "iowa",
            "ia", "us", "usa", "inc", "llc", "ltd", "corp", "co", "com", "org",
            "net", "www", "sq", "square", "paypal", "stripe", "wdm", "clive",
            "urbandale", "waudkee", "carlisle", "altoona", "anken", "johnston",
            "ashworth", "university", "douglas", "euclid", "hickman",
            "road", "rd", "st", "street", "ave", "avenue", "dr", "drive", "blvd",
            "ln", "lane", "ct", "court", "cir", "circle", "hwy", "highway",
            "admin", "office", "parks", "recur", "recurring", "store", "dividends",
            "completed", "subs", "dhs", "cmsvend", "pilot", "ultimate", "aramark",
            # Noisy Truncations / Concatenations seen in data
            "iaus", "caus", "mous", "moin", "moinw", "xx", "xxx", "xxxx"
        }

        all_words = []
        for desc in uncategorized_df["description"]:
            # Tokenize: split by non-alphanumeric, lowercase
            # We specifically look for words with at least 3 letters to avoid noise
            tokens = re.findall(r'[a-zA-Z]{3,}', str(desc).lower())
            for token in tokens:
                if token not in STOP_WORDS:
                    all_words.append(token)

        # Get top 10 most frequent words
        counts = Counter(all_words).most_common(10)
        for word, count in counts:
            # Use title case for display
            common_uncategorized.append({"description": word.title(), "count": count})

    return render_template(
        "transactions.html",
        transactions=transactions,
        error=None,
        filter_start=start_str,
        filter_end=end_str,
        current_sort=sort_by,
        current_search=search_query,
        current_status=status_filter,
        current_category=category_filter,
        current_account=account_filter,
        show_transfers=show_transfers,
        uncategorized_count=uncategorized_count,
        common_uncategorized=common_uncategorized,
        all_subcategories=all_subcategories,
        all_accounts=all_accounts,
    )


# --- Category edit APIs (DB-backed) ---


@transactions_bp.route("/api/transactions/<int:txn_id>/category", methods=["POST"])
def api_set_transaction_category(txn_id: int):
    """
    Set a single transaction's category from the /transactions row edit UI.
    Marks user_edited=1 so rule reapplication never overwrites the edit.
    Body: { "category": "Coffee" }
    """
    data = request.get_json(silent=True) or {}
    category = str(data.get("category", "")).strip()
    if not category:
        return jsonify({"status": "error", "message": "category is required"}), 400

    try:
        conn = get_db_connection()
        try:
            exists = conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            if exists is None:
                return jsonify({"status": "error", "message": "Transaction not found"}), 404

            with conn:
                conn.execute(
                    "UPDATE transactions SET category = ?, user_edited = 1 WHERE id = ?",
                    (category, txn_id),
                )
        finally:
            conn.close()

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to set transaction category for id=%s", txn_id)
        return jsonify({"status": "error", "message": str(e)}), 500


@transactions_bp.route("/api/transactions/similar", methods=["GET"])
def api_transactions_similar():
    """
    Read-only preview for the "Categorize Similar" panel: case-insensitive
    substring match of `keyword` against the raw transaction description
    (the same matching apply_rules_to_description/categorize_row use), split
    into uncategorized / already-categorized counts plus how many of the
    matches are user_edited (and therefore protected from rule reapply).
    Excludes transfers and inactive accounts -- mirrors /api/uncategorized-groups.

    Query: ?keyword=amazon+mktpl
    Response: {total, uncategorized, categorized, user_edited, samples: [{date, description, amount}]}
    """
    keyword = request.args.get("keyword", "").strip().lower()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    try:
        conn = get_db_connection()
        try:
            # Escape LIKE wildcards so a keyword containing % or _ is matched
            # literally, as a substring, not as a pattern.
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                "SELECT t.date, t.description, t.amount, t.category, t.user_edited "
                "FROM transactions t JOIN accounts a ON a.id = t.account_id "
                "WHERE a.active = 1 AND t.txn_type != 'transfer' "
                "AND LOWER(t.description) LIKE ? ESCAPE '\\' "
                "ORDER BY t.date DESC",
                (f"%{escaped}%",),
            ).fetchall()
        finally:
            conn.close()

        total = len(rows)
        uncategorized = sum(1 for r in rows if not r["category"])
        categorized = total - uncategorized
        user_edited = sum(1 for r in rows if r["user_edited"])
        samples = [
            {"date": r["date"], "description": r["description"], "amount": r["amount"]}
            for r in rows[:5]
        ]

        return jsonify({
            "total": total,
            "uncategorized": uncategorized,
            "categorized": categorized,
            "user_edited": user_edited,
            "samples": samples,
        })

    except Exception as e:
        logger.exception("Failed to load similar-transaction preview")
        return jsonify({"error": str(e)}), 500


@transactions_bp.route("/api/transactions/bulk-category", methods=["POST"])
def api_bulk_set_category():
    """
    Set category on many transactions at once (row-checkbox bulk action).
    Marks user_edited=1 on all affected rows.
    Body: { "ids": [1, 2, 3], "category": "Groceries", "create_rule": false }

    create_rule=true additionally:
      (a) sets the selected ids' category + user_edited=1, exactly as above
      (b) derives the DISTINCT normalize_merchant() keywords from the
          selected transactions' descriptions
      (c) inserts one categorization_rules row per distinct keyword, skipping
          (and reporting) any keyword that already has a rule under ANY
          category -- never stacks a conflicting rule
      (d) reapply_rules() so other matching, non-user_edited transactions
          pick up the new rule(s) too
      (e) refresh_data()

    Response (create_rule=true adds rules_created/rules_skipped/reapplied):
      { "status": "ok", "updated": n, "rules_created": [...], "rules_skipped": [...], "reapplied": n }
    """
    data = request.get_json(silent=True) or {}
    category = str(data.get("category", "")).strip()
    raw_ids = data.get("ids", [])
    create_rule = bool(data.get("create_rule", False))

    if not category:
        return jsonify({"status": "error", "message": "category is required"}), 400
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"status": "error", "message": "ids is required"}), 400

    try:
        txn_ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "ids must be integers"}), 400

    try:
        conn = get_db_connection()
        try:
            placeholders = ",".join("?" * len(txn_ids))

            rules_created: list[str] = []
            rules_skipped: list[str] = []
            reapplied = 0
            keywords: list[str] = []

            if create_rule:
                desc_rows = conn.execute(
                    f"SELECT description FROM transactions WHERE id IN ({placeholders})",
                    txn_ids,
                ).fetchall()
                keywords = sorted({
                    kw for kw in (normalize_merchant(r["description"]) for r in desc_rows) if kw
                })

            with conn:
                cur = conn.execute(
                    f"UPDATE transactions SET category = ?, user_edited = 1 WHERE id IN ({placeholders})",
                    (category, *txn_ids),
                )
                updated = cur.rowcount

                if create_rule and keywords:
                    priority = db.next_rule_priority(conn)
                    conn.execute(
                        "UPDATE categorization_rules SET priority = ? WHERE category = ?",
                        (priority, category),
                    )
                    for keyword in keywords:
                        existing = conn.execute(
                            "SELECT category FROM categorization_rules WHERE keyword = ?",
                            (keyword,),
                        ).fetchone()
                        if existing is not None:
                            rules_skipped.append(keyword)
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO categorization_rules (category, keyword, priority) "
                            "VALUES (?, ?, ?)",
                            (category, keyword, priority),
                        )
                        rules_created.append(keyword)

            if create_rule:
                reapplied = reapply_rules(conn)
        finally:
            conn.close()

        refresh_data()
        response = {"status": "ok", "updated": updated}
        if create_rule:
            response["rules_created"] = rules_created
            response["rules_skipped"] = rules_skipped
            response["reapplied"] = reapplied
        return jsonify(response)

    except Exception as e:
        logger.exception("Failed to bulk set category")
        return jsonify({"status": "error", "message": str(e)}), 500
