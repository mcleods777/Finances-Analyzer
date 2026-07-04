# The Desk — Conversational CFO, Insight Archive, and Dossier

**Date:** 2026-07-04 · **Status:** Approved
**User decisions:** models = Haiku 4.5 / Sonnet 5 / Opus 4.8 with **Sonnet 5 default**;
memory = editable profile page **plus** AI auto-learn (user can edit/delete anything).

## Goal

Extend the AI co-pilot from a one-way daily briefing into a two-way advisor:

1. **The Desk** (`/desk`) — real-time chat with the CFO about your finances. The
   model answers by querying live data through tools (agentic loop), in the same
   hallway-CFO voice as the briefing.
2. **Model & intelligence control** — per-conversation picker: model
   (Haiku ~1¢ / Sonnet ~5¢ / Opus ~15-25¢ per question) and intelligence
   (Standard / Deep), persisted per conversation.
3. **The Archive** (`/archive`) — every insight ever generated (daily briefings
   AND chat-surfaced insights) in a permanent, browsable log.
4. **Novelty** — both the briefing writer and the chat advisor read the Archive
   before generating, so insights don't repeat; repeated territory must add
   something new (magnitude change, new angle) or be skipped.
5. **The Dossier** — the advisor's memory of YOU: goals, financial weaknesses,
   debts to pay off, notes. User-editable page + the AI adds entries from
   conversations (clearly badged, one-click delete). Injected into every
   briefing and chat so advice is personal and it "learns as it goes".

## Anthropic API facts (authoritative — from claude-api skill, 2026-06)

- Model IDs exactly: `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-4-8`.
- **No `budget_tokens` on Sonnet 5 / Opus 4.8** (400). No `temperature`/`top_p`/`top_k`
  (400 on non-default). Thinking:
  - Sonnet 5: omit `thinking` → adaptive ON by default; `{type:"disabled"}` to turn off.
  - Opus 4.8: omit → off; set `{type:"adaptive"}` explicitly for thinking.
  - Haiku 4.5: pre-4.6 surface → `{type:"enabled", budget_tokens:N}` (N ≥1024, < max_tokens);
    no `effort` support (400).
- **Intelligence mapping** (UI: Standard / Deep):
  | Model | Standard | Deep |
  |---|---|---|
  | Haiku 4.5 | no thinking | `thinking:{enabled, budget_tokens:8000}` |
  | Sonnet 5 | adaptive (omit) + effort omitted (=high) | adaptive + `output_config:{effort:"xhigh"}` |
  | Opus 4.8 | `thinking:{adaptive}` (explicit) | adaptive + `output_config:{effort:"xhigh"}` |
- Prompt caching: prefix-match; render order tools→system→messages. `cache_control:
  {type:"ephemeral"}` on the LAST system block caches tools+system. System prompt must
  be byte-stable: NO dates, NO profile, NO dynamic numbers in it. Volatile context goes
  in the first user turn. Tools list deterministic (fixed order). Note per-model minimum
  cacheable prefix (Opus 4.8: 4096 tokens; Sonnet 5: 2048) — cache is best-effort.
- Tool loop: manual agentic loop; stop on `end_turn`; cap iterations (8); ALL
  `tool_result` blocks for one assistant turn go back in ONE user message;
  `is_error: true` for failed tools; parse `block.input` as objects (SDK does).
- Multi-turn: stateless API — store and resend full message history INCLUDING
  assistant `tool_use` content blocks and tool_result turns. Thinking blocks must be
  passed back unchanged on the same model; when the user switches model mid-
  conversation, other models tolerate/drop them (Sonnet 5/Opus adaptive blocks
  replay fine; simplest correct approach: store full content blocks verbatim).
- `max_tokens`: 8000 non-streaming (v1 is non-streaming; responses 2–15s; UI shows
  typing state with AbortController timeout 120s).
- Handle `stop_reason == "refusal"` (surface a readable message) and `max_tokens`
  (surface truncation). Catch typed SDK errors most-specific-first
  (RateLimitError → APIStatusError → APIConnectionError).

## Data model (schema v3, additive)

- `conversations` — id, title (first-question snippet, editable later), model,
  intelligence, created_at, updated_at, archived flag.
- `chat_messages` — id, conversation_id, role ('user'|'assistant'), content_json
  (the exact API-shape content blocks, verbatim, incl. tool_use/tool_result turns),
  display_text (extracted text for fast rendering), created_at, usage_json
  (input/output/cache tokens per assistant turn).
- `insights` — id, created_at, source ('briefing'|'chat'), text, fingerprints_json
  (pattern fingerprints when from briefing; NULL for chat), model, conversation_id
  (nullable FK). Append-only. THE log both humans and models read.
- `profile_entries` — id, section ('goal'|'weakness'|'debt'|'note'), text, source
  ('user'|'ai'), created_at, updated_at, active (soft delete).

Briefing writer change: every generated briefing (LLM or template) is ALSO
appended to `insights` (source='briefing', with its pattern fingerprints).
`briefing_state.json` stays for cache/dedup mechanics — the Archive is the
permanent record. Backfill on migration: import `recent_briefings` from
briefing_state.json into insights (best-effort, oldest available history).

## The advisor (finance/advisor.py)

Manual agentic loop, sync client (`anthropic.Anthropic()`, dotenv loaded like
plaid_sync). Tools (fixed order, compact JSON results, every free-text user-data
field — merchant names, descriptions, category names — wrapped `<data>…</data>`
with html.escape, per the existing briefing_writer._wrap pattern):

1. `get_overview()` — net worth + 30d change, account list w/ balances, current
   pay-period spend/free, income this month. (No args.)
2. `aggregate_transactions(group_by, start_date?, end_date?, category?, account?, top_n=15)`
   — group_by ∈ category|merchant|month|account|day; returns rollups
   {key, total, count, avg}. The workhorse — protects context from raw dumps.
3. `search_transactions(query?, category?, account?, start_date?, end_date?, min_amount?, max_amount?, limit=25)`
   — raw rows (date, description, amount, category, account), hard cap 50 + total count.
4. `get_bills()` — recurring bills w/ amounts, due days, paid/pending this period,
   half-month committed totals.
5. `get_forecast(horizon_days=90)` — projected balances, min-balance warning,
   monthly in/out/net (reuses finance/forecast.py).
6. `run_detectors()` — current pattern-detector output (headlines + magnitudes).
7. `search_insights(query?, source?, limit=20)` — search the Archive (substring,
   newest first). For novelty checks and "what did you tell me before".
8. `save_insight(text)` — append to Archive (source='chat'). The system prompt
   instructs: save conclusions that are NOVEL vs the Archive and materially
   useful; don't save restatements.
9. `get_profile()` — active dossier entries by section.
10. `add_profile_entry(section, text)` / `update_profile_entry(id, text?, active?)`
    — AI-writable memory; every AI write is source='ai' and gets surfaced in the
    chat UI as a chip ("Added to dossier: …") with the entry id for undo.

System prompt (byte-stable, cache_control on last block): the Private Wire CFO
persona (same voice as briefing), tool-usage guidance (prefer aggregate over
search; check search_insights before presenting something as new; when the user
states a goal/debt/weakness, add it to the dossier and say so), the verbatim
<data>-tag injection-defense clause, formatting rules (concrete numbers, no
bullet-list dumps, short paragraphs).

First user turn gets a `<context>` block (after cache breakpoint): today's date,
pay-period bounds, dossier snapshot, the 10 most recent Archive entries, latest
briefing prose. Rebuilt fresh per request (cheap, uncached by design).

Guards: `advisor` config section (config.yaml + example): default_model
(claude-sonnet-5), max_per_day (default 100, shared counter in briefing_state
daily_cap style but its own key), max_loop_iterations (8), enabled (true).
No API key → /desk shows setup hint (same pattern as briefing). Insight/profile
writes happen through tools only when the model calls them — code never
auto-saves model prose wholesale.

## Endpoints

- `POST /api/chat` {conversation_id?, message, model?, intelligence?} → creates
  conversation if absent (title = first 60 chars) → runs loop → {conversation_id,
  reply, tool_activity: [{tool, summary}], insights_saved: [...], profile_changes:
  [...], usage {tokens, est_cost}}. Errors: 400 bad model/intelligence, 429 daily
  cap, 503 unconfigured/no data (per briefing conventions; never 500).
- `GET /api/conversations` · `GET /api/conversations/<id>` (messages for render)
  · `PATCH` (rename) · `DELETE`.
- `GET /api/insights?source=&q=&page=` · `DELETE /api/insights/<id>` (user curation).
- `GET/POST/PATCH/DELETE /api/profile` (sections, entries; AI-added badge data).

## UI (all per DESIGN.md — The Private Wire)

- **/desk — "The Desk"**: nav entry after Dashboard. Left rail: conversation list
  (microtype dates, quiet rows) + "New conversation". Main: messages — user turns
  right-quiet, CFO turns as serif prose (Newsreader, like the briefing; numbers in
  Plex tabular via the existing wrapProseFigures approach); tool activity as
  collapsed microtype line ("consulted: aggregate_transactions · get_bills");
  dossier/insight chips when writes happen. Composer: textarea + brass send;
  model picker (HAIKU/SONNET/OPUS microtype segmented control) + intelligence
  toggle (STANDARD/DEEP) + est. cost hint; disabled state while thinking with
  "THE CFO IS THINKING…" microtype shimmer-free indicator. Enter sends,
  Shift+Enter newline. Errors inline, retry link.
- **/archive — "The Archive"**: chronological insight log, dateline-grouped
  (broadsheet morgue aesthetic): microtype date + source tag (BRIEFING/DESK),
  serif insight text, source-conversation link for chat insights, search box,
  delete affordance. The briefing card on the dashboard gets a quiet
  "view the archive →" link.
- **Dossier**: top section of /archive (or its own anchor): four labeled columns/
  groups (GOALS / WEAKNESSES / DEBTS / NOTES), hairline rows, inline add/edit,
  AI-added entries carry a small brass "AI" mark, delete = soft-deactivate.

## Testing

Mock the Anthropic client at a `_make_client()` seam (briefing_writer precedent).
tests: loop mechanics (tool_use → tool_result single message, multi-tool parallel,
iteration cap, end_turn), per-model request shape (Haiku budget_tokens vs Sonnet
omitted-adaptive vs Opus explicit-adaptive; Deep → xhigh effort; NEVER
temperature), cache_control present on system + absence of dynamic bytes in
system, tool functions against tmp DBs (aggregates, caps, data-wrapping),
insight append + briefing→insights, novelty context injection, profile CRUD +
ai-source badging, daily cap, refusal + error paths, endpoint contracts.
Full suite must stay green (301 now).

## Out of scope (v1)

Streaming responses (SSE), insight embeddings/semantic search, multi-user,
charts inside chat replies, voice.
