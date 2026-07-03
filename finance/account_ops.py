from __future__ import annotations

"""
Account management operations: merge, delete, and Plaid-item unlink.

Pure sqlite3 + importer logic (no Flask); the accounts/plaid blueprints wrap
these in routes. Every mutating operation runs in a single SQLite transaction.

Merge semantics:
- Source transactions move to the target with their dedup_hash recomputed for
  the target account id (the hash includes account_id).
- Overlap detection is one-to-one on (date, amount): each target transaction
  can absorb at most one source transaction, so two genuinely identical
  purchases on the target both survive. Pairs whose `source` columns differ
  (e.g. plaid vs csv) are matched preferentially.
- Skipped duplicates are deleted, not inserted; if a skipped source row was
  user-edited with a category, that category is copied to its matched target
  row so the edit isn't lost. Exact-duplicate hash collisions during the move
  are handled the same way.
- balance_snapshots move to the target, deduped on (date, source) keeping the
  target's; imports audit rows move; plaid link / institution / column_mapping
  are inherited when the target lacks them; the source account row is deleted.
"""

import json
import logging
import sqlite3

from finance import importer, plaid_sync

logger = logging.getLogger(__name__)


class AccountNotFound(LookupError):
    pass


class AccountOpsError(ValueError):
    pass


def get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise AccountNotFound(f"Account {account_id} not found")
    return row


def _tombstone_config_file(conn: sqlite3.Connection, column_mapping: str | None) -> None:
    """
    Remember that a config.yaml-seeded account (identified by its mapping's
    "file") is gone, so the startup migration never re-creates it.
    """
    if not column_mapping:
        return
    try:
        file = json.loads(column_mapping).get("file")
    except (TypeError, ValueError):
        return
    if file:
        conn.execute("INSERT OR IGNORE INTO config_tombstones (file) VALUES (?)", (file,))


# --- Merge ---


def _txn_key(row: sqlite3.Row) -> tuple[str, float]:
    return (str(row["date"]), round(float(row["amount"]), 2))


def _match_overlaps(
    conn: sqlite3.Connection, source_id: int, target_id: int
) -> tuple[list[tuple[sqlite3.Row, sqlite3.Row]], list[sqlite3.Row]]:
    """
    One-to-one duplicate matching between source and target transactions on
    (date, amount). Returns (matched pairs, unmatched source rows to move).
    Pairs with differing `source` columns (plaid vs csv) are preferred.
    """
    buckets: dict[tuple[str, float], list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? ORDER BY date, id", (target_id,)
    ):
        buckets.setdefault(_txn_key(row), []).append(row)

    matches: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    moving: list[sqlite3.Row] = []
    for row in conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? ORDER BY date, id", (source_id,)
    ).fetchall():
        bucket = buckets.get(_txn_key(row))
        if bucket:
            # Prefer absorbing into a target row from a different source
            # (the classic plaid-vs-csv double import).
            idx = next(
                (i for i, t in enumerate(bucket) if t["source"] != row["source"]), 0
            )
            matches.append((row, bucket.pop(idx)))
        else:
            moving.append(row)
    return matches, moving


def _count_snapshot_moves(
    conn: sqlite3.Connection, source_id: int, target_id: int
) -> tuple[int, int]:
    """(snapshots that would move, snapshots dropped as same-(date,source) dupes)."""
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM balance_snapshots WHERE account_id = ?", (source_id,)
    ).fetchone()["c"]
    dupes = conn.execute(
        """
        SELECT COUNT(*) AS c FROM balance_snapshots s
        WHERE s.account_id = :src
          AND EXISTS (SELECT 1 FROM balance_snapshots t
                       WHERE t.account_id = :tgt AND t.date = s.date AND t.source = s.source)
        """,
        {"src": source_id, "tgt": target_id},
    ).fetchone()["c"]
    return int(total) - int(dupes), int(dupes)


def merge_preview(conn: sqlite3.Connection, source_id: int, target_id: int) -> dict:
    """Dry-run counts + sample overlaps for merging source into target."""
    if source_id == target_id:
        raise AccountOpsError("Cannot merge an account into itself")
    get_account(conn, source_id)
    get_account(conn, target_id)

    matches, moving = _match_overlaps(conn, source_id, target_id)
    snapshots_moving, _ = _count_snapshot_moves(conn, source_id, target_id)
    return {
        "moving": len(moving),
        "overlaps": len(matches),
        "snapshots_moving": snapshots_moving,
        "sample_overlaps": [
            {
                "date": str(s["date"]),
                "amount": float(s["amount"]),
                "desc_source": s["description"],
                "desc_target": t["description"],
            }
            for s, t in matches[:5]
        ],
    }


def merge_accounts(conn: sqlite3.Connection, source_id: int, target_id: int) -> dict:
    """
    Merge the source account into the target atomically (one transaction).
    Returns {"moved", "duplicates_skipped", "snapshots_moved"}.
    """
    if source_id == target_id:
        raise AccountOpsError("Cannot merge an account into itself")
    source = get_account(conn, source_id)
    target = get_account(conn, target_id)

    moved = 0
    duplicates_skipped = 0
    snapshots_moved = 0

    with conn:  # single transaction: all-or-nothing
        matches, moving = _match_overlaps(conn, source_id, target_id)

        # Duplicates: keep the target row; preserve user edits from the source.
        for src, tgt in matches:
            if src["user_edited"] and src["category"]:
                conn.execute(
                    "UPDATE transactions SET category = ?, user_edited = 1 WHERE id = ?",
                    (src["category"], tgt["id"]),
                )
            conn.execute("DELETE FROM transactions WHERE id = ?", (src["id"],))
            duplicates_skipped += 1

        # Movers: recompute dedup_hash for the target account id.
        for src in moving:
            new_hash = importer.compute_dedup_hash(
                target_id, str(src["date"]), float(src["amount"]), src["description"]
            )
            try:
                conn.execute(
                    "UPDATE transactions SET account_id = ?, dedup_hash = ? WHERE id = ?",
                    (target_id, new_hash, src["id"]),
                )
                moved += 1
            except sqlite3.IntegrityError:
                # The target already holds an exact duplicate (same normalized
                # description too) — skip it like an overlap.
                if src["user_edited"] and src["category"]:
                    conn.execute(
                        "UPDATE transactions SET category = ?, user_edited = 1 "
                        "WHERE dedup_hash = ?",
                        (src["category"], new_hash),
                    )
                conn.execute("DELETE FROM transactions WHERE id = ?", (src["id"],))
                duplicates_skipped += 1

        # Balance snapshots: move, deduping same (date, source) — target's wins.
        for snap in conn.execute(
            "SELECT * FROM balance_snapshots WHERE account_id = ?", (source_id,)
        ).fetchall():
            dupe = conn.execute(
                "SELECT 1 FROM balance_snapshots "
                "WHERE account_id = ? AND date = ? AND source = ?",
                (target_id, snap["date"], snap["source"]),
            ).fetchone()
            if dupe is not None:
                conn.execute("DELETE FROM balance_snapshots WHERE id = ?", (snap["id"],))
            else:
                conn.execute(
                    "UPDATE balance_snapshots SET account_id = ? WHERE id = ?",
                    (target_id, snap["id"]),
                )
                snapshots_moved += 1

        # Import audit rows follow the transactions.
        conn.execute(
            "UPDATE imports SET account_id = ? WHERE account_id = ?",
            (target_id, source_id),
        )

        # Inherit plaid link / institution / column mapping where the target
        # lacks them. plaid_sync routes incoming transactions by a
        # plaid_account_id -> account id lookup, so moving plaid_account_id
        # onto the target makes future syncs land in the merged account.
        updates: dict[str, object] = {}
        if source["plaid_account_id"] and not target["plaid_account_id"]:
            updates["plaid_account_id"] = source["plaid_account_id"]
            updates["plaid_item_id"] = source["plaid_item_id"]
            if target["source"] != "plaid":
                updates["source"] = "plaid"
        if source["institution"] and not target["institution"]:
            updates["institution"] = source["institution"]
        if source["column_mapping"] and not target["column_mapping"]:
            updates["column_mapping"] = source["column_mapping"]
        elif source["column_mapping"] and target["column_mapping"]:
            # The source's config-file identity dies with it — tombstone it so
            # the startup migration doesn't re-create the source account.
            _tombstone_config_file(conn, source["column_mapping"])
        if updates:
            assignments = ", ".join(f"{col} = ?" for col in updates)
            conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?",
                (*updates.values(), target_id),
            )

        conn.execute("DELETE FROM accounts WHERE id = ?", (source_id,))

    logger.info(
        "Merged account %s into %s: %d moved, %d duplicates skipped, %d snapshots moved",
        source["name"], target["name"], moved, duplicates_skipped, snapshots_moved,
    )
    return {
        "moved": moved,
        "duplicates_skipped": duplicates_skipped,
        "snapshots_moved": snapshots_moved,
    }


# --- Delete ---


def delete_account(conn: sqlite3.Connection, account_id: int, remove_remote=None) -> dict:
    """
    Delete an account, cascading transactions / snapshots / imports. If it was
    the last account on a Plaid item, the plaid_items row is removed too (and
    item/remove is attempted best-effort via `remove_remote`).

    Returns {"deleted": {transactions, snapshots, imports}, "unlinked_item": ...}.
    """
    if remove_remote is None:
        remove_remote = plaid_sync.remove_item
    account = get_account(conn, account_id)

    unlinked_item = None
    stale_access_token = None
    with conn:
        counts = {
            "transactions": conn.execute(
                "DELETE FROM transactions WHERE account_id = ?", (account_id,)
            ).rowcount,
            "snapshots": conn.execute(
                "DELETE FROM balance_snapshots WHERE account_id = ?", (account_id,)
            ).rowcount,
            "imports": conn.execute(
                "DELETE FROM imports WHERE account_id = ?", (account_id,)
            ).rowcount,
        }
        _tombstone_config_file(conn, account["column_mapping"])

        if account["plaid_item_id"]:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM accounts WHERE plaid_item_id = ? AND id != ?",
                (account["plaid_item_id"], account_id),
            ).fetchone()["c"]
            if remaining == 0:
                item = conn.execute(
                    "SELECT * FROM plaid_items WHERE item_id = ?",
                    (account["plaid_item_id"],),
                ).fetchone()
                if item is not None:
                    stale_access_token = item["access_token"]
                    unlinked_item = {
                        "item_id": item["item_id"],
                        "institution_name": item["institution_name"],
                    }
                conn.execute(
                    "DELETE FROM plaid_items WHERE item_id = ?",
                    (account["plaid_item_id"],),
                )

        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    if stale_access_token:
        remove_remote(stale_access_token)  # best-effort; never raises

    logger.info(
        "Deleted account %s (%d txns, %d snapshots, %d imports)%s",
        account["name"], counts["transactions"], counts["snapshots"], counts["imports"],
        f"; unlinked Plaid item {unlinked_item['item_id']}" if unlinked_item else "",
    )
    return {"deleted": counts, "unlinked_item": unlinked_item}


# --- Unlink a Plaid item ---


def unlink_item(
    conn: sqlite3.Connection, item_id: str, keep_data: bool, remove_remote=None
) -> dict:
    """
    Remove a plaid_items row. keep_data=True detaches its accounts (they lose
    plaid_account_id/plaid_item_id and become upload-style accounts, history
    kept). keep_data=False also deletes the accounts and all their data.
    item/remove is attempted best-effort against the Plaid API.
    """
    if remove_remote is None:
        remove_remote = plaid_sync.remove_item
    item = conn.execute(
        "SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if item is None:
        raise AccountNotFound(f"Plaid item {item_id} not found")

    accounts = conn.execute(
        "SELECT * FROM accounts WHERE plaid_item_id = ?", (item_id,)
    ).fetchall()

    deleted = {"transactions": 0, "snapshots": 0, "imports": 0}
    with conn:
        if keep_data:
            conn.execute(
                "UPDATE accounts SET plaid_account_id = NULL, plaid_item_id = NULL, "
                "source = 'csv' WHERE plaid_item_id = ?",
                (item_id,),
            )
        else:
            for account in accounts:
                deleted["transactions"] += conn.execute(
                    "DELETE FROM transactions WHERE account_id = ?", (account["id"],)
                ).rowcount
                deleted["snapshots"] += conn.execute(
                    "DELETE FROM balance_snapshots WHERE account_id = ?", (account["id"],)
                ).rowcount
                deleted["imports"] += conn.execute(
                    "DELETE FROM imports WHERE account_id = ?", (account["id"],)
                ).rowcount
                conn.execute("DELETE FROM accounts WHERE id = ?", (account["id"],))
        conn.execute("DELETE FROM plaid_items WHERE item_id = ?", (item_id,))

    remove_remote(item["access_token"])  # best-effort; never raises

    logger.info(
        "Unlinked Plaid item %s (%s): %d account(s) %s",
        item_id, item["institution_name"] or "unknown institution",
        len(accounts), "kept" if keep_data else "deleted",
    )
    return {
        "item_id": item_id,
        "institution_name": item["institution_name"],
        "keep_data": keep_data,
        "accounts": len(accounts),
        "deleted": deleted,
    }
