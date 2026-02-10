import argparse
from pathlib import Path

from finances_analyzer.loader import load_transactions, transactions_to_dataframe
from finances_analyzer.graphs import plot_spending_over_time, plot_category_breakdown

DEFAULT_CATEGORIES = ["Green", "Groceries", "Dining Out"]
DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "transactions.csv"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate spending graphs from transaction data."
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help="Path to the transactions CSV file.",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=DEFAULT_CATEGORIES,
        help="Categories to graph (default: Green, Groceries, 'Dining Out').",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save output graphs.",
    )
    args = parser.parse_args()

    transactions = load_transactions(args.data)
    df = transactions_to_dataframe(transactions)

    print(f"Loaded {len(transactions)} transactions from {args.data}")
    print(f"Categories: {', '.join(args.categories)}")

    line_path = plot_spending_over_time(
        df, args.categories, args.output_dir / "spending_over_time.png"
    )
    print(f"Saved line chart: {line_path}")

    bar_path = plot_category_breakdown(
        df, args.categories, args.output_dir / "spending_breakdown.png"
    )
    print(f"Saved bar chart:  {bar_path}")


if __name__ == "__main__":
    main()
