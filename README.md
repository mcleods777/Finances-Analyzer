# The Private Wire

A self-hosted personal finance co-pilot. Your money doesn't get a dashboard —
it gets a newspaper: a daily AI-written briefing leads the page, backed by
net-worth tracking, budget runway, bill tracking, a cash-flow forecast
calendar, and live bank sync. Single-user, runs entirely on your machine;
your financial data never leaves it except to the APIs you configure.

## Features

- **Daily briefing** — 3–5 sentences of "hallway CFO" prose generated from
  deterministic pattern detectors (new recurring charges, category drift,
  spending anomalies, missing bills, runway variance). Uses the Claude API
  when a key is configured; falls back to templated prose without one.
- **Live bank sync** via Plaid (free Trial plan covers ~10 institutions).
- **CSV upload** for anything Plaid can't reach — drag-and-drop per account,
  duplicate detection, add-new-account flow with column mapping.
- **Net worth** over time with balance snapshots (manual entry supported for
  investment accounts), assets/liabilities split.
- **Budget runway** per half-month with committed bills and free cash.
- **Calendar forecast** — projected bills, paychecks, and running balance up
  to a year out.
- **Transactions** — paginated ledger with filters, inline category editing,
  bulk re-categorization, an uncategorized review queue, and keyword rules.
- **Account management** — rename, merge (with overlap dedup), hide, exclude
  from net worth, delete, unlink banks.

## Quick start

```bash
git clone https://github.com/mcleods777/Finances-Analyzer.git
cd Finances-Analyzer
pip install -r requirements.txt

cp config.example.yaml config.yaml   # pay period, accounts, bills — edit to taste
cp .env.example .env                 # API keys — optional, see below

python3 app.py
```

Open **http://localhost:5000**. On first run the app creates
`data/finance.db` and imports any CSVs configured in `config.yaml`. The app
is fully functional with no API keys — you just won't have live bank sync or
AI-written briefings until you add them.

## API keys (optional, in `.env`)

| Key | What it enables | Where to get it |
|---|---|---|
| `PLAID_CLIENT_ID` / `PLAID_SECRET` | Live bank sync ("Connect a bank" on the Accounts page) | [dashboard.plaid.com/signup](https://dashboard.plaid.com/signup) — choose "Personal use"; the free Trial plan is auto-approved. Set `PLAID_ENV=production` for real banks. |
| `ANTHROPIC_API_KEY` | AI-written briefings (Claude Haiku, ~$0.30/mo at daily use, hard-capped at 20 calls/day) | [console.anthropic.com](https://console.anthropic.com) |

## Your data (what git does and doesn't carry)

`data/` (the SQLite database, CSVs, uploads), `config.yaml`, and `.env` are
**gitignored** — they never reach GitHub. Cloning this repo on a new machine
gives you the app with an empty database. To move your data to another
machine, copy those three things directly; to start fresh, just link your
banks and upload CSVs again.

## Everyday use

- **Import transactions:** automatic via Plaid sync, or drop a bank CSV onto
  an account on the **Accounts** page. Duplicates are detected by content, so
  overlapping files are safe.
- **Categorize:** click any category cell on **Transactions** to edit inline;
  the **Categories** page has a review queue that groups uncategorized
  spending by merchant for one-click cleanup.
- **Bills:** managed in `config.yaml` (`recurring_bills`) or via the
  Recurring Bills section on the dashboard. Matching survives bank descriptor
  changes via multiple keywords per bill and amount-based disambiguation.
- **Fidelity / investment balances:** entered manually via the Update
  Balances panel on the dashboard (Plaid doesn't support Fidelity).

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q        # 289 tests
```

- `finance/` — application code (blueprints, importer, Plaid sync, pattern
  detectors, briefing writer, analytics).
- `DESIGN.md` — the design system ("The Private Wire") and source of truth
  for all visual decisions.
- `CHANGELOG.md` — release history.
