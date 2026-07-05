from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

import pytest
from flask import Flask

from finance import advisor, db
from finance.blueprints import desk as desk_module
from finance.config_loader import (
    AdvisorConfig,
    AppConfig,
    ClassificationConfig,
    PayPeriodConfig,
)

FAKE_RESULT = {
    "reply": "Net worth is $4,250 and free cash is $500.",
    "tool_activity": [{"tool": "get_overview", "summary": ""}],
    "insights_saved": [],
    "profile_changes": [],
    "usage": {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "est_cost": 0.006,
    },
}


def make_cfg(**advisor_kwargs) -> AppConfig:
    return AppConfig(
        pay_period=PayPeriodConfig(start_date=date(2026, 1, 5), frequency_days=14),
        accounts=[],
        classification=ClassificationConfig(),
        advisor=AdvisorConfig(**advisor_kwargs),
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Desk blueprint wired to a tmp DB and a fake cache/advisor."""
    db_path = str(tmp_path / "finance.db")
    seed = db.get_connection(db_path)
    db.init_db(seed)
    seed.close()

    state = SimpleNamespace(
        db_path=db_path,
        cache={"df": object(), "config": make_cfg()},
        answer_calls=[],
        answer_result=dict(FAKE_RESULT),
        answer_exc=None,
    )

    def fake_get_db_connection():
        conn = db.get_connection(db_path)
        db.init_db(conn)
        return conn

    def fake_answer(conn, conversation_id, message, model, intelligence, today=None):
        state.answer_calls.append(
            {"conversation_id": conversation_id, "message": message,
             "model": model, "intelligence": intelligence}
        )
        if state.answer_exc is not None:
            raise state.answer_exc
        return dict(state.answer_result)

    monkeypatch.setattr(desk_module, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(desk_module, "get_cache", lambda: state.cache)
    monkeypatch.setattr(desk_module.advisor, "answer", fake_answer)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    flask_app = Flask(__name__)
    flask_app.register_blueprint(desk_module.desk_bp)
    state.client = flask_app.test_client()

    def conn_factory():
        return fake_get_db_connection()

    state.conn_factory = conn_factory
    return state


# --- Page routes ---


@pytest.fixture
def page_client():
    """Client with real template/static folders so /desk and /archive render."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    flask_app = Flask(
        __name__,
        template_folder=os.path.join(root, "templates"),
        static_folder=os.path.join(root, "static"),
    )
    flask_app.register_blueprint(desk_module.desk_bp)

    @flask_app.context_processor
    def inject_dateline():
        return {"dateline": "Saturday · July 4 · 2026"}

    return flask_app.test_client()


def test_desk_page_renders(page_client):
    resp = page_client.get("/desk")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Desk — The Private Wire" in html
    assert "desk.js" in html
    assert "conv-list" in html  # conversation rail
    assert "composer-input" in html  # composer


def test_archive_page_renders(page_client):
    resp = page_client.get("/archive")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Archive — The Private Wire" in html
    assert "archive.js" in html
    assert "The Dossier" in html
    assert "The Insight Log" in html


# --- POST /api/chat ---


def test_chat_creates_conversation_and_returns_shape(harness):
    message = "How much did I spend on groceries last month? " + "x" * 60
    resp = harness.client.post("/api/chat", json={"message": message})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {
        "conversation_id", "reply", "tool_activity", "insights_saved",
        "profile_changes", "usage",
    }
    assert body["reply"] == FAKE_RESULT["reply"]
    assert body["usage"]["est_cost"] == 0.006

    conn = harness.conn_factory()
    conversation = db.get_conversation(conn, body["conversation_id"])
    conn.close()
    assert conversation["title"] == message[:60]  # first-question snippet
    assert conversation["model"] == "claude-sonnet-5"  # config default
    assert conversation["intelligence"] == "standard"
    assert harness.answer_calls == [
        {
            "conversation_id": body["conversation_id"], "message": message,
            "model": "claude-sonnet-5", "intelligence": "standard",
        }
    ]


def test_chat_reuses_conversation_and_persists_picker(harness):
    first = harness.client.post(
        "/api/chat",
        json={"message": "hi", "model": "claude-haiku-4-5", "intelligence": "deep"},
    ).get_json()
    conversation_id = first["conversation_id"]

    # Omitting model/intelligence falls back to the conversation's stored pick.
    harness.client.post(
        "/api/chat", json={"conversation_id": conversation_id, "message": "more"}
    )
    assert harness.answer_calls[-1]["model"] == "claude-haiku-4-5"
    assert harness.answer_calls[-1]["intelligence"] == "deep"

    # Switching persists the new pick on the conversation.
    harness.client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "switch",
              "model": "claude-opus-4-8", "intelligence": "standard"},
    )
    conn = harness.conn_factory()
    conversation = db.get_conversation(conn, conversation_id)
    conn.close()
    assert conversation["model"] == "claude-opus-4-8"
    assert conversation["intelligence"] == "standard"


def test_chat_400_on_missing_message(harness):
    assert harness.client.post("/api/chat", json={}).status_code == 400
    assert harness.client.post("/api/chat", json={"message": "  "}).status_code == 400


def test_chat_400_on_bad_model_and_intelligence(harness):
    resp = harness.client.post(
        "/api/chat", json={"message": "hi", "model": "claude-9-mega"}
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad_model"

    resp = harness.client.post(
        "/api/chat", json={"message": "hi", "intelligence": "galaxy"}
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad_intelligence"
    assert harness.answer_calls == []


def test_chat_404_on_unknown_conversation(harness):
    resp = harness.client.post(
        "/api/chat", json={"conversation_id": 9999, "message": "hi"}
    )
    assert resp.status_code == 404


def test_chat_429_on_daily_cap(harness):
    harness.answer_exc = advisor.AdvisorError("daily_cap", "cap reached")
    resp = harness.client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 429
    assert resp.get_json()["error"] == "daily_cap"


def test_chat_503_on_advisor_api_errors(harness):
    for code in ("rate_limited", "api_error", "network_error"):
        harness.answer_exc = advisor.AdvisorError(code, "boom")
        resp = harness.client.post("/api/chat", json={"message": "hi"})
        assert resp.status_code == 503
        assert resp.get_json()["error"] == code


def test_chat_503_never_500_on_unexpected_exception(harness):
    harness.answer_exc = RuntimeError("kaboom")
    resp = harness.client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "chat_failed"


def test_chat_503_without_api_key(harness, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = harness.client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "advisor_not_configured"
    assert "ANTHROPIC_API_KEY" in resp.get_json()["message"]


def test_chat_503_when_disabled(harness):
    harness.cache["config"] = make_cfg(enabled=False)
    resp = harness.client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "advisor_not_configured"


def test_chat_503_when_no_data_loaded(harness):
    harness.cache.clear()
    resp = harness.client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "no_data_loaded"
    assert harness.answer_calls == []


# --- Conversations CRUD ---


def _seed_conversation(harness, title="What about rent?") -> int:
    conn = harness.conn_factory()
    conversation_id = db.create_conversation(conn, title, "claude-sonnet-5", "standard")
    db.insert_chat_message(
        conn, conversation_id, "user",
        [{"type": "text", "text": "What about rent?"}], display_text="What about rent?",
    )
    db.insert_chat_message(
        conn, conversation_id, "assistant",
        [{"type": "tool_use", "id": "tu_1", "name": "get_bills", "input": {}}],
        display_text="",
    )
    db.insert_chat_message(
        conn, conversation_id, "user",
        [{"type": "tool_result", "tool_use_id": "tu_1", "content": "{}"}],
        display_text="",
    )
    db.insert_chat_message(
        conn, conversation_id, "assistant",
        [{"type": "text", "text": "Rent is $800, due the 1st."}],
        display_text="Rent is $800, due the 1st.",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    conn.close()
    return conversation_id


def test_list_conversations(harness):
    a = _seed_conversation(harness, title="first")
    b = _seed_conversation(harness, title="second")
    body = harness.client.get("/api/conversations").get_json()
    rows = body["conversations"]
    assert {r["id"] for r in rows} == {a, b}
    assert set(rows[0]) == {
        "id", "title", "model", "intelligence", "created_at", "updated_at", "archived",
    }


def test_get_conversation_returns_renderable_messages(harness):
    conversation_id = _seed_conversation(harness)
    resp = harness.client.get(f"/api/conversations/{conversation_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["title"] == "What about rent?"
    # tool_use / tool_result turns (empty display_text) are not rendered
    assert [m["display_text"] for m in body["messages"]] == [
        "What about rent?", "Rent is $800, due the 1st.",
    ]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_get_conversation_404(harness):
    assert harness.client.get("/api/conversations/9999").status_code == 404


def test_rename_conversation(harness):
    conversation_id = _seed_conversation(harness)
    resp = harness.client.patch(
        f"/api/conversations/{conversation_id}", json={"title": "Rent deep-dive"}
    )
    assert resp.status_code == 200
    conn = harness.conn_factory()
    assert db.get_conversation(conn, conversation_id)["title"] == "Rent deep-dive"
    conn.close()

    assert harness.client.patch(
        f"/api/conversations/{conversation_id}", json={"title": "  "}
    ).status_code == 400
    assert harness.client.patch(
        "/api/conversations/9999", json={"title": "x"}
    ).status_code == 404


def test_delete_conversation_cascades_messages(harness):
    conversation_id = _seed_conversation(harness)
    assert harness.client.delete(f"/api/conversations/{conversation_id}").status_code == 200
    conn = harness.conn_factory()
    assert db.get_conversation(conn, conversation_id) is None
    assert db.list_chat_messages(conn, conversation_id) == []
    conn.close()
    assert harness.client.delete("/api/conversations/9999").status_code == 404


# --- Insights (The Archive) ---


def _seed_insights(harness):
    conn = harness.conn_factory()
    ids = [
        db.insert_insight(conn, source="briefing", text="Dining is up 40%.",
                          fingerprints=["category_delta:Dining"],
                          created_at="2026-06-01 08:00:00"),
        db.insert_insight(conn, source="chat", text="Groceries doubled since May.",
                          model="claude-sonnet-5", created_at="2026-06-02 09:00:00"),
        db.insert_insight(conn, source="chat", text="Rent is 38% of income.",
                          model="claude-sonnet-5", created_at="2026-06-03 10:00:00"),
    ]
    conn.close()
    return ids


def test_list_insights_newest_first_with_source_and_search(harness):
    _seed_insights(harness)
    body = harness.client.get("/api/insights").get_json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert [i["text"] for i in body["insights"]] == [
        "Rent is 38% of income.", "Groceries doubled since May.", "Dining is up 40%.",
    ]
    assert set(body["insights"][0]) == {
        "id", "created_at", "source", "text", "model", "conversation_id",
    }

    briefing_only = harness.client.get("/api/insights?source=briefing").get_json()
    assert [i["source"] for i in briefing_only["insights"]] == ["briefing"]

    search = harness.client.get("/api/insights?q=groceries").get_json()
    assert search["total"] == 1
    assert "Groceries" in search["insights"][0]["text"]

    assert harness.client.get("/api/insights?source=gossip").status_code == 400


def test_insights_pagination(harness):
    conn = harness.conn_factory()
    for i in range(55):
        db.insert_insight(conn, source="chat", text=f"insight {i}",
                          created_at=f"2026-05-01 00:{i:02d}:00")
    conn.close()
    page1 = harness.client.get("/api/insights").get_json()
    assert page1["total"] == 55
    assert len(page1["insights"]) == 50
    page2 = harness.client.get("/api/insights?page=2").get_json()
    assert len(page2["insights"]) == 5


def test_delete_insight(harness):
    ids = _seed_insights(harness)
    assert harness.client.delete(f"/api/insights/{ids[0]}").status_code == 200
    assert harness.client.get("/api/insights").get_json()["total"] == 2
    assert harness.client.delete(f"/api/insights/{ids[0]}").status_code == 404


# --- Profile (the Dossier) ---


def test_profile_crud_with_ai_badge_data(harness):
    # Empty dossier: all four sections present.
    body = harness.client.get("/api/profile").get_json()
    assert body == {"goal": [], "weakness": [], "debt": [], "note": []}

    resp = harness.client.post(
        "/api/profile", json={"section": "goal", "text": "Save $10k by December"}
    )
    assert resp.status_code == 201
    entry = resp.get_json()
    assert entry["source"] == "user"

    # AI-added entry (via the advisor tool path) carries the badge data.
    conn = harness.conn_factory()
    ai_id = db.insert_profile_entry(conn, "weakness", "Impulse dining", source="ai")
    conn.close()

    body = harness.client.get("/api/profile").get_json()
    assert body["goal"][0]["text"] == "Save $10k by December"
    assert body["weakness"][0]["source"] == "ai"
    assert body["weakness"][0]["id"] == ai_id

    # PATCH text
    resp = harness.client.patch(
        f"/api/profile/{entry['id']}", json={"text": "Save $12k by December"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["text"] == "Save $12k by December"

    # DELETE = soft-deactivate: gone from GET, still in the table.
    assert harness.client.delete(f"/api/profile/{ai_id}").status_code == 200
    body = harness.client.get("/api/profile").get_json()
    assert body["weakness"] == []
    conn = harness.conn_factory()
    row = db.get_profile_entry(conn, ai_id)
    conn.close()
    assert row is not None and row["active"] == 0


def test_profile_validation_errors(harness):
    assert harness.client.post(
        "/api/profile", json={"section": "hobby", "text": "x"}
    ).status_code == 400
    assert harness.client.post(
        "/api/profile", json={"section": "goal", "text": "  "}
    ).status_code == 400
    assert harness.client.patch("/api/profile/1", json={}).status_code == 400
    assert harness.client.patch("/api/profile/1", json={"text": " "}).status_code == 400
    assert harness.client.patch(
        "/api/profile/1", json={"active": "nope"}
    ).status_code == 400
    assert harness.client.patch("/api/profile/9999", json={"text": "x"}).status_code == 404
    assert harness.client.delete("/api/profile/9999").status_code == 404


def test_profile_reactivate_via_patch(harness):
    conn = harness.conn_factory()
    entry_id = db.insert_profile_entry(conn, "note", "Old note", source="user")
    db.update_profile_entry(conn, entry_id, active=False)
    conn.close()
    resp = harness.client.patch(f"/api/profile/{entry_id}", json={"active": True})
    assert resp.status_code == 200
    assert resp.get_json()["active"] is True
    body = harness.client.get("/api/profile").get_json()
    assert body["note"][0]["id"] == entry_id
