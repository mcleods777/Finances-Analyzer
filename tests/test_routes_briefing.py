from __future__ import annotations

import os

import pytest
from flask import Flask

from finance.blueprints import dashboard as dashboard_module

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_BRIEFING = {
    "prose": "Dining is up 40% this period.",
    "patterns": [
        {
            "pattern_type": "category_delta",
            "headline": "Dining spending is up 40%.",
            "magnitude": 123.4,
            "drill_down_filter": {
                "category": ["Dining"], "account": None,
                "start_date": "2026-06-08", "end_date": "2026-06-21", "search": None,
            },
            "fingerprint": "category_delta:Dining",
        }
    ],
    "generated_at": "2026-07-02T08:00:00",
    "source": "template",
    "cache_hit": False,
}


@pytest.fixture
def client(monkeypatch):
    flask_app = Flask(
        __name__,
        template_folder=os.path.join(REPO_ROOT, "templates"),
        static_folder=os.path.join(REPO_ROOT, "static"),
    )
    flask_app.register_blueprint(dashboard_module.dashboard_bp)
    return flask_app.test_client()


def test_briefing_200(client, monkeypatch):
    calls = []

    def fake_generate(force=False):
        calls.append(force)
        return dict(FAKE_BRIEFING)

    monkeypatch.setattr(dashboard_module, "get_cache", lambda: {"df": object()})
    monkeypatch.setattr(
        dashboard_module.briefing_writer, "generate_briefing", fake_generate
    )

    resp = client.get("/api/briefing")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["prose"] == FAKE_BRIEFING["prose"]
    assert body["patterns"] == FAKE_BRIEFING["patterns"]
    assert body["source"] == "template"
    assert calls == [False]


def test_briefing_force_param(client, monkeypatch):
    calls = []

    def fake_generate(force=False):
        calls.append(force)
        return dict(FAKE_BRIEFING)

    monkeypatch.setattr(dashboard_module, "get_cache", lambda: {"df": object()})
    monkeypatch.setattr(
        dashboard_module.briefing_writer, "generate_briefing", fake_generate
    )

    assert client.get("/api/briefing?force=1").status_code == 200
    assert client.get("/api/briefing?force=0").status_code == 200
    assert calls == [True, False]


def test_briefing_503_when_no_data_loaded(client, monkeypatch):
    monkeypatch.setattr(dashboard_module, "get_cache", lambda: {})

    def boom(force=False):  # pragma: no cover - must not be reached
        raise AssertionError("generate_briefing must not be called without data")

    monkeypatch.setattr(dashboard_module.briefing_writer, "generate_briefing", boom)

    resp = client.get("/api/briefing")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"] == "no_data_loaded"
    assert "config.yaml" in body["message"]


def test_briefing_503_on_exception_never_500(client, monkeypatch):
    monkeypatch.setattr(dashboard_module, "get_cache", lambda: {"df": object()})

    def boom(force=False):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(dashboard_module.briefing_writer, "generate_briefing", boom)

    resp = client.get("/api/briefing")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "briefing_failed"
