from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd


CATEGORY_COLORS = {
    "Green": "#2ecc71",
    "Groceries": "#3498db",
    "Dining Out": "#e74c3c",
}


def plot_spending_over_time(
    df: pd.DataFrame,
    categories: list[str],
    output_path: str | Path,
    resample_rule: str = "MS",
) -> Path:
    """Plot monthly spending over time for the given categories.

    Args:
        df: DataFrame with columns: date, category, amount.
        categories: Category names to include in the graph.
        output_path: Where to save the resulting PNG.
        resample_rule: Pandas resample rule. Default "MS" = month start.

    Returns:
        Path to the saved image.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 6))

    for category in categories:
        cat_df = df[df["category"] == category].copy()
        if cat_df.empty:
            continue

        cat_df["date"] = pd.to_datetime(cat_df["date"])
        monthly = (
            cat_df.set_index("date")["amount"]
            .resample(resample_rule)
            .sum()
        )

        color = CATEGORY_COLORS.get(category, None)
        ax.plot(
            monthly.index,
            monthly.values,
            marker="o",
            label=category,
            color=color,
            linewidth=2,
        )

    ax.set_title("Monthly Spending by Category", fontsize=16, fontweight="bold")
    ax.set_xlabel("Month", fontsize=12)
    ax.set_ylabel("Amount ($)", fontsize=12)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.autofmt_xdate(rotation=45)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path


def plot_category_breakdown(
    df: pd.DataFrame,
    categories: list[str],
    output_path: str | Path,
) -> Path:
    """Plot a stacked bar chart showing monthly spending breakdown.

    Args:
        df: DataFrame with columns: date, category, amount.
        categories: Category names to include.
        output_path: Where to save the resulting PNG.

    Returns:
        Path to the saved image.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = df[df["category"].isin(categories)].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M")

    pivot = df.pivot_table(
        index="month", columns="category", values="amount", aggfunc="sum", fill_value=0
    )
    # Reorder columns to match the requested category order
    pivot = pivot.reindex(columns=[c for c in categories if c in pivot.columns])

    fig, ax = plt.subplots(figsize=(12, 6))

    colors = [CATEGORY_COLORS.get(c, None) for c in pivot.columns]
    pivot.plot(kind="bar", stacked=True, ax=ax, color=colors, width=0.7)

    ax.set_title("Monthly Spending Breakdown", fontsize=16, fontweight="bold")
    ax.set_xlabel("Month", fontsize=12)
    ax.set_ylabel("Amount ($)", fontsize=12)
    ax.set_xticklabels([str(p) for p in pivot.index], rotation=45, ha="right")
    ax.legend(fontsize=11)
    ax.grid(True, axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path
