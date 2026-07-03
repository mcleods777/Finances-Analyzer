from __future__ import annotations

from finance.config_loader import BriefingConfig, _parse_briefing, load_config

MINIMAL_CONFIG = """
pay_period:
  start_date: '2026-01-05'
  frequency_days: 14
accounts: []
"""


def test_parse_briefing_defaults_when_absent():
    cfg = _parse_briefing(None)
    assert cfg == BriefingConfig(model="claude-haiku-4-5", max_per_day=20, enabled=True)


def test_parse_briefing_explicit_values():
    cfg = _parse_briefing({"model": "claude-sonnet-4-6", "max_per_day": 5, "enabled": False})
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.max_per_day == 5
    assert cfg.enabled is False


def test_parse_briefing_partial_uses_defaults_for_rest():
    cfg = _parse_briefing({"max_per_day": 3})
    assert cfg.model == "claude-haiku-4-5"
    assert cfg.max_per_day == 3
    assert cfg.enabled is True


def test_load_config_without_briefing_section_gets_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    config = load_config(str(path))
    assert config.briefing == BriefingConfig()


def test_load_config_with_briefing_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        MINIMAL_CONFIG + "\nbriefing:\n  model: claude-sonnet-4-6\n  max_per_day: 10\n  enabled: false\n",
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.briefing.model == "claude-sonnet-4-6"
    assert config.briefing.max_per_day == 10
    assert config.briefing.enabled is False
