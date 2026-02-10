from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, render_template, request

from finance.analytics import (
    biweekly_income,
    biweekly_spending,
    compute_runway,
    get_recurring_bill_status,
    get_spending_breakdown,
    spending_averages,
    summary_statistics,
)
from finance.config_loader import load_config, validate_config
from finance.csv_reader import read_all_accounts
from finance.data_processor import (
    classify_transactions,
    compute_daily_balances,
    compute_net_worth_series,
)
from finance.manual_balances import (
    delete_balance_entry,
    get_all_entries,
    get_manual_account_names,
    load_manual_balances,
    save_balance_entry,
    save_bulk_entries,
)

logger = logging.getLogger(__name__)

# In-memory cache for computed data
_cache: dict = {}


def _get_base_dir() -> str:
    """Get the project base directory (where app.py lives)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_data_dir() -> str:
    return os.path.join(_get_base_dir(), "data")


def _get_config_path() -> str:
    return os.path.join(_get_base_dir(), "config.yaml")


def refresh_data() -> dict:
    """Reload all CSVs + manual balances, reprocess, recompute analytics, populate cache."""
    base_dir = _get_base_dir()
    config_path = os.path.join(base_dir, "config.yaml")
    data_dir = _get_data_dir()

    config = load_config(config_path)

    issues = validate_config(config, data_dir)
    if issues:
        for issue in issues:
            logger.warning("Config issue: %s", issue)

    # Read CSV transaction data
    df = read_all_accounts(config, data_dir)

    # Read manual balance snapshots
    manual_df = load_manual_balances(data_dir)

    if df.empty and manual_df.empty:
        _cache.clear()
        _cache["error"] = "No data found. Place CSV files in data/ or log manual balances."
        return _cache

    # Classify transactions (only applies to CSV-based data)
    if not df.empty:
        df = classify_transactions(df, config)

    # Compute daily balances (merges CSV + manual accounts)
    daily_bal = compute_daily_balances(df, config, manual_df)
    nw_series = compute_net_worth_series(daily_bal)

    # Analytics (spending/income is only from CSV transactions, not manual accounts)
    biweekly_df = biweekly_spending(df, config.pay_period) if not df.empty else None
    biweekly_inc_df = biweekly_income(df, config.pay_period) if not df.empty else None
    avg_stats = spending_averages(biweekly_df) if biweekly_df is not None else {
        "overall_average": 0,
        "median": 0,
        "std_dev": 0,
        "rolling_average": [],
    }

    # Current balance for runway — only spendable accounts (exclude manual/investment)
    # Manual balance accounts (brokerage, 401k, etc.) are long-term holdings,
    # not money you spend biweekly.
    RUNWAY_EXCLUDE_TYPES = {"manual_balance", "investment"}
    if not daily_bal.empty:
        latest = daily_bal.groupby("account_name").last()
        current_balance = 0
        for _, row in latest.iterrows():
            if row["account_type"] in RUNWAY_EXCLUDE_TYPES:
                continue  # Skip — not spendable cash
            elif row["account_type"] in ("credit_card", "loan"):
                current_balance -= abs(row["balance"])
            else:
                current_balance += row["balance"]
    else:
        current_balance = 0

    runway = compute_runway(current_balance, avg_stats["overall_average"], config.pay_period)
    
    # Calculate recurring bills status and impact on runway
    recurring_status = get_recurring_bill_status(df, config.pay_period, config.recurring_bills)
    pending_total = sum(b['amount'] for b in recurring_status if b['status'] == 'pending')
    
    runway["pending_bills_total"] = round(pending_total, 2)
    runway["free_cash"] = round(runway.get("budget_remaining_this_period", 0) - pending_total, 2)
    runway["recurring_bills"] = recurring_status

    summary = summary_statistics(
        df if not df.empty else _empty_classified_df(),
        nw_series,
        biweekly_df if biweekly_df is not None else _empty_biweekly_df(),
    )

    _cache.clear()
    _cache["df"] = df
    _cache["daily_balances"] = daily_bal
    _cache["net_worth_series"] = nw_series
    _cache["biweekly_df"] = biweekly_df
    _cache["biweekly_income_df"] = biweekly_inc_df
    _cache["avg_stats"] = avg_stats
    _cache["runway"] = runway
    _cache["summary"] = summary
    _cache["config"] = config

    return _cache


def _empty_classified_df():
    import pandas as pd
    return pd.DataFrame(
        columns=["date", "description", "amount", "account_name", "account_type", "raw_balance", "category", "subcategory"]
    )


def _empty_biweekly_df():
    import pandas as pd
    return pd.DataFrame(
        columns=["period_start", "period_end", "total_spending", "transaction_count"]
    )


def register_routes(app: Flask) -> None:
    """Register all Flask routes."""

    @app.template_filter("currency")
    def currency_filter(value):
        if value is None:
            return "N/A"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return str(value)
        if value < 0:
            return f"-${abs(value):,.2f}"
        return f"${value:,.2f}"

    @app.route("/")
    def dashboard():
        if "error" in _cache:
            return render_template(
                "dashboard.html", summary=None, runway=None,
                manual_accounts=[], error=_cache["error"],
            )
        summary = _cache.get("summary", {})
        runway = _cache.get("runway", {})
        manual_accounts = get_manual_account_names(_get_data_dir())
        return render_template(
            "dashboard.html", summary=summary, runway=runway,
            manual_accounts=manual_accounts, error=None,
        )

    @app.route("/transactions")
    def transactions():
        df = _cache.get("df")
        if df is None:
             return render_template("transactions.html", transactions=[], error="No data loaded.")
        
        if df.empty:
             return render_template("transactions.html", transactions=[], error=None)

        # Filter by date if params present
        start_str = request.args.get("start")
        end_str = request.args.get("end")
        
        filtered_df = df.copy()

        if start_str:
            import pandas as pd
            try:
                start_date = pd.to_datetime(start_str)
                filtered_df = filtered_df[filtered_df["date"] >= start_date]
            except Exception:
                pass # Ignore invalid dates
        
        if end_str:
            import pandas as pd
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

        # Filter by category (for pie chart drill-down)
        category_filter = request.args.get("category")
        if category_filter:
            if category_filter == "Uncategorized":
                filtered_df = filtered_df[filtered_df["subcategory"].isna() | (filtered_df["subcategory"] == "")]
            else:
                 filtered_df = filtered_df[filtered_df["subcategory"] == category_filter]

        # Sort by date desc
        sort_by = request.args.get("sort", "date_desc")
        
        if sort_by == "date_asc":
            transactions = filtered_df.sort_values("date", ascending=True).to_dict(orient="records")
        elif sort_by == "amount_desc":
            transactions = filtered_df.sort_values("amount", ascending=False).to_dict(orient="records")
        elif sort_by == "amount_asc":
            transactions = filtered_df.sort_values("amount", ascending=True).to_dict(orient="records")
        elif sort_by == "description_asc":
            transactions = filtered_df.sort_values("description", ascending=True).to_dict(orient="records")
        elif sort_by == "description_desc":
            transactions = filtered_df.sort_values("description", ascending=False).to_dict(orient="records")
        else:
            # Default: date_desc
            transactions = filtered_df.sort_values("date", ascending=False).to_dict(orient="records")
        
        # Match recurring bills
        config = load_config(_get_config_path())
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
            show_transfers=show_transfers,
            uncategorized_count=uncategorized_count,
            common_uncategorized=common_uncategorized
        )

    @app.route("/api/net-worth")
    def api_net_worth():
        nw = _cache.get("net_worth_series")
        if nw is None or nw.empty:
            return jsonify({"labels": [], "datasets": []})

        labels = [d.strftime("%Y-%m-%d") for d in nw["date"]]

        datasets = [
            {
                "label": "Net Worth",
                "data": [round(float(v), 2) for v in nw["net_worth"]],
            }
        ]

        # Add per-account lines
        for col in nw.columns:
            if col in ("date", "net_worth"):
                continue
            datasets.append(
                {
                    "label": col,
                    "data": [round(float(v), 2) for v in nw[col]],
                }
            )

        return jsonify({"labels": labels, "datasets": datasets})

    @app.route("/api/biweekly-spending")
    def api_biweekly_spending():
        bw = _cache.get("biweekly_df")
        avg = _cache.get("avg_stats", {})

        if bw is None or bw.empty:
            return jsonify({"labels": [], "spending": [], "average": 0, "rolling_average": []})

        labels = []
        period_starts = []
        period_ends = []
        
        for _, row in bw.iterrows():
            start = row["period_start"]
            end = row["period_end"]
            labels.append(f"{start.strftime('%b %d')}-{end.strftime('%b %d')}")
            period_starts.append(start.strftime("%Y-%m-%d"))
            period_ends.append(end.strftime("%Y-%m-%d"))

        return jsonify(
            {
                "labels": labels,
                "period_starts": period_starts,
                "period_ends": period_ends,
                "spending": [round(float(v), 2) for v in bw["total_spending"]],
                "average": avg.get("overall_average", 0),
                "rolling_average": avg.get("rolling_average", []),
            }
        )

    @app.route("/api/biweekly-income")
    def api_biweekly_income():
        bw = _cache.get("biweekly_income_df")

        if bw is None or bw.empty:
            return jsonify({"labels": [], "income": [], "period_starts": [], "period_ends": []})

        labels = []
        period_starts = []
        period_ends = []

        for _, row in bw.iterrows():
            start = row["period_start"]
            end = row["period_end"]
            labels.append(f"{start.strftime('%b %d')}-{end.strftime('%b %d')}")
            period_starts.append(start.strftime("%Y-%m-%d"))
            period_ends.append(end.strftime("%Y-%m-%d"))

        return jsonify(
            {
                "labels": labels,
                "period_starts": period_starts,
                "period_ends": period_ends,
                "income": [round(float(v), 2) for v in bw["total_income"]],
            }
        )

    @app.route("/api/runway")
    def api_runway():
        return jsonify(_cache.get("runway", {}))

    @app.route("/api/summary")
    def api_summary():
        return jsonify(_cache.get("summary", {}))

    @app.route("/api/spending-breakdown")
    def api_spending_breakdown():
        days_param = request.args.get("days", "30")
        try:
            days = int(days_param)
        except ValueError:
            days = 30
        
        df = _cache.get("df")
        if df is None:
            return jsonify([])
            
        breakdown = get_spending_breakdown(df, days)
        return jsonify(breakdown)

    # --- Manual balance API routes ---

    @app.route("/api/manual-balances")
    def api_manual_balances():
        """Return all manual balance entries for the history table."""
        entries = get_all_entries(_get_data_dir())
        return jsonify(entries)

    @app.route("/api/manual-balance", methods=["POST"])
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
            save_balance_entry(_get_data_dir(), account, entry_date, balance)
            refresh_data()
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.exception("Failed to save manual balance")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/manual-balance", methods=["DELETE"])
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
            deleted = delete_balance_entry(_get_data_dir(), account, entry_date)
            if deleted:
                refresh_data()
                return jsonify({"status": "ok"})
            else:
                return jsonify({"status": "error", "message": "Entry not found"}), 404
        except Exception as e:
            logger.exception("Failed to delete manual balance")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/manual-balance/bulk", methods=["POST"])
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
        data_dir = _get_data_dir()

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

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        try:
            refresh_data()
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.exception("Failed to refresh data")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/rules", methods=["POST"])
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
            import yaml
            base_dir = _get_base_dir()
            config_path = os.path.join(base_dir, "config.yaml")
            
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)

            if "categorization_rules" not in config_data:
                config_data["categorization_rules"] = []

            # Check if category exists
            found_idx = -1
            found_rule = None
            
            # Find the rule by category
            if config_data["categorization_rules"]:
                for i, rule in enumerate(config_data["categorization_rules"]):
                    if rule["category"] == category:
                        found_idx = i
                        found_rule = rule
                        break
            
            if found_idx != -1:
                # Remove it so we can re-append at the end (giving it priority)
                config_data["categorization_rules"].pop(found_idx)
                
                # Merge keywords
                current_kws = found_rule.get("keywords", [])
                for k in keywords:
                    if k not in current_kws:
                        current_kws.append(k)
                
                # Re-append with updated keywords
                config_data["categorization_rules"].append({
                    "category": category,
                    "keywords": current_kws
                })
            else:
                # Brand new rule, append
                config_data["categorization_rules"].append({
                    "category": category,
                    "keywords": keywords
                })

            # Write back
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f, sort_keys=False)

            # Changes to config require a full reload
            refresh_data()
            
            return jsonify({"status": "ok"})

        except Exception as e:
            logger.exception("Failed to save rule")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/rules")
    def rules_page():
        return render_template("rules.html")

    @app.route("/categories")
    def categories_page():
        df = _cache.get("df")
        if df is None or df.empty:
            return render_template("categories.html", categories=[], error="No data loaded.")

        import pandas as pd

        # Build category data with actual transactions
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

        return render_template("categories.html", categories=categories, error=None)

    @app.route("/api/rules", methods=["GET"])
    def api_get_rules():
        base_dir = _get_base_dir()
        config_path = os.path.join(base_dir, "config.yaml")
        if not os.path.exists(config_path):
             return jsonify({"rules": []})
        
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)
            
            rules = config_data.get("categorization_rules", [])
            return jsonify({"rules": rules})
        except Exception as e:
            logger.exception("Failed to load rules")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/rules/delete", methods=["POST"])
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
            import yaml
            base_dir = _get_base_dir()
            config_path = os.path.join(base_dir, "config.yaml")

            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)
            
            rules = config_data.get("categorization_rules", [])
            found_idx = -1
            found_rule = None

            for i, rule in enumerate(rules):
                if rule["category"] == category:
                    found_idx = i
                    found_rule = rule
                    break
            
            if found_idx == -1:
                return jsonify({"status": "error", "message": "Category not found"}), 404

            if keyword_to_remove:
                # Remove specific keyword
                current_keywords = found_rule.get("keywords", [])
                if keyword_to_remove in current_keywords:
                    current_keywords.remove(keyword_to_remove)
                    # If no keywords left, should we delete the category? 
                    # Let's keep the category but empty for now, unless user explicitly deletes category.
                
                # Update rule in list (pop and re-insert to ensure it saves correctly if needed, 
                # though strictly modifying 'found_rule' dictionary *should* update the reference within 'rules' list. 
                # Re-assigning just to be safe if python dict reference behavior is tricky in yaml load context)
                rules[found_idx]["keywords"] = current_keywords
            else:
                # Delete entire category
                rules.pop(found_idx)

            config_data["categorization_rules"] = rules

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f, sort_keys=False)
            
            refresh_data()
            return jsonify({"status": "ok"})

        except Exception as e:
            logger.exception("Failed to delete rule")
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/rules/rename", methods=["POST"])
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
            import yaml
            base_dir = _get_base_dir()
            config_path = os.path.join(base_dir, "config.yaml")

            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)
            
            rules = config_data.get("categorization_rules", [])
            found = False

            for rule in rules:
                if rule["category"] == old_cat:
                    rule["category"] = new_cat
                    found = True
                    break
            
            if not found:
                 return jsonify({"status": "error", "message": "Category not found"}), 404

            # We also need to move this rule to the end to ensure priority? 
            # Or just rename in place? Renaming doesn't necessarily change priority intent, 
            # but user might expect 'most recent edit wins'. 
            # Let's simple rename for now to preserve existing complex priorities if they exist.
            # actually, if we rename, we might want to ensure it has priority over other rules that might conflict with the new name?
            # For simplicity, rename in place.

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f, sort_keys=False)
            
            refresh_data()
            return jsonify({"status": "ok"})

        except Exception as e:
            logger.exception("Failed to rename rule")
            return jsonify({"status": "error", "message": str(e)}), 500


    # --- Recurring Bills API ---

    @app.route("/api/recurring-bills", methods=["GET"])
    def api_get_recurring_bills():
        config = _cache.get("config")
        if not config:
            # Try reloading if cache empty
            base_dir = _get_base_dir()
            config = load_config(os.path.join(base_dir, "config.yaml"))
        
        bills = []
        for b in config.recurring_bills:
            bills.append({
                "name": b.name,
                "amount": b.amount,
                "day_of_month": b.day_of_month,
                "match_criteria": b.match_criteria
            })
        return jsonify(bills)

    @app.route("/api/recurring-bills", methods=["POST"])
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
            import yaml
            base_dir = _get_base_dir()
            config_path = os.path.join(base_dir, "config.yaml")

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

    @app.route("/api/recurring-bills", methods=["DELETE"])
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
            import yaml
            base_dir = _get_base_dir()
            config_path = os.path.join(base_dir, "config.yaml")

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
