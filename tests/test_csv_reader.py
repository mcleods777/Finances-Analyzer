from __future__ import annotations

from finance.config_loader import ColumnMapping
from finance.csv_reader import read_account_csv
from tests.conftest import make_csv_account


def test_read_csv_with_amount_column(tmp_path):
    csv_path = tmp_path / "amount.csv"
    csv_path.write_text(
        "Date,Description,Amount\n"
        "01/05/2026,COFFEE SHOP,-4.50\n"
        '01/06/2026,PAYCHECK,"$1,500.00"\n'
    )
    account = make_csv_account("amount.csv")

    df = read_account_csv(account, str(tmp_path))

    assert list(df.columns) == ["date", "description", "amount", "account_name", "account_type", "raw_balance"]
    assert len(df) == 2
    assert df["amount"].tolist() == [-4.50, 1500.00]
    assert df["account_name"].unique().tolist() == ["Test Checking"]


def test_read_csv_with_debit_credit_columns(tmp_path):
    csv_path = tmp_path / "debitcredit.csv"
    csv_path.write_text(
        "Date,Description,Debit,Credit\n"
        "01/05/2026,GROCERY STORE,52.30,\n"
        "01/06/2026,DEPOSIT,,250.00\n"
    )
    account = make_csv_account(
        "debitcredit.csv",
        columns=ColumnMapping(date="Date", description="Description", debit="Debit", credit="Credit"),
    )

    df = read_account_csv(account, str(tmp_path))

    assert len(df) == 2
    # debit becomes negative, credit positive
    assert df["amount"].tolist() == [-52.30, 250.00]


def test_read_csv_inverted_sign(tmp_path):
    csv_path = tmp_path / "creditcard.csv"
    csv_path.write_text(
        "Date,Description,Amount\n"
        "01/05/2026,RESTAURANT CHARGE,25.00\n"
        "01/06/2026,CARD PAYMENT,-100.00\n"
    )
    account = make_csv_account("creditcard.csv")
    account.amount_sign = "inverted"

    df = read_account_csv(account, str(tmp_path))

    assert df["amount"].tolist() == [-25.00, 100.00]


def test_read_csv_balance_column_and_bad_dates(tmp_path):
    csv_path = tmp_path / "balance.csv"
    csv_path.write_text(
        "Date,Description,Amount,Balance\n"
        '01/05/2026,ITEM ONE,-1.00,"$999.00"\n'
        "not-a-date,ITEM TWO,-2.00,998.00\n"
    )
    account = make_csv_account("balance.csv")
    account.balance_column = "Balance"

    df = read_account_csv(account, str(tmp_path))

    assert len(df) == 1  # bad date row dropped
    assert df["raw_balance"].tolist() == [999.00]
