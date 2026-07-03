from __future__ import annotations

import logging

import pandas as pd

from finance.config_loader import AppConfig, ClassificationConfig

logger = logging.getLogger(__name__)

NEGATIVE_BALANCE_TYPES = {"credit_card", "loan"}


def classify_transactions(
    df: pd.DataFrame, config: AppConfig
) -> pd.DataFrame:
    """
    Add a 'category' column: 'income', 'expense', or 'transfer'.

    Priority:
    1. Keyword match on description (income_keywords, transfer_keywords)
    2. Sign-based fallback (positive = income, negative = expense)
    3. Custom categorization rules (overrides previous)
    """
    df = df.copy()
    desc_lower = df["description"].str.lower()
    classification = config.classification

    # Default: classify by sign
    df["category"] = "expense"
    df.loc[df["amount"] > 0, "category"] = "income"

    # Override with keyword matches (transfer first, then income — income wins ties)
    for keyword in classification.transfer_keywords:
        mask = desc_lower.str.contains(keyword, na=False)
        df.loc[mask, "category"] = "transfer"

    for keyword in classification.income_keywords:
        mask = desc_lower.str.contains(keyword, na=False)
        df.loc[mask, "category"] = "income"

    # Explicit expense keywords (overrides transfer/income)
    # Useful for things like 'PayPal' which might contain 'transfer' but are actually expenses
    for keyword in classification.expense_keywords:
        mask = desc_lower.str.contains(keyword, na=False)
        df.loc[mask, "category"] = "expense"

    # Custom Subcategories (e.g. Groceries, Rent) from rules
    # We keep the main 'category' as income/expense/transfer for analytics.
    df["subcategory"] = None

    if hasattr(config, "categorization_rules"):
        for rule in config.categorization_rules:
            for keyword in rule.keywords:
                # Use regex=False to treat keyword as a literal string
                # This prevents crashes if the keyword contains regex meta-characters (e.g. +, *, (, ))
                mask = desc_lower.str.contains(keyword, na=False, regex=False)
                df.loc[mask, "subcategory"] = rule.category
    
    # Auto-categorize Transfers
    # If it's classified as a transfer (by keyword) but has no custom subcategory,
    # set the subcategory to "Transfer" as well.
    mask_transfer_auto = (df["category"] == "transfer") & (df["subcategory"].isna())
    df.loc[mask_transfer_auto, "subcategory"] = "Transfer"
    
    return df


def compute_daily_balances(
    df: pd.DataFrame,
    config: AppConfig,
    manual_balances_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Compute a unified running balance per account per day.

    Each account's balance for a given day is resolved with this precedence:

    1. A balance_snapshot on/before that day (forward-filled) — snapshots are
       ground truth (manual entry, Plaid balance check, bank statement, etc.)
       and always win once they exist for a day.
    2. Otherwise, the transaction-derived balance for that day: the
       bank-reported running-balance column if the CSV/import provided one,
       else a cumulative sum of transactions from an optional configured
       opening balance.

    This lets a single account carry both transaction history *and* periodic
    snapshots (e.g. an account with imported transactions plus a couple of
    manually-logged statement balances): days before the account's first
    snapshot fall back to the transaction-derived trajectory, and every day
    on/after a snapshot is anchored to it (forward-filled) instead of the
    derived figure, which can drift from bank-reported truth over time.

    Returns DataFrame with columns: date, account_name, account_type, balance
    """
    # --- Determine the global date range across all data sources ---
    all_dates = []
    if not df.empty:
        all_dates.extend([df["date"].min(), df["date"].max()])
    if manual_balances_df is not None and not manual_balances_df.empty:
        all_dates.extend([manual_balances_df["date"].min(), manual_balances_df["date"].max()])

    if not all_dates:
        return pd.DataFrame(columns=["date", "account_name", "account_type", "balance"])

    date_range = pd.date_range(min(all_dates), max(all_dates), freq="D")

    # --- Transaction-derived balances, per account ---
    derived: dict[str, pd.Series] = {}
    account_types: dict[str, str] = {}

    if not df.empty:
        opening_balances = {}
        for acct in config.accounts:
            if acct.opening_balance is not None:
                opening_balances[acct.name] = acct.opening_balance

        for account_name, group in df.groupby("account_name"):
            account_types[account_name] = group["account_type"].iloc[0]

            # Check if we have actual balance data from the CSV
            has_balance_col = group["raw_balance"].notna().any()

            if has_balance_col:
                # Use the bank-provided balance column — much more accurate than cumsum.
                # Take the last known balance per day, then forward-fill gaps.
                daily_balance = (
                    group.groupby("date")["raw_balance"]
                    .last()
                    .reindex(date_range)
                    .ffill()
                    .bfill()  # Fill days before the first transaction
                )
            else:
                # Fallback: cumulative sum of transactions
                daily = group.groupby("date")["amount"].sum().reindex(date_range, fill_value=0)
                opening = opening_balances.get(account_name, 0)
                daily_balance = daily.cumsum() + opening

            derived[account_name] = daily_balance

    # --- Snapshot (balance_snapshots) balances, per account ---
    # "manual_balances_df" historically meant source='manual' only, but the
    # merge logic here is source-agnostic: any balance_snapshots rows passed
    # in (manual, plaid, csv-statement, ...) are treated the same way.
    snapshot_filled: dict[str, pd.Series] = {}
    snapshot_present: dict[str, pd.Series] = {}

    if manual_balances_df is not None and not manual_balances_df.empty:
        for account_name, group in manual_balances_df.groupby("account_name"):
            # Place snapshots on the date index, forward-fill between them
            snapshots = group.set_index("date")["balance"]
            # Drop duplicates keeping last (most recent entry for a given date)
            snapshots = snapshots[~snapshots.index.duplicated(keep="last")]
            forward_filled = snapshots.reindex(date_range).ffill()
            # True from the first snapshot's date onward (that's "on/after
            # their date" — forward-fill makes every subsequent day count).
            snapshot_present[account_name] = forward_filled.notna()
            snapshot_filled[account_name] = forward_filled
            account_types.setdefault(account_name, "manual_balance")

    # --- Merge: snapshots win on/after their date; derived fills the rest ---
    results = []
    for account_name, account_type in account_types.items():
        has_derived = account_name in derived
        has_snapshot = account_name in snapshot_filled

        if has_derived and has_snapshot:
            balance = derived[account_name].copy()
            has_snapshot_on_day = snapshot_present[account_name]
            balance.loc[has_snapshot_on_day] = snapshot_filled[account_name].loc[has_snapshot_on_day]
        elif has_snapshot:
            # Snapshot-only account: no transaction history to fall back on,
            # so pre-first-snapshot days are treated as 0 (unknown history).
            balance = snapshot_filled[account_name].fillna(0)
        else:
            balance = derived[account_name]

        acct_df = pd.DataFrame(
            {
                "date": date_range,
                "account_name": account_name,
                "account_type": account_type,
                "balance": balance.values,
            }
        )
        results.append(acct_df)

    if not results:
        return pd.DataFrame(columns=["date", "account_name", "account_type", "balance"])

    return pd.concat(results, ignore_index=True)


def compute_net_worth_series(
    daily_balances: pd.DataFrame,
    excluded_accounts: set[str] | None = None,
) -> pd.DataFrame:
    """
    For each day, compute total net worth, split into assets vs liabilities.

    Account balances are stored as the bank/account reports them; the sign
    convention is applied here, by account type, for aggregation:
    - credit_card / loan balances count as liabilities (their magnitude is
      subtracted from net worth, and accumulated into `liabilities_total`).
    - every other account type (checking, savings, investment,
      manual_balance, ...) counts as an asset.

    Accounts named in `excluded_accounts` (exclude_from_net_worth flag) are
    dropped entirely: no per-account column and no contribution to the totals.

    Returns DataFrame with columns: date, net_worth, assets_total,
    liabilities_total, plus one column per account (original signed balance).
    """
    if excluded_accounts:
        daily_balances = daily_balances[
            ~daily_balances["account_name"].isin(excluded_accounts)
        ]
    if daily_balances.empty:
        return pd.DataFrame(columns=["date", "net_worth", "assets_total", "liabilities_total"])

    # Pivot: one column per account
    pivot = daily_balances.pivot_table(
        index="date", columns="account_name", values="balance", aggfunc="first"
    ).ffill().fillna(0)

    # Build account_type lookup
    type_lookup = (
        daily_balances[["account_name", "account_type"]]
        .drop_duplicates()
        .set_index("account_name")["account_type"]
        .to_dict()
    )

    # Compute net worth: assets positive, debts negative
    net_worth = pd.Series(0.0, index=pivot.index)
    assets_total = pd.Series(0.0, index=pivot.index)
    liabilities_total = pd.Series(0.0, index=pivot.index)
    for col in pivot.columns:
        acct_type = type_lookup.get(col, "checking")
        if acct_type in NEGATIVE_BALANCE_TYPES:
            # For debt accounts, the balance represents what we owe.
            # A credit card with cumsum of transactions will have negative balance
            # (charges are negative after normalization). We take the absolute value
            # and subtract it.
            liability_amount = pivot[col].abs()
            liabilities_total += liability_amount
            net_worth -= liability_amount
        else:
            assets_total += pivot[col]
            net_worth += pivot[col]

    result = pivot.copy()
    result["net_worth"] = net_worth
    result["assets_total"] = assets_total
    result["liabilities_total"] = liabilities_total
    result = result.reset_index()

    return result
