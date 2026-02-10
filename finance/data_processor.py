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
    Compute a running balance per account per day.

    For CSV-based accounts:
    - If opening_balance is set in config, start from there.
    - Otherwise, cumulative sum of transactions (assumes CSV is complete history).

    For manual balance accounts:
    - Snapshots are forward-filled: balance stays at the last known value until
      a new snapshot overrides it.

    Returns DataFrame with columns: date, account_name, account_type, balance
    """
    results = []

    # --- Determine the global date range across all data sources ---
    all_dates = []
    if not df.empty:
        all_dates.extend([df["date"].min(), df["date"].max()])
    if manual_balances_df is not None and not manual_balances_df.empty:
        all_dates.extend([manual_balances_df["date"].min(), manual_balances_df["date"].max()])

    if not all_dates:
        return pd.DataFrame(columns=["date", "account_name", "account_type", "balance"])

    date_range = pd.date_range(min(all_dates), max(all_dates), freq="D")

    # --- CSV-based (transaction) accounts ---
    if not df.empty:
        opening_balances = {}
        for acct in config.accounts:
            if acct.opening_balance is not None:
                opening_balances[acct.name] = acct.opening_balance

        for account_name, group in df.groupby("account_name"):
            account_type = group["account_type"].iloc[0]

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
                balance = daily_balance
            else:
                # Fallback: cumulative sum of transactions
                daily = group.groupby("date")["amount"].sum().reindex(date_range, fill_value=0)
                opening = opening_balances.get(account_name, 0)
                balance = daily.cumsum() + opening

            acct_df = pd.DataFrame(
                {
                    "date": date_range,
                    "account_name": account_name,
                    "account_type": account_type,
                    "balance": balance.values,
                }
            )
            results.append(acct_df)

    # --- Manual balance (snapshot) accounts ---
    if manual_balances_df is not None and not manual_balances_df.empty:
        for account_name, group in manual_balances_df.groupby("account_name"):
            # Place snapshots on the date index, forward-fill between them
            snapshots = group.set_index("date")["balance"]
            # Drop duplicates keeping last (most recent entry for a given date)
            snapshots = snapshots[~snapshots.index.duplicated(keep="last")]
            # Reindex to the full date range and forward-fill
            daily_balance = snapshots.reindex(date_range).ffill()
            # Rows before the first snapshot will be NaN — fill with 0
            daily_balance = daily_balance.fillna(0)

            acct_df = pd.DataFrame(
                {
                    "date": date_range,
                    "account_name": account_name,
                    "account_type": "manual_balance",
                    "balance": daily_balance.values,
                }
            )
            results.append(acct_df)

    if not results:
        return pd.DataFrame(columns=["date", "account_name", "account_type", "balance"])

    return pd.concat(results, ignore_index=True)


def compute_net_worth_series(daily_balances: pd.DataFrame) -> pd.DataFrame:
    """
    For each day, compute total net worth.
    Credit card and loan balances are subtracted (they represent debt).

    Returns DataFrame with columns: date, net_worth, plus per-account columns.
    """
    if daily_balances.empty:
        return pd.DataFrame(columns=["date", "net_worth"])

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
    for col in pivot.columns:
        acct_type = type_lookup.get(col, "checking")
        if acct_type in NEGATIVE_BALANCE_TYPES:
            # For debt accounts, the balance represents what we owe.
            # A credit card with cumsum of transactions will have negative balance
            # (charges are negative after normalization). We take the absolute value
            # and subtract it.
            net_worth -= pivot[col].abs()
        else:
            net_worth += pivot[col]

    result = pivot.copy()
    result["net_worth"] = net_worth
    result = result.reset_index()

    return result
