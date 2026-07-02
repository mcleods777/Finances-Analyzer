# Changelog

All notable changes to the Personal Finance Dashboard.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses 4-digit semantic versioning (MAJOR.MINOR.PATCH.MICRO).

## [0.2.0.0] - 2026-07-02

Mint/PocketSmith-style release: the app moves from a stateless CSV/YAML
dashboard to a stateful local finance platform with live bank sync.

### Added
- **SQLite storage** (`data/finance.db`, WAL mode): transactions, accounts,
  balance snapshots, categorization rules, Plaid state, and import audit
  history now persist in one database. Startup migration seeds it from
  config.yaml, existing `data/*.csv`, and `manual_balances.json` —
  idempotent via content-hash dedup (3,728 transactions migrated exactly).
- **In-app CSV upload** on the new Accounts page: per-account drag-and-drop
  with imported/duplicate/error reporting, per-account import history, and
  an "Add account from CSV" flow with column-mapping preview and
  auto-detected date formats. Raw uploads archived under `data/uploads/`.
- **Plaid live bank sync** (free Trial plan): Link flow, token exchange,
  `/transactions/sync` cursor loop with add/modify/remove handling,
  automatic balance snapshots, per-institution status + error surfacing.
  Fully optional — without `.env` credentials the app runs CSV-only and the
  Accounts page shows setup instructions.
- **Net worth tracking**: snapshot-anchored daily series with
  assets-vs-liabilities split, headline number with 30d/90d change pills,
  3M/6M/1Y/All range selector, and a quick-update panel for manual
  (Fidelity) balances with staleness badges.
- **Calendar cash-flow forecast** (`/forecast`): PocketSmith-style month
  grid projecting recurring bills, paychecks (median per-pay-period income),
  and temporary expenses up to 365 days, with running projected balances,
  negative-balance warnings, and monthly in/out/net summaries.
- **Smarter categorization**: inline category editing on the transactions
  page (user edits are never clobbered by rules), an uncategorized review
  queue on the categories page grouping transactions by normalized merchant
  with one-click categorize-all and optional rule creation, and id-based
  bulk re-categorization.
- **Test suite**: 120 pytest tests covering the importer, dedup, migration
  idempotency, Plaid sync semantics, net-worth math, forecast projections,
  and categorization endpoints.

### Changed
- `finance/routes.py` (1,149 lines) split into blueprints:
  `dashboard`, `transactions`, `rules`, `accounts`, `forecast`, `plaid`.
- Templates now extend a shared `base.html` with a common nav.
- Categorization rules and manual balances are DB-backed; the rules API
  no longer writes to config.yaml (YAML remains the first-run seed).
- New dependencies: `plaid-python`, `python-dotenv` (see `.env.example`).

## [0.1.0.0] - 2026-05-04

### Added
- Category spending trends chart on the dashboard. 12-month default with
  2/3/6/12/24-month range buttons. One line per subcategory, sorted by total
  spend, click any line to drill into that category's transactions for the
  selected window.
- Multi-select category and account filtering on the transactions page. Pick
  any combination of subcategories (Uncategorized is a first-class option) and
  any combination of accounts. URLs use comma-separated params so filter
  state shares cleanly.
- Sortable columns on the transactions table: Date, Description, Type,
  Category, Account, Amount.
- "Make Recurring" + click-badge-to-remove flow for tagging or untagging a
  transaction as part of a recurring bill.
- Per-segment tooltips on the runway bars showing each bill's contribution.
- `search_keyword` field on bill-status API responses for downstream
  drill-down to matching transactions.

### Changed
- `finance/routes.py` now imports pandas at module level (was inlined in five
  places, which would have NameError'd if a code path skipped both inline
  imports).
- The recurring-bill remove badge uses `data-bill` + `dataset.bill` instead
  of inline `onclick` string interpolation, avoiding apostrophe-escape
  issues when bill names contain special characters.

### Fixed
- The trends chart "1 Year" range was rendering 13 month columns instead of
  12 (off-by-one in `pd.DateOffset(months=...)` + inclusive period_range).
- The "Uncategorized" line on the trends chart silently showed zero spend
  even when it was the largest bucket. The detector now normalizes NaN and
  empty subcategories to "Uncategorized" before filtering.
- Dynamic query-string interpolations in `templates/transactions.html` now
  URL-encode `current_search`, `current_category`, and `current_account`,
  so spaces, ampersands, and other special characters round-trip cleanly.

### Tooling
- Added `.gitattributes` enforcing LF line endings globally (prevents
  Windows-side editors on WSL from silently rewriting templates with CRLF
  and creating noise diffs — `categories.html` and `rules.html` had picked
  up CRLF before this change and were reverted in this release).
- Added skill-routing rules to `CLAUDE.md` so Claude Code auto-invokes the
  right gstack skills.
