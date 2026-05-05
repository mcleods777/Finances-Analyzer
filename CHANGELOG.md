# Changelog

All notable changes to the Personal Finance Dashboard.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses 4-digit semantic versioning (MAJOR.MINOR.PATCH.MICRO).

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
