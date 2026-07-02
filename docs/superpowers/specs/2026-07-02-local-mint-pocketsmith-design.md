# Local Mint/PocketSmith-Style Finance App — Design

**Date:** 2026-07-02
**Status:** Approved (Option A — SQLite core, evolve in place)

## Goal

Evolve the existing finance-tracker (Flask + pandas, CSV/YAML, stateless) into a
locally-run personal finance app comparable to Mint/PocketSmith:

- **Live bank sync** via Plaid (free Trial plan, ~10 institutions) for banks/cards
- **In-app CSV upload** as universal fallback (Fidelity is manual-upload by choice —
  Plaid does not support Fidelity brokerage; Fidelity uses Akoya/Fidelity Access)
- **Account dashboard + net worth over time** (investment accounts: balances only, no holdings)
- **PocketSmith-style calendar cash-flow forecast** (project balances forward from
  recurring bills/income)
- **Smarter categorization** (rule management UI, suggestions for uncategorized,
  bulk re-categorize)
- Preserve all existing analytics: runway bars, budget simulator, pay-period
  spending/income, category trends

Single user, local only, `python app.py` remains the way to run it.

## Architecture Decision (Option A)

Add **SQLite** (stdlib `sqlite3`, no ORM) as the single source of truth for
transactions, accounts, balance snapshots, categorization rules, and Plaid state.
Existing pandas analytics survive intact — the data-loading layer swaps from
CSV-read to DB-read (DB → DataFrame with the same standardized columns:
`date, description, amount, account_name, account_type, category, txn_type`).

Rejected: staying file-based (reinvents a DB badly once dedup/edits/history exist);
full rewrite (throws away ~2,600 lines of tuned, working code).

## Data Model

- `accounts` — id, name, type (checking/savings/credit/investment/loan), source
  (csv/plaid/manual), plaid_account_id, column-mapping JSON (for CSV accounts),
  active flag
- `transactions` — id, account_id, date, description, amount (signed: + in / − out),
  category (nullable), txn_type (income/expense/transfer), dedup_hash (unique),
  source (csv/plaid), plaid_transaction_id (nullable), user_edited flag,
  imported_at
- `balance_snapshots` — id, account_id, date, balance, source (csv/plaid/manual);
  replaces `data/manual_balances.json`
- `categorization_rules` — id, category, keyword, priority; replaces YAML rules
- `plaid_items` — id, item_id, access_token, institution_name, sync_cursor, last_synced_at
- `imports` — id, account_id, filename, imported_at, row_count, duplicate_count
  (audit trail for uploads)

**Dedup:** `dedup_hash = sha256(account_id | date | amount | normalized_description)`,
unique index; importer counts and skips collisions. Plaid rows also dedup by
`plaid_transaction_id`.

**Stays in YAML:** pay period, classification keywords, recurring bills, temporary
expenses, budget overrides. (Rules move to DB; existing YAML rules seeded on first run.)

**Category edits:** manual category set via UI marks `user_edited=1`; re-running
rules never overwrites user-edited rows.

## Components

1. `finance/db.py` — connection, schema init (idempotent `CREATE TABLE IF NOT EXISTS`
   + tiny `schema_version` migration), query helpers returning DataFrames
2. `finance/importer.py` — unified ingest: normalized rows → dedup → insert; used by
   both CSV upload and Plaid sync; wraps existing `csv_reader.py` normalization
3. `finance/plaid_sync.py` — Plaid Link token creation, public-token exchange,
   `/transactions/sync` cursor loop, account linking, balance snapshots on each sync.
   Credentials via `.env` (`PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`);
   `plaid-python` SDK. Works in sandbox mode until user drops in Trial keys.
4. **Blueprints** — split 1,149-line `routes.py` into: `dashboard`, `transactions`,
   `rules`, `accounts` (upload + manual balances + Plaid link/sync), `forecast`.
   `app.py` registers all; shared `base.html` template with nav.
5. **Upload UI** — per-account drag-and-drop on an Accounts page; new-account flow
   with column mapping (reuse config format); import result report (n imported,
   n duplicates skipped)
6. **Net worth** — daily series from balance_snapshots (forward-filled) +
   transaction-derived balances where snapshots absent; assets vs liabilities split;
   manual balance entry UI kept (writes snapshots)
7. **Calendar forecast** — month-grid calendar; each day shows projected inflows/
   outflows from recurring bills + income cadence (from pay_period config) and
   projected total cash balance; horizon ≥ 90 days; reuses recurring-bill match logic
8. **Categorization UX** — rules CRUD against DB; "uncategorized" review queue
   suggesting rules from frequent merchant tokens; bulk re-categorize by filter;
   transaction-row category edit

## Data Flow

CSV upload / Plaid sync → normalize to standard columns → importer (dedup) →
SQLite → `refresh_data()` loads DB into the same in-memory DataFrame shape the
analytics already consume → pages/APIs unchanged in contract.

Startup migration: if DB missing/empty, import existing `data/*.csv` per
config.yaml accounts, seed rules from YAML, import `manual_balances.json` into
balance_snapshots. Idempotent via dedup.

## Error Handling

- Malformed CSV upload → import report with row-level errors, nothing partially
  committed (transaction per file)
- Plaid API failure → surfaced on Accounts page per-item with last-sync time;
  never crashes dashboard
- Missing Plaid credentials → Accounts page shows setup instructions; rest of app
  fully functional (CSV-only mode)
- DB is WAL-mode; single-user Flask dev server, no concurrency ambitions

## Testing

- pytest for: importer dedup (same file twice → 0 new rows), CSV normalization
  variants (debit/credit split cols, sign conventions), rule application respecting
  `user_edited`, net-worth series math, forecast projection math
- Plaid tested against sandbox creds where feasible; sync logic unit-tested with
  faked API responses

## Implementation Waves (orchestration plan)

1. **Foundation** (sequential): db.py, importer, YAML/CSV/manual-balances migration,
   blueprint split, base template, analytics reading from DB, tests green, app runs
2. **Features** (parallel agents): (a) upload UI, (b) Plaid integration,
   (c) net worth + balances; each owns its blueprint + templates
3. **Features 2** (parallel): (d) calendar forecast, (e) categorization UX
4. **Integration QA**: run app, exercise flows end-to-end, fix, commit
