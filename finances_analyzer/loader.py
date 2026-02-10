import csv
from datetime import date
from pathlib import Path

import pandas as pd

from finances_analyzer.models import Transaction


def load_transactions(csv_path: str | Path) -> list[Transaction]:
    """Load transactions from a CSV file.

    Expected CSV columns: date, category, amount, description
    Date format: YYYY-MM-DD
    """
    transactions = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            transactions.append(
                Transaction(
                    date=date.fromisoformat(row["date"]),
                    category=row["category"],
                    amount=float(row["amount"]),
                    description=row["description"],
                )
            )
    return transactions


def transactions_to_dataframe(transactions: list[Transaction]) -> pd.DataFrame:
    """Convert a list of transactions into a pandas DataFrame."""
    return pd.DataFrame(
        [
            {
                "date": t.date,
                "category": t.category,
                "amount": t.amount,
                "description": t.description,
            }
            for t in transactions
        ]
    )
