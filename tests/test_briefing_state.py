from __future__ import annotations

import json
import os
from datetime import date

from finance import briefing_state


def _read_raw(data_dir: str) -> dict:
    with open(os.path.join(data_dir, briefing_state.STATE_FILENAME), encoding="utf-8") as f:
        return json.load(f)


# --- Cold start (no file) ---


def test_cold_start_load_state_defaults(tmp_path):
    state = briefing_state.load_state(str(tmp_path))
    assert state == {
        "seen_recurring_merchants": {},
        "recent_briefings": [],
        "daily_cap": {"date": None, "count": 0},
    }


def test_cold_start_getters(tmp_path):
    data_dir = str(tmp_path)
    assert briefing_state.get_seen_merchants(data_dir) == {}
    assert briefing_state.get_cached_briefing(data_dir) is None
    assert briefing_state.get_recent_briefings(data_dir) == []
    assert briefing_state.get_daily_count(data_dir) == 0


def test_reads_do_not_create_the_file(tmp_path):
    data_dir = str(tmp_path)
    briefing_state.load_state(data_dir)
    briefing_state.get_cached_briefing(data_dir)
    briefing_state.get_daily_count(data_dir)
    assert not os.path.exists(os.path.join(data_dir, briefing_state.STATE_FILENAME))


def test_write_creates_missing_data_dir(tmp_path):
    data_dir = str(tmp_path / "nested" / "data")
    briefing_state.add_seen_merchant(data_dir, "netflix", "2026-06-01", 15.49)
    assert briefing_state.get_seen_merchants(data_dir) == {
        "netflix": {"first_seen": "2026-06-01", "monthly_run_rate": 15.49}
    }


# --- Corrupt JSON recovery ---


def _write_corrupt(tmp_path, content: str) -> str:
    data_dir = str(tmp_path)
    with open(os.path.join(data_dir, briefing_state.STATE_FILENAME), "w", encoding="utf-8") as f:
        f.write(content)
    return data_dir


def test_corrupt_json_treated_as_cold_start_with_warning(tmp_path, caplog):
    data_dir = _write_corrupt(tmp_path, "{not valid json!!!")
    with caplog.at_level("WARNING"):
        state = briefing_state.load_state(data_dir)
    assert state["seen_recurring_merchants"] == {}
    assert state["recent_briefings"] == []
    assert "cold start" in caplog.text


def test_non_object_json_treated_as_cold_start(tmp_path, caplog):
    data_dir = _write_corrupt(tmp_path, '["a", "b"]')
    with caplog.at_level("WARNING"):
        state = briefing_state.load_state(data_dir)
    assert state == briefing_state.load_state(str(tmp_path / "missing"))


def test_wrongly_typed_keys_are_repaired(tmp_path):
    data_dir = _write_corrupt(
        tmp_path,
        json.dumps(
            {
                "seen_recurring_merchants": ["not", "a", "dict"],
                "recent_briefings": {"not": "a list"},
                "daily_cap": {"date": "2026-06-01", "count": "oops"},
            }
        ),
    )
    state = briefing_state.load_state(data_dir)
    assert state["seen_recurring_merchants"] == {}
    assert state["recent_briefings"] == []
    assert state["daily_cap"] == {"date": "2026-06-01", "count": 0}


def test_corrupt_file_is_recoverable_by_next_write(tmp_path):
    data_dir = _write_corrupt(tmp_path, "garbage")
    briefing_state.add_seen_merchant(data_dir, "spotify", "2026-06-02", 11.99)
    assert briefing_state.get_seen_merchants(data_dir) == {
        "spotify": {"first_seen": "2026-06-02", "monthly_run_rate": 11.99}
    }


# --- Atomic-write preservation in both directions ---


def test_set_cached_briefing_preserves_seen_merchants(tmp_path):
    data_dir = str(tmp_path)
    briefing_state.add_seen_merchant(data_dir, "netflix", "2026-06-01", 15.49)
    briefing_state.set_cached_briefing(data_dir, "key-1", "Some prose.", [{"pattern_type": "anomaly"}])
    assert briefing_state.get_seen_merchants(data_dir) == {
        "netflix": {"first_seen": "2026-06-01", "monthly_run_rate": 15.49}
    }
    cached = briefing_state.get_cached_briefing(data_dir)
    assert cached["cache_key"] == "key-1"
    assert cached["prose"] == "Some prose."
    assert cached["patterns"] == [{"pattern_type": "anomaly"}]
    assert "rendered_at" in cached


def test_add_seen_merchant_preserves_cached_briefing(tmp_path):
    data_dir = str(tmp_path)
    briefing_state.set_cached_briefing(data_dir, "key-1", "Prose.", [])
    briefing_state.add_seen_merchant(data_dir, "flo", "2026-06-10", 14.99)
    cached = briefing_state.get_cached_briefing(data_dir)
    assert cached["cache_key"] == "key-1"
    assert briefing_state.get_seen_merchants(data_dir)["flo"]["monthly_run_rate"] == 14.99


def test_increment_daily_count_preserves_other_sections(tmp_path):
    data_dir = str(tmp_path)
    briefing_state.add_seen_merchant(data_dir, "netflix", "2026-06-01", 15.49)
    briefing_state.set_cached_briefing(data_dir, "key-1", "Prose.", [])
    briefing_state.increment_daily_count(data_dir, today=date(2026, 6, 15))
    assert "netflix" in briefing_state.get_seen_merchants(data_dir)
    assert briefing_state.get_cached_briefing(data_dir)["cache_key"] == "key-1"


def test_writes_leave_no_tmp_files_and_valid_json(tmp_path):
    data_dir = str(tmp_path)
    briefing_state.add_seen_merchant(data_dir, "a", "2026-01-01", 1.0)
    briefing_state.set_cached_briefing(data_dir, "k", "p", [])
    briefing_state.increment_daily_count(data_dir)
    leftovers = [name for name in os.listdir(data_dir) if name.endswith(".tmp")]
    assert leftovers == []
    raw = _read_raw(data_dir)  # must parse as valid JSON
    assert set(raw) == {"seen_recurring_merchants", "recent_briefings", "daily_cap"}


def test_add_seen_merchant_overwrites_existing_key(tmp_path):
    data_dir = str(tmp_path)
    briefing_state.add_seen_merchant(data_dir, "netflix", "2026-06-01", 15.49)
    briefing_state.add_seen_merchant(data_dir, "netflix", "2026-06-01", 17.99)
    merchants = briefing_state.get_seen_merchants(data_dir)
    assert len(merchants) == 1
    assert merchants["netflix"]["monthly_run_rate"] == 17.99


# --- recent_briefings bounding (max 7, newest first) ---


def test_recent_briefings_newest_first_and_bounded_at_seven(tmp_path):
    data_dir = str(tmp_path)
    for i in range(10):
        briefing_state.set_cached_briefing(data_dir, f"key-{i}", f"prose {i}", [])
    recent = briefing_state.get_recent_briefings(data_dir)
    assert len(recent) == briefing_state.MAX_RECENT_BRIEFINGS == 7
    assert [entry["cache_key"] for entry in recent] == [
        "key-9", "key-8", "key-7", "key-6", "key-5", "key-4", "key-3",
    ]
    assert briefing_state.get_cached_briefing(data_dir)["cache_key"] == "key-9"


def test_load_state_trims_oversized_recent_briefings(tmp_path):
    data_dir = _write_corrupt(
        tmp_path,
        json.dumps(
            {
                "seen_recurring_merchants": {},
                "recent_briefings": [{"cache_key": f"k{i}"} for i in range(12)],
                "daily_cap": {"date": None, "count": 0},
            }
        ),
    )
    state = briefing_state.load_state(data_dir)
    assert len(state["recent_briefings"]) == 7
    assert state["recent_briefings"][0]["cache_key"] == "k0"


# --- Daily-cap counter with date rollover ---


def test_daily_count_increments_within_same_day(tmp_path):
    data_dir = str(tmp_path)
    today = date(2026, 6, 15)
    assert briefing_state.increment_daily_count(data_dir, today=today) == 1
    assert briefing_state.increment_daily_count(data_dir, today=today) == 2
    assert briefing_state.get_daily_count(data_dir, today=today) == 2


def test_daily_count_rolls_over_on_new_date(tmp_path):
    data_dir = str(tmp_path)
    yesterday = date(2026, 6, 14)
    today = date(2026, 6, 15)
    briefing_state.increment_daily_count(data_dir, today=yesterday)
    briefing_state.increment_daily_count(data_dir, today=yesterday)
    # Read with a newer date: stale counter reads as 0 without a write
    assert briefing_state.get_daily_count(data_dir, today=today) == 0
    assert _read_raw(data_dir)["daily_cap"] == {"date": "2026-06-14", "count": 2}
    # Increment on the new date resets to 1
    assert briefing_state.increment_daily_count(data_dir, today=today) == 1
    assert _read_raw(data_dir)["daily_cap"] == {"date": "2026-06-15", "count": 1}


def test_daily_count_defaults_to_real_today(tmp_path):
    data_dir = str(tmp_path)
    assert briefing_state.increment_daily_count(data_dir) == 1
    assert briefing_state.get_daily_count(data_dir) == 1
    assert _read_raw(data_dir)["daily_cap"]["date"] == date.today().isoformat()
