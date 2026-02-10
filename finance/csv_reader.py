from __future__ import annotations

import logging
import os

import pandas as pd

from finance.config_loader import AccountConfig, AppConfig

logger = logging.getLogger(__name__)


def read_account_csv(account: AccountConfig, data_dir: str) -> pd.DataFrame:
    """
    Read a single account CSV and normalize it.

    Returns DataFrame with standardized columns:
        - date: datetime64
        - description: str
        - amount: float (positive = money in, negative = money out)
        - account_name: str
        - account_type: str
        - raw_balance: float (if balance_column configured, else NaN)
    """
    filepath = os.path.join(data_dir, account.file)

    # Try UTF-8 first, fall back to latin-1
    for encoding in ("utf-8", "latin-1"):
        try:
            df = pd.read_csv(filepath, encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"Could not read {filepath} with UTF-8 or latin-1 encoding")

    # Strip whitespace from column names
    df.columns = df.columns.str.strip()

    # Rename columns to standard names
    col = account.columns
    rename_map = {
        col.date: "date",
        col.description: "description",
    }

    if col.amount:
        rename_map[col.amount] = "amount"
    if col.debit:
        rename_map[col.debit] = "_debit"
    if col.credit:
        rename_map[col.credit] = "_credit"

    # Check that expected columns exist
    for src_col in rename_map:
        if src_col not in df.columns:
            raise ValueError(
                f"Column '{src_col}' not found in {account.file}. "
                f"Available columns: {list(df.columns)}"
            )

    df = df.rename(columns=rename_map)

    # Parse dates
    df["date"] = pd.to_datetime(df["date"], format=account.date_format, errors="coerce")
    bad_dates = df["date"].isna().sum()
    if bad_dates > 0:
        logger.warning(
            "%s: %d rows had unparseable dates and were dropped", account.file, bad_dates
        )
        df = df.dropna(subset=["date"])

    # Compute amount
    if col.amount:
        # Clean amount: remove $, commas, whitespace
        df["amount"] = (
            df["amount"]
            .astype(str)
            .str.replace(r"[$,\s]", "", regex=True)
        )
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    else:
        # Separate debit/credit columns
        for c in ("_debit", "_credit"):
            df[c] = (
                df[c]
                .astype(str)
                .str.replace(r"[$,\s]", "", regex=True)
            )
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        df["amount"] = df["_credit"] - df["_debit"]
        df = df.drop(columns=["_debit", "_credit"])

    # Invert sign if needed (e.g., credit cards where charges are positive)
    if account.amount_sign == "inverted":
        df["amount"] = df["amount"] * -1

    # Extract balance column if configured
    if account.balance_column and account.balance_column in df.columns:
        df["raw_balance"] = (
            df[account.balance_column]
            .astype(str)
            .str.replace(r"[$,\s]", "", regex=True)
        )
        df["raw_balance"] = pd.to_numeric(df["raw_balance"], errors="coerce")
    else:
        df["raw_balance"] = float("nan")

    # Add account metadata
    df["account_name"] = account.name
    df["account_type"] = account.type

    # Strip description whitespace
    df["description"] = df["description"].astype(str).str.strip()

    # Drop rows with invalid amounts
    df = df.dropna(subset=["amount"])

    # Keep only standard columns
    df = df[["date", "description", "amount", "account_name", "account_type", "raw_balance"]]

    # Sort by date
    df = df.sort_values("date").reset_index(drop=True)

    return df


def read_all_accounts(config: AppConfig, data_dir: str) -> pd.DataFrame:
    """
    Read all account CSVs, normalize each, concatenate into one DataFrame.
    Deduplicates by (date, description, amount, account_name).
    """
    frames = []
    for account in config.accounts:
        filepath = os.path.join(data_dir, account.file)
        if not os.path.exists(filepath):
            logger.warning("Skipping missing CSV: %s", filepath)
            continue
        try:
            df = read_account_csv(account, data_dir)
            frames.append(df)
            logger.info("Loaded %d transactions from %s", len(df), account.file)
        except Exception as e:
            logger.error("Failed to read %s: %s", account.file, e)
            continue

    if not frames:
        # Return empty DataFrame with correct columns
        return pd.DataFrame(
            columns=["date", "description", "amount", "account_name", "account_type", "raw_balance"]
        )

    combined = pd.concat(frames, ignore_index=True)

    # Deduplicate
    before = len(combined)
    combined = combined.drop_duplicates(
        subset=["date", "description", "amount", "account_name"], keep="first"
    )
    dupes = before - len(combined)
    if dupes > 0:
        logger.info("Removed %d duplicate transactions", dupes)

    combined = combined.sort_values("date").reset_index(drop=True)
    return combined
