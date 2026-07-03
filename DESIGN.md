# Design System — The Private Wire

Your money doesn't get a dashboard — it gets a newspaper. The app is a private
financial dispatch typeset overnight for an audience of one. The AI briefing is
the lede; every chart is a captioned figure supporting the story.

## Product Context
- **What this is:** Self-hosted personal finance co-pilot (Flask). Daily AI briefing,
  net worth, budget runway, bills, transactions, cash-flow forecast, Plaid sync.
- **Who it's for:** One power user (Shannon). Checked daily.
- **Space:** Personal finance (Copilot Money, Monarch, Mercury are the reference bar).
- **Project type:** Data-dense web app / dashboard, dark-only.
- **Memorable thing:** THE BRIEFING IS THE HERO — "my money talks to me."
  Category is charts-first; this product is narrative-first. Every decision serves that.

## Aesthetic Direction
- **Direction:** Editorial dispatch ("The Private Wire") — financial broadsheet, not dashboard.
- **Decoration level:** Minimal-intentional. Hairline rules, not card boxes. Typography
  does the work. The briefing is distinguished by NOT being in a card: it sits on raw
  background, bounded by a scotch rule (double hairline), the only serif block on the page.
- **Mood:** Calm, private, authored. First-3-seconds target: "my money wrote me a letter."
  Never: dashboard vigilance, fintech playfulness, "an AI feature was added."
- **Anti-patterns (hard bans):** tinted/gradient "AI glow" boxes, purple anything,
  icon-in-circle decoration, card mosaics, bubbly uniform border-radius, blue slate.

## Typography — three voices, strictly separated
Prose is always serif. Numbers are never serif. Labels whisper in mono.

- **Reading voice (briefing prose, figure annotations): Newsreader**
  (Google Fonts, variable, opsz axis on, true italics). Briefing body 20px/1.58,
  max-width 62ch, weight 400; emphasis = 500 italic (an editor's pen, never a highlight
  box). Drop cap on the briefing's first letter: 500, ~3 lines, brass. Figure
  annotations: 14.5px italic muted.
- **Data voice (all numbers, UI chrome, tables): IBM Plex Sans** 400/500/600.
  `font-variant-numeric: tabular-nums` on every numeric cell/stat. Numbers inside
  briefing prose are set in Plex Sans at 0.95em (typeset into the serif text).
  Table body 13.5px; UI body/nav 13–14px.
- **Wire voice (microtype): IBM Plex Mono** 400/500 — 11px, uppercase,
  letter-spacing 0.08em, muted. Used for: datelines, section labels, table column
  headers, figure captions (`FIG. 1 — RUNWAY`), timestamps, the briefing sign-off
  (`— YOUR CFO, 06:00`).
- **Loading:** Google Fonts CDN, preconnect ×2, display=swap:
  `Newsreader:ital,opsz,wght@0,6..72,400..600;1,6..72,400..600`, `IBM Plex Sans 400/500/600`,
  `IBM Plex Mono 400/500`.
- **Scale:** microtype 11 / table 13.5 / UI 14 / annotation 14.5 / rail value 22 /
  briefing 20 / masthead 21 / drop cap ~64.

## Color — Ink & Newsprint (CSS variables, dark-only)
- **Approach:** Restrained. One accent, used scarcely — when everything whispers,
  the one amber word is loud.

| Token | Hex | Usage |
|---|---|---|
| `--ink` | `#121110` | Page background (warm ink-black — never blue-gray) |
| `--surface` | `#1B1917` | Table hover, cards where unavoidable |
| `--elevated` | `#242019` | Popovers, modals, sticky elements |
| `--rule` | `#33302A` | ALL borders — 1px hairlines only |
| `--text` | `#EDE8DF` | Primary text (newsprint white, warm) |
| `--muted` | `#97907E` | Labels, secondary, microtype |
| `--brass` | `#D9A03F` | THE accent: links, drop cap, focus rings, active nav, primary buttons. Never large areas. |
| `--ledger-green` | `#7CAF7E` | Money in / positive deltas (desaturated, ink-like) |
| `--red-ink` | `#CE6A57` | Money out / negative deltas (brick, not alarm) |

- **Semantic:** success `--ledger-green`, error/danger `--red-ink`, warning `--brass`,
  info `--muted`. Chart series derive from brass/green + muted earth tones
  (`#8A6F3B`, `#6B5A34`, `#3A362E`) — never rainbow palettes.
- **Contrast:** brass on ink passes for text ≥15px; use `--text` with brass underline
  for small links.

## Spacing
- **Base unit:** 8px (4px allowed in table cells).
- **Density:** Comfortable on the front page (dashboard), compact in tables
  (rows ~45px, cell padding 10px 14px).
- **Scale:** 4 / 8 / 12 / 16 / 24 / 32 / 44 / 56 / 64.

## Layout
- **Approach:** Hybrid — editorial composition on the dashboard ("the front page"),
  grid-disciplined on data pages (transactions, accounts, categories, rules).
- **Front page order:** masthead (name small-caps left, Plex Mono dateline right,
  quiet text nav) → scotch rule → THE LEDE (briefing, cols 1–8: drop cap, 62ch,
  entity links in brass that scroll to their evidence, sign-off) → TODAY rail
  (cols 9–12: net worth line + sparkline, next 3–4 bills, income) → figures
  (`FIG. N — TITLE` captions, hairline rules, one-line serif annotations) →
  the ledger (recent transactions).
- **Grid:** 12 columns, max 1200px, 40px page gutters (20px mobile).
- **Border radius:** 0–2px. Square corners — this is a broadsheet, not an app store.
- **Rules over boxes:** prefer `border-top: 1px solid var(--rule)` separators to
  wrapped/boxed cards. Scotch rule = 1px rule + second 1px line 3px below.

## Motion — exactly three
- **Approach:** Intentional-minimal. Easing: enter ease-out, exit ease-in.
1. **Ink settles:** first briefing view of the day — lines fade in staggered
   (60ms/line, 500ms each, 4px rise), once per day (localStorage flag by date).
2. **Prose→evidence:** entity link underline draws left-to-right 160ms on hover;
   click smooth-scrolls and pulses the target figure caption in brass (600ms).
3. **Runway fill:** bars fill 400ms ease-out on first viewport entry, once.
- No loops, no shimmer, no hover lifts, no `transition: all`. Respect
  `prefers-reduced-motion` (disable all three).

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-03 | Initial system: "The Private Wire" editorial dispatch | /design-consultation: research (Mercury/Copilot restraint trend) + outside-voice synthesis; serves "briefing is the hero." User approved via rendered preview. |
| 2026-07-03 | Numbers demoted below the lede, not deleted | Softened outside-voice's "no hero number" — daily utility of stats preserved in TODAY rail. |
