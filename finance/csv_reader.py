from __future__ import annotations

import logging
import os
from typing import IO

import pandas as pd

from finance.config_loader import AccountConfig, AppConfig

logger = logging.getLogger(__name__)

CSV_ENCODINGS = ("utf-8", "latin-1")

# Candidate date formats tried by detect_date_format, in preference order.
_CANDIDATE_DATE_FORMATS = [
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%m/%d/%y",
]

_DATE_HINTS = ["date", "trans date", "transaction date", "posted date", "posted"]
_DESC_HINTS = ["description", "memo", "payee", "name", "narrative", "details"]
_AMOUNT_HINTS = ["amount", "amt"]
_DEBIT_HINTS = ["debit", "withdrawal"]
_CREDIT_HINTS = ["credit", "deposit"]
_BALANCE_HINTS = ["balance", "running balance", "ending balance"]


def read_csv_any_encoding(source: str | IO[bytes]) -> pd.DataFrame:
    """
    Read a CSV from a filesystem path or a file-like object (e.g. an
    uploaded file's BytesIO buffer), trying UTF-8 then Latin-1.
    """
    last_err: Exception | None = None
    for encoding in CSV_ENCODINGS:
        if hasattr(source, "seek"):
            source.seek(0)
        try:
            return pd.read_csv(source, encoding=encoding)
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise ValueError(f"Could not read CSV with encodings {CSV_ENCODINGS}: {last_err}")


def normalize_dataframe(
    df: pd.DataFrame,
    *,
    date_col: str,
    description_col: str,
    amount_col: str | None = None,
    debit_col: str | None = None,
    credit_col: str | None = None,
    date_format: str | None = None,
    amount_sign: str = "standard",
    balance_col: str | None = None,
    account_name: str | None = None,
    account_type: str | None = None,
) -> pd.DataFrame:
    """
    Mapping-driven CSV normalization shared by the config-driven account path
    (read_account_csv) and the in-app upload path (blueprints/accounts.py).

    Returns a DataFrame with standardized columns:
        date, description, amount, [account_name, account_type,] raw_balance

    Raises ValueError if a required mapped column is missing from df, mirroring
    the previous read_account_csv behavior.
    """
    df = df.copy()
    df.columns = df.columns.str.strip()

    rename_map: dict[str, str] = {date_col: "date", description_col: "description"}
    if amount_col:
        rename_map[amount_col] = "amount"
    if debit_col:
        rename_map[debit_col] = "_debit"
    if credit_col:
        rename_map[credit_col] = "_credit"

    if not amount_col and not (debit_col and credit_col):
        raise ValueError("Must specify either an amount column or both debit and credit columns")

    for src_col in rename_map:
        if src_col not in df.columns:
            raise ValueError(
                f"Column '{src_col}' not found in CSV. Available columns: {list(df.columns)}"
            )

    df = df.rename(columns=rename_map)

    # Parse dates
    df["date"] = pd.to_datetime(df["date"], format=date_format, errors="coerce")
    bad_dates = df["date"].isna().sum()
    if bad_dates > 0:
        logger.warning("%d rows had unparseable dates and were dropped", bad_dates)
        df = df.dropna(subset=["date"])

    # Compute amount
    if amount_col:
        df["amount"] = df["amount"].astype(str).str.replace(r"[$,\s]", "", regex=True)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    else:
        for c in ("_debit", "_credit"):
            df[c] = df[c].astype(str).str.replace(r"[$,\s]", "", regex=True)
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        df["amount"] = df["_credit"] - df["_debit"]
        df = df.drop(columns=["_debit", "_credit"])

    # Invert sign if needed (e.g., credit cards where charges are positive)
    if amount_sign == "inverted":
        df["amount"] = df["amount"] * -1

    # Extract balance column if configured
    if balance_col and balance_col in df.columns:
        df["raw_balance"] = df[balance_col].astype(str).str.replace(r"[$,\s]", "", regex=True)
        df["raw_balance"] = pd.to_numeric(df["raw_balance"], errors="coerce")
    else:
        df["raw_balance"] = float("nan")

    # Strip description whitespace
    df["description"] = df["description"].astype(str).str.strip()

    # Drop rows with invalid amounts
    df = df.dropna(subset=["amount"])

    # Keep only standard columns, in a fixed order
    keep_cols = ["date", "description", "amount"]
    if account_name is not None:
        df["account_name"] = account_name
        keep_cols.append("account_name")
    if account_type is not None:
        df["account_type"] = account_type
        keep_cols.append("account_type")
    keep_cols.append("raw_balance")
    df = df[keep_cols]

    # Sort by date
    df = df.sort_values("date").reset_index(drop=True)

    return df


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
    try:
        df = read_csv_any_encoding(filepath)
    except ValueError as e:
        raise ValueError(f"Could not read {filepath}: {e}") from e

    col = account.columns
    return normalize_dataframe(
        df,
        date_col=col.date,
        description_col=col.description,
        amount_col=col.amount,
        debit_col=col.debit,
        credit_col=col.credit,
        date_format=account.date_format,
        amount_sign=account.amount_sign,
        balance_col=account.balance_column,
        account_name=account.name,
        account_type=account.type,
    )


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


# --- Column / date-format detection for the upload "Add account" flow ---


def _best_match(columns: list[str], hints: list[str]) -> str | None:
    lowered = {c: c.strip().lower() for c in columns}
    for hint in hints:
        for col, low in lowered.items():
            if low == hint:
                return col
    for hint in hints:
        for col, low in lowered.items():
            if hint in low:
                return col
    return None


def detect_column_mapping(columns: list[str]) -> dict[str, str | None]:
    """Best-guess column mapping from a list of CSV header names."""
    debit = _best_match(columns, _DEBIT_HINTS)
    credit = _best_match(columns, _CREDIT_HINTS)
    amount = None
    if not (debit and credit):
        amount = _best_match(columns, _AMOUNT_HINTS)
    return {
        "date": _best_match(columns, _DATE_HINTS),
        "description": _best_match(columns, _DESC_HINTS),
        "amount": amount,
        "debit": debit,
        "credit": credit,
        "balance": _best_match(columns, _BALANCE_HINTS),
    }


def detect_date_format(values) -> str | None:
    """
    Best-guess strptime date format for a column's sample values. Returns
    the candidate format with the highest successful-parse count, or None
    if the sample is empty. The UI must let the user confirm/override this.
    """
    sample = [str(v).strip() for v in list(values)[:50] if str(v).strip() and str(v).lower() != "nan"]
    if not sample:
        return None
    best_fmt: str | None = None
    best_score = -1
    for fmt in _CANDIDATE_DATE_FORMATS:
        parsed = pd.to_datetime(sample, format=fmt, errors="coerce")
        score = int(parsed.notna().sum())
        if score > best_score:
            best_score = score
            best_fmt = fmt
    return best_fmt


def preview_csv(source: str | IO[bytes], max_rows: int = 5) -> dict:
    """
    Parse an uploaded CSV without importing it: return its columns, a
    small row preview (as strings, for display), and a best-guess column +
    date-format mapping for the "Add account" flow to prefill.
    """
    df = read_csv_any_encoding(source)
    df.columns = df.columns.str.strip()
    columns = list(df.columns)

    detected_mapping = detect_column_mapping(columns)
    detected_date_format = None
    date_col = detected_mapping.get("date")
    if date_col and date_col in df.columns:
        detected_date_format = detect_date_format(df[date_col])

    preview_rows = df.head(max_rows).fillna("").astype(str).to_dict(orient="records")

    return {
        "columns": columns,
        "rows": preview_rows,
        "row_count_sample": len(df),
        "detected_mapping": detected_mapping,
        "detected_date_format": detected_date_format,
    }
