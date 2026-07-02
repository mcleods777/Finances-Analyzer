from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from finance import db, plaid_sync

# --- Fakes (no real Plaid API is ever hit) ---


def fake_account(account_id="acc-1", name="Plaid Checking", type="depository",
                 subtype="checking", current=1234.56):
    return SimpleNamespace(
        account_id=account_id,
        name=name,
        official_name=name,
        type=type,
        subtype=subtype,
        balances=SimpleNamespace(current=current),
    )


def fake_txn(transaction_id="txn-1", account_id="acc-1", amount=52.30,
             name="WALMART SUPERCENTER #123", merchant_name="Walmart",
             date=datetime.date(2026, 6, 1), pfc_primary=None):
    return SimpleNamespace(
        transaction_id=transaction_id,
        account_id=account_id,
        amount=amount,
        name=name,
        merchant_name=merchant_name,
        date=date,
        personal_finance_category=(
            SimpleNamespace(primary=pfc_primary) if pfc_primary else None
        ),
    )


def sync_page(added=(), modified=(), removed=(), accounts=(),
              next_cursor="cursor-1", has_more=False):
    return SimpleNamespace(
        added=list(added),
        modified=list(modified),
        removed=list(removed),
        accounts=list(accounts),
        next_cursor=next_cursor,
        has_more=has_more,
    )


class FakePlaidClient:
    """Stands in for plaid_api.PlaidApi; serves canned /transactions/sync pages."""

    def __init__(self, pages=None, accounts=None):
        self.pages = list(pages or [])
        self.accounts = list(accounts or [])
        self.sync_requests: list = []

    def transactions_sync(self, request):
        self.sync_requests.append(request)
        return self.pages.pop(0)

    def item_public_token_exchange(self, request):
        return SimpleNamespace(access_token="access-sandbox-token", item_id="item-1")

    def accounts_get(self, request):
        return SimpleNamespace(accounts=self.accounts)


class FailingPlaidClient:
    def __init__(self, body):
        self.body = body

    def transactions_sync(self, request):
        exc = Exception("plaid failure")
        exc.body = self.body
        raise exc


# --- Fixtures / helpers ---


@pytest.fixture
def linked_item(conn):
    """A plaid_items row plus a linked plaid checking account."""
    plaid_sync.ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO plaid_items (item_id, access_token, institution_name) "
            "VALUES ('item-1', 'access-sandbox-token', 'Test Bank')"
        )
        conn.execute(
            "INSERT INTO accounts (name, type, source, plaid_account_id, plaid_item_id) "
            "VALUES ('Test Bank Checking', 'checking', 'plaid', 'acc-1', 'item-1')"
        )
    return conn.execute("SELECT * FROM plaid_items WHERE item_id = 'item-1'").fetchone()


def get_item(conn, item_id="item-1"):
    return conn.execute("SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()


def get_txns(conn):
    return conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()


def run_sync(conn, item, client, classification):
    return plaid_sync.sync_item(
        conn, item, classification=classification, client=client, refresh=False
    )


# --- is_configured ---


def test_is_configured_false_without_credentials(monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)
    assert plaid_sync.is_configured() is False


def test_is_configured_false_with_placeholder_values(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "your-plaid-client-id")
    monkeypatch.setenv("PLAID_SECRET", "your-plaid-sandbox-secret")
    assert plaid_sync.is_configured() is False


def test_is_configured_true_with_real_looking_values(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "abc123")
    monkeypatch.setenv("PLAID_SECRET", "def456")
    assert plaid_sync.is_configured() is True


# --- Account type mapping ---


@pytest.mark.parametrize("ptype,subtype,expected", [
    ("depository", "checking", "checking"),
    ("depository", "savings", "savings"),
    ("depository", "money market", "savings"),
    ("depository", "weird-new-subtype", "checking"),
    ("credit", "credit card", "credit"),
    ("loan", "mortgage", "loan"),
    ("investment", "401k", "investment"),
    ("unknown", None, "checking"),
])
def test_map_account_type(ptype, subtype, expected):
    assert plaid_sync.map_account_type(ptype, subtype) == expected


def test_map_account_type_handles_enum_like_objects():
    enum_like = SimpleNamespace(value="credit")
    assert plaid_sync.map_account_type(enum_like, None) == "credit"


# --- exchange_public_token ---


def test_exchange_stores_item_and_creates_mapped_accounts(conn):
    client = FakePlaidClient(accounts=[
        fake_account("acc-1", "Checking", "depository", "checking"),
        fake_account("acc-2", "Savings", "depository", "savings"),
        fake_account("acc-3", "Rewards Card", "credit", "credit card"),
    ])
    result = plaid_sync.exchange_public_token(
        "public-token", institution_name="Test Bank", conn=conn, client=client
    )
    assert result == {"item_id": "item-1", "institution_name": "Test Bank", "accounts": 3}

    item = get_item(conn)
    assert item["access_token"] == "access-sandbox-token"
    assert item["institution_name"] == "Test Bank"

    accounts = {
        row["name"]: row
        for row in conn.execute("SELECT * FROM accounts").fetchall()
    }
    assert accounts["Test Bank Checking"]["type"] == "checking"
    assert accounts["Test Bank Savings"]["type"] == "savings"
    assert accounts["Test Bank Rewards Card"]["type"] == "credit"
    for row in accounts.values():
        assert row["source"] == "plaid"
        assert row["plaid_item_id"] == "item-1"


def test_exchange_twice_does_not_duplicate_accounts(conn):
    client = FakePlaidClient(accounts=[fake_account("acc-1", "Checking")])
    plaid_sync.exchange_public_token("tok", "Test Bank", conn=conn, client=client)
    plaid_sync.exchange_public_token("tok", "Test Bank", conn=conn, client=client)
    assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM plaid_items").fetchone()["c"] == 1


# --- sync_item: added ---


def test_sync_adds_transactions_with_negated_sign(conn, classification, linked_item):
    client = FakePlaidClient(pages=[sync_page(
        added=[
            fake_txn("txn-1", amount=52.30, merchant_name="Walmart"),   # money out
            fake_txn("txn-2", amount=-1500.00, merchant_name=None,
                     name="ACME CORP PAYROLL"),                          # money in
        ],
        accounts=[fake_account()],
        next_cursor="cursor-a",
    )])
    result = run_sync(conn, linked_item, client, classification)
    assert result["added"] == 2
    assert result["error"] is None

    txns = {row["plaid_transaction_id"]: row for row in get_txns(conn)}
    assert txns["txn-1"]["amount"] == pytest.approx(-52.30)   # Plaid +52.30 out -> ours -52.30
    assert txns["txn-2"]["amount"] == pytest.approx(1500.00)  # Plaid -1500 in -> ours +1500
    assert txns["txn-1"]["description"] == "Walmart"          # merchant_name preferred
    assert txns["txn-2"]["description"] == "ACME CORP PAYROLL"  # falls back to name
    assert txns["txn-1"]["source"] == "plaid"
    assert txns["txn-2"]["txn_type"] == "income"


def test_sync_persists_cursor_and_last_synced(conn, classification, linked_item):
    client = FakePlaidClient(pages=[sync_page(next_cursor="cursor-final")])
    run_sync(conn, linked_item, client, classification)
    item = get_item(conn)
    assert item["sync_cursor"] == "cursor-final"
    assert item["last_synced_at"] is not None
    assert item["last_error"] is None


def test_sync_pages_through_has_more_and_keeps_last_cursor(conn, classification, linked_item):
    client = FakePlaidClient(pages=[
        sync_page(added=[fake_txn("txn-1")], next_cursor="cursor-1", has_more=True),
        sync_page(added=[fake_txn("txn-2", amount=10.0)], next_cursor="cursor-2"),
    ])
    result = run_sync(conn, linked_item, client, classification)
    assert result["added"] == 2
    assert len(client.sync_requests) == 2
    assert get_item(conn)["sync_cursor"] == "cursor-2"


def test_sync_same_transactions_twice_dedups(conn, classification, linked_item):
    def pages():
        return [sync_page(added=[fake_txn("txn-1")], accounts=[fake_account()])]

    first = run_sync(conn, linked_item, FakePlaidClient(pages=pages()), classification)
    item = get_item(conn)  # reload with the stored cursor
    second = run_sync(conn, item, FakePlaidClient(pages=pages()), classification)
    assert first["added"] == 1
    assert second["added"] == 0
    assert db.count_transactions(conn) == 1


def test_sync_applies_rules_before_plaid_category_fallback(conn, classification, linked_item):
    with conn:
        conn.execute(
            "INSERT INTO categorization_rules (category, keyword, priority) "
            "VALUES ('Groceries', 'walmart', 0)"
        )
    client = FakePlaidClient(pages=[sync_page(added=[
        fake_txn("txn-1", merchant_name="Walmart", pfc_primary="GENERAL_MERCHANDISE"),
        fake_txn("txn-2", amount=12.5, merchant_name="Chipotle",
                 pfc_primary="FOOD_AND_DRINK"),
    ])])
    run_sync(conn, linked_item, client, classification)

    txns = {row["plaid_transaction_id"]: row for row in get_txns(conn)}
    assert txns["txn-1"]["category"] == "Groceries"        # DB rule wins
    assert txns["txn-2"]["category"] == "Food And Drink"   # PFC fallback, readable


def test_sync_inserts_plaid_balance_snapshot(conn, classification, linked_item):
    client = FakePlaidClient(pages=[sync_page(
        accounts=[fake_account(current=987.65)],
    )])
    run_sync(conn, linked_item, client, classification)
    snapshots = conn.execute(
        "SELECT * FROM balance_snapshots WHERE source = 'plaid'"
    ).fetchall()
    assert len(snapshots) == 1
    assert snapshots[0]["balance"] == pytest.approx(987.65)

    # Re-sync same day updates the snapshot instead of failing the UNIQUE constraint.
    client2 = FakePlaidClient(pages=[sync_page(accounts=[fake_account(current=1000.00)])])
    run_sync(conn, get_item(conn), client2, classification)
    snapshots = conn.execute(
        "SELECT * FROM balance_snapshots WHERE source = 'plaid'"
    ).fetchall()
    assert len(snapshots) == 1
    assert snapshots[0]["balance"] == pytest.approx(1000.00)


def test_sync_creates_accounts_discovered_mid_sync(conn, classification, linked_item):
    """Accounts added at the institution after link get created on the fly."""
    client = FakePlaidClient(pages=[sync_page(
        added=[fake_txn("txn-9", account_id="acc-new", amount=5.0)],
        accounts=[fake_account(), fake_account("acc-new", "New Savings",
                                               "depository", "savings")],
    )])
    result = run_sync(conn, linked_item, client, classification)
    assert result["added"] == 1
    row = conn.execute(
        "SELECT * FROM accounts WHERE plaid_account_id = 'acc-new'"
    ).fetchone()
    assert row is not None
    assert row["type"] == "savings"


# --- sync_item: modified ---


def test_sync_modified_updates_row(conn, classification, linked_item):
    run_sync(conn, linked_item, FakePlaidClient(pages=[
        sync_page(added=[fake_txn("txn-1", amount=50.0)]),
    ]), classification)

    result = run_sync(conn, get_item(conn), FakePlaidClient(pages=[
        sync_page(modified=[fake_txn("txn-1", amount=55.0, merchant_name="Walmart Updated")]),
    ]), classification)
    assert result["modified"] == 1
    assert result["added"] == 0

    row = get_txns(conn)[0]
    assert row["amount"] == pytest.approx(-55.0)
    assert row["description"] == "Walmart Updated"


def test_sync_modified_skips_user_edited_rows(conn, classification, linked_item):
    run_sync(conn, linked_item, FakePlaidClient(pages=[
        sync_page(added=[fake_txn("txn-1", amount=50.0)]),
    ]), classification)
    with conn:
        conn.execute(
            "UPDATE transactions SET user_edited = 1, category = 'My Category' "
            "WHERE plaid_transaction_id = 'txn-1'"
        )

    result = run_sync(conn, get_item(conn), FakePlaidClient(pages=[
        sync_page(modified=[fake_txn("txn-1", amount=99.0, merchant_name="Overwritten")]),
    ]), classification)
    assert result["modified"] == 0

    row = get_txns(conn)[0]
    assert row["amount"] == pytest.approx(-50.0)
    assert row["description"] == "Walmart"
    assert row["category"] == "My Category"


def test_sync_modified_unknown_transaction_treated_as_added(conn, classification, linked_item):
    result = run_sync(conn, linked_item, FakePlaidClient(pages=[
        sync_page(modified=[fake_txn("txn-never-seen")]),
    ]), classification)
    assert result["modified"] == 0
    assert result["added"] == 1
    assert get_txns(conn)[0]["plaid_transaction_id"] == "txn-never-seen"


# --- sync_item: removed ---


def test_sync_removed_deletes_row(conn, classification, linked_item):
    run_sync(conn, linked_item, FakePlaidClient(pages=[
        sync_page(added=[fake_txn("txn-1")]),
    ]), classification)

    result = run_sync(conn, get_item(conn), FakePlaidClient(pages=[
        sync_page(removed=[SimpleNamespace(transaction_id="txn-1")]),
    ]), classification)
    assert result["removed"] == 1
    assert db.count_transactions(conn) == 0


def test_sync_removed_spares_user_edited_rows(conn, classification, linked_item):
    run_sync(conn, linked_item, FakePlaidClient(pages=[
        sync_page(added=[fake_txn("txn-1")]),
    ]), classification)
    with conn:
        conn.execute("UPDATE transactions SET user_edited = 1")

    result = run_sync(conn, get_item(conn), FakePlaidClient(pages=[
        sync_page(removed=[SimpleNamespace(transaction_id="txn-1")]),
    ]), classification)
    assert result["removed"] == 0
    assert db.count_transactions(conn) == 1


# --- sync_item: errors ---


def test_sync_error_is_stored_not_raised(conn, classification, linked_item):
    client = FailingPlaidClient(
        body='{"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "user must re-auth"}'
    )
    result = run_sync(conn, linked_item, client, classification)
    assert result["error"] is not None
    assert "ITEM_LOGIN_REQUIRED" in result["error"]
    assert "Reconnect needed" in result["error"]

    item = get_item(conn)
    assert "ITEM_LOGIN_REQUIRED" in item["last_error"]
    assert item["sync_cursor"] is None  # cursor untouched on failure
    assert plaid_sync.error_needs_reconnect(item["last_error"]) is True


def test_sync_error_cursor_not_advanced_midway(conn, classification, linked_item):
    """A failure on page 2 must not persist page 1's cursor (data would be lost)."""
    class HalfFailingClient:
        def __init__(self):
            self.calls = 0

        def transactions_sync(self, request):
            self.calls += 1
            if self.calls == 1:
                return sync_page(added=[fake_txn("txn-1")], next_cursor="cursor-1",
                                 has_more=True)
            raise Exception("boom")

    result = run_sync(conn, linked_item, HalfFailingClient(), classification)
    assert result["error"] is not None
    assert get_item(conn)["sync_cursor"] is None
    assert db.count_transactions(conn) == 0  # nothing partially applied


def test_successful_sync_clears_previous_error(conn, classification, linked_item):
    with conn:
        conn.execute("UPDATE plaid_items SET last_error = 'old error'")
    run_sync(conn, get_item(conn), FakePlaidClient(pages=[sync_page()]), classification)
    assert get_item(conn)["last_error"] is None


# --- sync_all / status ---


def test_sync_all_syncs_every_item_and_isolates_failures(conn, classification, linked_item):
    plaid_sync.ensure_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO plaid_items (item_id, access_token, institution_name) "
            "VALUES ('item-2', 'access-token-2', 'Broken Bank')"
        )

    class PerItemClient:
        def transactions_sync(self, request):
            if getattr(request, "access_token", None) == "access-token-2":
                exc = Exception("down")
                exc.body = '{"error_code": "INSTITUTION_DOWN", "error_message": "down"}'
                raise exc
            return sync_page(added=[fake_txn("txn-1")])

    results = plaid_sync.sync_all(
        conn, classification=classification, client=PerItemClient(), refresh=False
    )
    by_item = {r["item_id"]: r for r in results}
    assert by_item["item-1"]["error"] is None
    assert by_item["item-1"]["added"] == 1
    assert "INSTITUTION_DOWN" in by_item["item-2"]["error"]


def test_get_status_reports_items(conn, linked_item):
    with conn:
        conn.execute("UPDATE plaid_items SET last_error = 'Reconnect needed — bank login expired (ITEM_LOGIN_REQUIRED)'")
    status = plaid_sync.get_status(conn)
    assert len(status) == 1
    assert status[0]["institution_name"] == "Test Bank"
    assert status[0]["account_count"] == 1
    assert status[0]["needs_reconnect"] is True


def test_ensure_schema_is_idempotent(conn):
    plaid_sync.ensure_schema(conn)
    plaid_sync.ensure_schema(conn)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(plaid_items)")}
    assert "last_error" in cols
