from __future__ import annotations

import logging
import os

import pandas as pd

from finance import db, migrate
from finance.analytics import (
    biweekly_income,
    biweekly_spending,
    compute_monthly_half_runway,
    compute_runway,
    get_recurring_bill_status,
    spending_averages,
    summary_statistics,
)
from finance.config_loader import load_config, validate_config
from finance.data_processor import compute_daily_balances, compute_net_worth_series

logger = logging.getLogger(__name__)

# In-memory cache for computed data
_cache: dict = {}


def get_base_dir() -> str:
    """Get the project base directory (where app.py lives)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_data_dir() -> str:
    return os.path.join(get_base_dir(), "data")


def get_config_path() -> str:
    return os.path.join(get_base_dir(), "config.yaml")


def get_db_connection():
    """Open a connection to the app database (data/finance.db), schema ensured."""
    conn = db.get_connection(db.get_db_path(get_data_dir()))
    db.init_db(conn)
    return conn


def get_cache() -> dict:
    return _cache


def refresh_data() -> dict:
    """
    Sync file sources into SQLite (idempotent), load transactions/snapshots
    from the DB into the same in-memory DataFrame shape the analytics layer
    has always consumed, recompute analytics, and repopulate the cache.
    """
    data_dir = get_data_dir()
    config = load_config(get_config_path())

    issues = validate_config(config, data_dir)
    for issue in issues:
        logger.warning("Config issue: %s", issue)

    conn = get_db_connection()
    try:
        migrate.run_startup_migration(conn, config, data_dir)

        # Load transaction data from SQLite (already classified + categorized)
        df = db.load_transactions_df(conn)

        # Manual balance snapshots (formerly manual_balances.json)
        snapshots = db.load_balance_snapshots_df(conn, source="manual")
        manual_df = snapshots[["date", "account_name", "balance"]] if not snapshots.empty else snapshots

        # Accounts flagged "exclude from net worth" (net-worth math only)
        nw_excluded = db.excluded_net_worth_account_names(conn)
    finally:
        conn.close()

    if df.empty and manual_df.empty:
        _cache.clear()
        _cache["error"] = "No data found. Place CSV files in data/ or log manual balances."
        return _cache

    # Compute daily balances (merges CSV + manual accounts)
    daily_bal = compute_daily_balances(df, config, manual_df)
    nw_series = compute_net_worth_series(daily_bal, excluded_accounts=nw_excluded)

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

    # Monthly-half runway
    monthly_runway = compute_monthly_half_runway(
        current_balance=current_balance,
        avg_biweekly_spending=avg_stats["overall_average"],
        df=df if not df.empty else empty_classified_df(),
        recurring_bills=config.recurring_bills,
        temporary_expenses=config.temporary_expenses,
        budget_overrides=config.budget_overrides,
    )

    summary = summary_statistics(
        df if not df.empty else empty_classified_df(),
        nw_series,
        biweekly_df if biweekly_df is not None else empty_biweekly_df(),
        config.income_attribution,
    )

    _cache.clear()
    _cache["df"] = df
    _cache["daily_balances"] = daily_bal
    _cache["net_worth_series"] = nw_series
    _cache["biweekly_df"] = biweekly_df
    _cache["biweekly_income_df"] = biweekly_inc_df
    _cache["avg_stats"] = avg_stats
    _cache["runway"] = runway
    _cache["monthly_runway"] = monthly_runway
    _cache["summary"] = summary
    _cache["config"] = config

    return _cache


def empty_classified_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["date", "description", "amount", "account_name", "account_type", "raw_balance", "category", "subcategory"]
    )


def empty_biweekly_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["period_start", "period_end", "total_spending", "transaction_count"]
    )
