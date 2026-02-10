from __future__ import annotations

import json
import logging
import os
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

MANUAL_BALANCES_FILE = "manual_balances.json"


def _get_filepath(data_dir: str) -> str:
    return os.path.join(data_dir, MANUAL_BALANCES_FILE)


def _read_raw(data_dir: str) -> list[dict]:
    """Read the raw JSON entries from disk."""
    filepath = _get_filepath(data_dir)
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("manual_balances.json is not a list, returning empty")
            return []
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read manual_balances.json: %s", e)
        return []


def _write_raw(data_dir: str, entries: list[dict]) -> None:
    """Write the full entries list to disk."""
    filepath = _get_filepath(data_dir)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def load_manual_balances(data_dir: str) -> pd.DataFrame:
    """
    Load manual balance snapshots from JSON.

    Returns DataFrame with columns: [date, account_name, balance]
    Sorted by date ascending.
    """
    entries = _read_raw(data_dir)
    if not entries:
        return pd.DataFrame(columns=["date", "account_name", "balance"])

    df = pd.DataFrame(entries)

    # Normalize column names (JSON uses "account", we want "account_name")
    if "account" in df.columns:
        df = df.rename(columns={"account": "account_name"})

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["balance"] = pd.to_numeric(df["balance"], errors="coerce")

    # Drop invalid rows
    df = df.dropna(subset=["date", "account_name", "balance"])
    df = df[["date", "account_name", "balance"]]
    df = df.sort_values("date").reset_index(drop=True)

    return df


def save_balance_entry(
    data_dir: str, account: str, entry_date: str, balance: float
) -> None:
    """
    Append a new balance snapshot to the JSON file.
    If an entry for the same account+date already exists, it's updated.
    """
    entries = _read_raw(data_dir)

    # Check for existing entry with same account + date -> update it
    updated = False
    for entry in entries:
        if entry.get("account") == account and entry.get("date") == entry_date:
            entry["balance"] = balance
            updated = True
            break

    if not updated:
        entries.append(
            {"account": account, "date": entry_date, "balance": balance}
        )

    _write_raw(data_dir, entries)
    logger.info("Saved balance: %s on %s = $%.2f", account, entry_date, balance)


def delete_balance_entry(data_dir: str, account: str, entry_date: str) -> bool:
    """
    Remove a specific balance entry by account + date.
    Returns True if an entry was removed, False if not found.
    """
    entries = _read_raw(data_dir)
    original_len = len(entries)

    entries = [
        e
        for e in entries
        if not (e.get("account") == account and e.get("date") == entry_date)
    ]

    if len(entries) < original_len:
        _write_raw(data_dir, entries)
        logger.info("Deleted balance: %s on %s", account, entry_date)
        return True

    return False


def get_manual_account_names(data_dir: str) -> list[str]:
    """Return distinct account names from manual balances, sorted."""
    entries = _read_raw(data_dir)
    names = sorted(set(e.get("account", "") for e in entries if e.get("account")))
    return names


def get_all_entries(data_dir: str) -> list[dict]:
    """
    Return all manual balance entries as a list of dicts,
    sorted by date descending (most recent first) for display.
    """
    entries = _read_raw(data_dir)
    # Sort by date descending, then account name
    entries.sort(key=lambda e: (e.get("date", ""), e.get("account", "")), reverse=True)
    return entries


def save_bulk_entries(
    data_dir: str, account: str, entries_list: list[dict]
) -> int:
    """
    Save multiple balance entries for a single account at once.
    Each entry in entries_list should have {"date": "YYYY-MM-DD", "balance": float}.
    Entries with the same account+date are updated; new ones are appended.
    Returns the number of entries saved.
    """
    entries = _read_raw(data_dir)
    saved = 0

    for item in entries_list:
        entry_date = item.get("date", "").strip()
        balance = item.get("balance")

        if not entry_date or balance is None:
            continue

        try:
            balance = float(balance)
        except (TypeError, ValueError):
            continue

        # Check for existing entry with same account + date -> update it
        updated = False
        for entry in entries:
            if entry.get("account") == account and entry.get("date") == entry_date:
                entry["balance"] = balance
                updated = True
                break

        if not updated:
            entries.append({"account": account, "date": entry_date, "balance": balance})

        saved += 1

    if saved > 0:
        _write_raw(data_dir, entries)
        logger.info("Bulk saved %d entries for %s", saved, account)

    return saved
