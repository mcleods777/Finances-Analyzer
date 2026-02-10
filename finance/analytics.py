from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from finance.config_loader import PayPeriodConfig, RecurringBill


def biweekly_spending(
    df: pd.DataFrame, pay_period: PayPeriodConfig
) -> pd.DataFrame:
    """
    Group expenses into pay-period buckets.

    Returns DataFrame with columns:
        - period_start: date
        - period_end: date
        - total_spending: float (positive number)
        - transaction_count: int
    """
    # Filter to expenses only (exclude income and transfers)
    expenses = df[df["category"] == "expense"].copy()

    if expenses.empty:
        return pd.DataFrame(
            columns=["period_start", "period_end", "total_spending", "transaction_count"]
        )

    start = pd.Timestamp(pay_period.start_date)
    freq = pay_period.frequency_days

    # Assign each transaction to a pay period
    expenses["days_since_start"] = (expenses["date"] - start).dt.days
    expenses["period_index"] = expenses["days_since_start"] // freq
    expenses["period_start"] = expenses["period_index"].apply(
        lambda i: start + pd.Timedelta(days=int(i) * freq)
    )
    expenses["period_end"] = expenses["period_start"] + pd.Timedelta(days=freq - 1)

    # Group by period
    grouped = (
        expenses.groupby(["period_start", "period_end"])
        .agg(
            total_spending=("amount", lambda x: x.abs().sum()),
            transaction_count=("amount", "count"),
        )
        .reset_index()
        .sort_values("period_start")
        .reset_index(drop=True)
    )

    return grouped


def spending_averages(biweekly_df: pd.DataFrame) -> dict:
    """
    Compute spending statistics from biweekly data.

    Returns dict with:
        - overall_average: float
        - median: float
        - std_dev: float
        - rolling_average: list (6-period rolling mean, NaN-padded)
    """
    if biweekly_df.empty:
        return {
            "overall_average": 0,
            "median": 0,
            "std_dev": 0,
            "rolling_average": [],
        }

    spending = biweekly_df["total_spending"]
    rolling = spending.rolling(window=6, min_periods=1).mean()

    return {
        "overall_average": round(float(spending.mean()), 2),
        "median": round(float(spending.median()), 2),
        "std_dev": round(float(spending.std()), 2) if len(spending) > 1 else 0,
        "rolling_average": [
            round(float(v), 2) if pd.notna(v) else None for v in rolling
        ],
    }


def compute_runway(
    current_balance: float,
    avg_biweekly_spending: float,
    pay_period: PayPeriodConfig,
) -> dict:
    """
    Compute how long the current balance will last at the average burn rate.

    Returns dict with:
        - current_balance: float
        - avg_biweekly_spending: float
        - runway_periods: float
        - runway_days: int
        - runway_date: str (ISO date when balance hits zero)
        - budget_remaining_this_period: float
        - days_left_in_period: int
        - period_start: str
        - period_end: str
    """
    today = date.today()
    freq = pay_period.frequency_days
    start = pay_period.start_date

    # Find current pay period
    days_since_start = (today - start).days
    current_period_index = days_since_start // freq
    period_start = start + timedelta(days=current_period_index * freq)
    period_end = period_start + timedelta(days=freq - 1)
    days_into_period = (today - period_start).days
    days_left = freq - days_into_period

    if avg_biweekly_spending > 0:
        runway_periods = current_balance / avg_biweekly_spending
        runway_days = int(runway_periods * freq)
        runway_date = (today + timedelta(days=runway_days)).isoformat()

        # Proportional spending expected so far this period
        expected_spent = avg_biweekly_spending * (days_into_period / freq)
        budget_remaining = current_balance - expected_spent
    else:
        runway_periods = float("inf")
        runway_days = 9999
        runway_date = "N/A"
        budget_remaining = current_balance

    return {
        "current_balance": round(current_balance, 2),
        "avg_biweekly_spending": round(avg_biweekly_spending, 2),
        "runway_periods": round(runway_periods, 1) if runway_periods != float("inf") else "∞",
        "runway_days": runway_days,
        "runway_date": runway_date,
        "budget_remaining_this_period": round(budget_remaining, 2),
        "days_left_in_period": days_left,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }


def summary_statistics(
    df: pd.DataFrame,
    net_worth_series: pd.DataFrame,
    biweekly_df: pd.DataFrame,
) -> dict:
    """
    Compile summary stats for the dashboard header cards.
    """
    today = pd.Timestamp(date.today())
    result = {}

    # Net worth
    if not net_worth_series.empty and "net_worth" in net_worth_series.columns:
        result["current_net_worth"] = round(float(net_worth_series["net_worth"].iloc[-1]), 2)

        # 30-day change
        thirty_days_ago = today - pd.Timedelta(days=30)
        past = net_worth_series[net_worth_series["date"] <= thirty_days_ago]
        if not past.empty:
            old_nw = float(past["net_worth"].iloc[-1])
            change = result["current_net_worth"] - old_nw
            result["net_worth_change_30d"] = round(change, 2)
            if old_nw != 0:
                result["net_worth_change_pct_30d"] = round(change / abs(old_nw) * 100, 1)
            else:
                result["net_worth_change_pct_30d"] = 0
        else:
            result["net_worth_change_30d"] = 0
            result["net_worth_change_pct_30d"] = 0
    else:
        result["current_net_worth"] = 0
        result["net_worth_change_30d"] = 0
        result["net_worth_change_pct_30d"] = 0

    # This month spending
    month_start = today.replace(day=1)
    month_expenses = df[(df["date"] >= month_start) & (df["category"] == "expense")]
    result["current_month_spending"] = round(float(month_expenses["amount"].abs().sum()), 2)

    # This month income
    month_income = df[(df["date"] >= month_start) & (df["category"] == "income")]
    result["income_this_month"] = round(float(month_income["amount"].sum()), 2)

    # Savings rate (trailing 3 months)
    three_months_ago = today - pd.Timedelta(days=90)
    recent = df[df["date"] >= three_months_ago]
    income_3m = float(recent[recent["category"] == "income"]["amount"].sum())
    expenses_3m = float(recent[recent["category"] == "expense"]["amount"].abs().sum())
    if income_3m > 0:
        result["savings_rate"] = round((income_3m - expenses_3m) / income_3m * 100, 1)
    else:
        result["savings_rate"] = 0

    # Average biweekly spending
    if not biweekly_df.empty:
        result["avg_biweekly_spending"] = round(float(biweekly_df["total_spending"].mean()), 2)
    else:
        result["avg_biweekly_spending"] = 0

    # Total accounts
    result["total_accounts"] = df["account_name"].nunique()

    return result


def biweekly_income(
    df: pd.DataFrame, pay_period: PayPeriodConfig
) -> pd.DataFrame:
    """
    Group income into pay-period buckets.

    Returns DataFrame with columns:
        - period_start: date
        - period_end: date
        - total_income: float (positive number)
        - transaction_count: int
    """
    income = df[df["category"] == "income"].copy()

    if income.empty:
        return pd.DataFrame(
            columns=["period_start", "period_end", "total_income", "transaction_count"]
        )

    start = pd.Timestamp(pay_period.start_date)
    freq = pay_period.frequency_days

    income["days_since_start"] = (income["date"] - start).dt.days
    income["period_index"] = income["days_since_start"] // freq
    income["period_start"] = income["period_index"].apply(
        lambda i: start + pd.Timedelta(days=int(i) * freq)
    )
    income["period_end"] = income["period_start"] + pd.Timedelta(days=freq - 1)

    grouped = (
        income.groupby(["period_start", "period_end"])
        .agg(
            total_income=("amount", lambda x: x.abs().sum()),
            transaction_count=("amount", "count"),
        )
        .reset_index()
        .sort_values("period_start")
        .reset_index(drop=True)
    )

    return grouped


def get_spending_breakdown(df: pd.DataFrame, days: int) -> list[dict]:
    """
    Get spending breakdown by category for the last N days.
    """
    if df.empty:
        return []

    # Filter by date
    today = pd.Timestamp(date.today())
    start_date = today - pd.Timedelta(days=days)
    
    # Filter for expenses in the time range
    mask = (df["date"] >= start_date) & (df["category"] == "expense")
    filtered = df[mask].copy()

    if filtered.empty:
        return []

    # Fill NaN subcategories with "Uncategorized"
    filtered["subcategory"] = filtered["subcategory"].fillna("Uncategorized")
    filtered.loc[filtered["subcategory"] == "", "subcategory"] = "Uncategorized"

    # Group by subcategory
    grouped = filtered.groupby("subcategory")["amount"].agg(lambda x: x.abs().sum()).reset_index()
    grouped.columns = ["category", "amount"]

    # Calculate percentages
    total_spending = grouped["amount"].sum()
    if total_spending == 0:
        return []

    grouped["percentage"] = (grouped["amount"] / total_spending * 100).round(1)
    grouped["amount"] = grouped["amount"].round(2)

    # Sort by amount descending
    grouped = grouped.sort_values("amount", ascending=False)

    return grouped.to_dict(orient="records")


def get_recurring_bill_status(
    df: pd.DataFrame,
    pay_period: PayPeriodConfig,
    recurring_bills: list[RecurringBill],
) -> list[dict]:
    """
    Check status of recurring bills in the current pay period.

    Returns list of dicts:
    {
        "name": str,
        "amount": float,
        "due_date": str (YYYY-MM-DD),
        "status": "paid" | "pending",
        "paid_date": str | None,
        "paid_amount": float | None
    }
    """
    if not recurring_bills:
        return []

    today = date.today()
    start = pay_period.start_date
    freq = pay_period.frequency_days

    # Determine current period
    days_since_start = (today - start).days
    current_period_index = days_since_start // freq
    period_start = start + timedelta(days=current_period_index * freq)
    period_end = period_start + timedelta(days=freq - 1)

    # Filter applicable transactions (expenses in this period)
    period_txns = df[
        (df["date"].dt.date >= period_start)
        & (df["date"].dt.date <= period_end)
        & (df["category"] == "expense")
    ].copy()
    
    # Pre-process descriptions for matching
    period_txns["desc_lower"] = period_txns["description"].str.lower()

    results = []

    for bill in recurring_bills:
        # Check if bill falls in this period
        # Iterate days in period
        bill_due_date = None
        for i in range(freq):
            d = period_start + timedelta(days=i)
            if d.day == bill.day_of_month:
                bill_due_date = d
                break
        
        if not bill_due_date:
            continue

        # Check for payment
        is_paid = False
        paid_details = {}

        if not period_txns.empty:
            # Check for exact matches first? Or just contains
            # Config has match_criteria (list of keywords)
            for criteria in bill.match_criteria:
                match = period_txns[period_txns["desc_lower"].str.contains(criteria, na=False)]
                if not match.empty:
                    # Found it!
                    is_paid = True
                    # Take the first match (most likely the payment)
                    row = match.iloc[0]
                    paid_details = {
                        "paid_date": row["date"].strftime("%Y-%m-%d"),
                        "paid_amount": abs(row["amount"])
                    }
                    break
        
        status = "paid" if is_paid else "pending"
        
        results.append({
            "name": bill.name,
            "amount": bill.amount,
            "due_date": bill_due_date.isoformat(),
            "status": status,
            **paid_details
        })

    # Sort checks by due date
    results.sort(key=lambda x: x["due_date"])
    return results
