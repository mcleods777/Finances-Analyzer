from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

# Make the project root importable when pytest is run from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finance import db  # noqa: E402
from finance.config_loader import (  # noqa: E402
    AccountConfig,
    AppConfig,
    CategorizationRule,
    ClassificationConfig,
    ColumnMapping,
    PayPeriodConfig,
)


@pytest.fixture
def conn(tmp_path):
    """A fresh, schema-initialized SQLite connection in a temp directory."""
    connection = db.get_connection(str(tmp_path / "finance.db"))
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def classification():
    return ClassificationConfig(
        income_keywords=["direct deposit", "payroll"],
        expense_keywords=["paypal"],
        transfer_keywords=["transfer", "xfer"],
    )


@pytest.fixture
def sample_rows():
    """Normalized rows in the standardized csv_reader shape."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
            "description": ["WALMART GROCERY", "ACME CORP PAYROLL", "Xfer To Savings"],
            "amount": [-52.30, 1500.00, -200.00],
            "account_name": ["Test Checking"] * 3,
            "account_type": ["checking"] * 3,
            "raw_balance": [1000.0, 2500.0, 2300.0],
        }
    )


def make_config(accounts: list[AccountConfig], classification: ClassificationConfig,
                rules: list[CategorizationRule] | None = None) -> AppConfig:
    from datetime import date

    return AppConfig(
        pay_period=PayPeriodConfig(start_date=date(2026, 1, 5), frequency_days=14),
        accounts=accounts,
        classification=classification,
        categorization_rules=rules or [],
    )


def make_csv_account(filename: str, name: str = "Test Checking",
                     columns: ColumnMapping | None = None) -> AccountConfig:
    return AccountConfig(
        file=filename,
        name=name,
        type="checking",
        columns=columns or ColumnMapping(date="Date", description="Description", amount="Amount"),
        date_format="%m/%d/%Y",
    )
