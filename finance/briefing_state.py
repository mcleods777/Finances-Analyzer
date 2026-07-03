"""
Single owner of data/briefing_state.json IO.

Schema (see the AI co-pilot design doc):

    {
      "seen_recurring_merchants": {
        "<merchant_key>": {"first_seen": "YYYY-MM-DD", "monthly_run_rate": 14.99}
      },
      "recent_briefings": [
        {
          "cache_key": "...",
          "rendered_at": "ISO-8601 timestamp",
          "patterns": [...],
          "prose": "..."
        }
      ],
      "daily_cap": {"date": "YYYY-MM-DD", "count": 0}
    }

- `recent_briefings` is bounded at MAX_RECENT_BRIEFINGS entries, newest at
  index 0, oldest evicted on insert.
- All writes are atomic: write to a temp file in the same directory, then
  os.replace(). A crash mid-write can never corrupt the state file.
- A corrupt/unreadable state file is treated as cold start (logged warning),
  never an error.

Both pattern_detector (read-only, via the state dict passed in) and
briefing_writer (Phase 2) go through this module — nothing else writes
briefing_state.json.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime

logger = logging.getLogger(__name__)

STATE_FILENAME = "briefing_state.json"
MAX_RECENT_BRIEFINGS = 7


def _state_path(data_dir: str) -> str:
    return os.path.join(data_dir, STATE_FILENAME)


def _default_state() -> dict:
    return {
        "seen_recurring_merchants": {},
        "recent_briefings": [],
        "daily_cap": {"date": None, "count": 0},
    }


def load_state(data_dir: str) -> dict:
    """
    Load the full briefing state dict.

    Missing file -> cold-start defaults. Corrupt JSON (or a JSON value that
    isn't an object) -> cold-start defaults with a logged warning. Unknown or
    wrongly-typed top-level keys are dropped/repaired so callers can always
    rely on the schema.
    """
    path = _state_path(data_dir)
    if not os.path.exists(path):
        return _default_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.warning(
            "Corrupt briefing state at %s (%s) — treating as cold start", path, exc
        )
        return _default_state()

    if not isinstance(raw, dict):
        logger.warning(
            "Briefing state at %s is not a JSON object — treating as cold start", path
        )
        return _default_state()

    state = _default_state()
    if isinstance(raw.get("seen_recurring_merchants"), dict):
        state["seen_recurring_merchants"] = raw["seen_recurring_merchants"]
    if isinstance(raw.get("recent_briefings"), list):
        state["recent_briefings"] = raw["recent_briefings"][:MAX_RECENT_BRIEFINGS]
    daily_cap = raw.get("daily_cap")
    if isinstance(daily_cap, dict):
        try:
            count = int(daily_cap.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        state["daily_cap"] = {"date": daily_cap.get("date"), "count": count}
    return state


def _write_state(data_dir: str, state: dict) -> None:
    """Atomic write: temp file in the same directory + os.replace."""
    os.makedirs(data_dir, exist_ok=True)
    path = _state_path(data_dir)
    fd, tmp_path = tempfile.mkstemp(
        dir=data_dir, prefix=".briefing_state-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Seen recurring merchants ---


def get_seen_merchants(data_dir: str) -> dict:
    """Return the seen_recurring_merchants mapping (merchant_key -> details)."""
    return load_state(data_dir)["seen_recurring_merchants"]


def add_seen_merchant(
    data_dir: str, merchant_key: str, first_seen: str, monthly_run_rate: float
) -> None:
    """
    Record a merchant as seen so detect_new_recurring never re-surfaces it.
    Idempotent: re-adding an existing key overwrites its details.
    """
    state = load_state(data_dir)
    state["seen_recurring_merchants"][merchant_key] = {
        "first_seen": first_seen,
        "monthly_run_rate": round(float(monthly_run_rate), 2),
    }
    _write_state(data_dir, state)


# --- Briefing cache (recent_briefings, newest first, max 7) ---


def get_cached_briefing(data_dir: str) -> dict | None:
    """Return the most recent briefing entry (recent_briefings[0]) or None."""
    recent = load_state(data_dir)["recent_briefings"]
    return recent[0] if recent else None


def get_recent_briefings(data_dir: str) -> list[dict]:
    """All retained briefings, newest first (for the freshness filter)."""
    return load_state(data_dir)["recent_briefings"]


def set_cached_briefing(
    data_dir: str, cache_key: str, prose: str, patterns: list[dict],
    source: str = "template",
) -> None:
    """
    Prepend a new briefing entry and trim the list to MAX_RECENT_BRIEFINGS.
    `source` records how the prose was produced ("llm" or "template") so the
    UI can indicate it on cache hits.
    """
    state = load_state(data_dir)
    entry = {
        "cache_key": cache_key,
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
        "patterns": patterns,
        "prose": prose,
        "source": source,
    }
    state["recent_briefings"] = (
        [entry] + state["recent_briefings"][: MAX_RECENT_BRIEFINGS - 1]
    )
    _write_state(data_dir, state)


# --- Daily LLM-call cap counter (survives Flask restart) ---


def get_daily_count(data_dir: str, today: date | None = None) -> int:
    """
    Current LLM-call count for `today`. A stored date that doesn't match
    today reads as 0 (date rollover), without writing anything.
    """
    today_str = (today or date.today()).isoformat()
    cap = load_state(data_dir)["daily_cap"]
    return cap["count"] if cap.get("date") == today_str else 0


def increment_daily_count(data_dir: str, today: date | None = None) -> int:
    """
    Increment the daily counter (resetting to 1 on date rollover) and return
    the new count.
    """
    today_str = (today or date.today()).isoformat()
    state = load_state(data_dir)
    cap = state["daily_cap"]
    count = cap["count"] + 1 if cap.get("date") == today_str else 1
    state["daily_cap"] = {"date": today_str, "count": count}
    _write_state(data_dir, state)
    return count
