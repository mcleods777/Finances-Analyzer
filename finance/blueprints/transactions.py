from __future__ import annotations

import logging

import pandas as pd
from flask import Blueprint, render_template, request

from finance.config_loader import load_config
from finance.data_service import get_cache, get_config_path

logger = logging.getLogger(__name__)

transactions_bp = Blueprint("transactions", __name__)


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
