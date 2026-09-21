"""Chat history persisted in SQLite: conversations, follow-ups, isolation and management."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import FakeLLM, parse_sse, upload_text


def ask(client: TestClient, message: str, conversation_id: str | None = None) -> dict:
    body = {"message": message, "web_search": False, "conversation_id": conversation_id}
    return dict(parse_sse(client.post("/api/chat", json=body).text))


def test_first_message_creates_a_titled_conversation(client: TestClient) -> None:
    upload_text(client)
    events = ask(client, "When   do I get my\ndeposit back?")
    conversation = events["conversation"]
    assert conversation["title"] == "When do I get my deposit back?"
    assert "message_id" in events["done"]

    listed = client.get("/api/conversations").json()
    assert [c["id"] for c in listed] == [conversation["id"]]
    assert listed[0]["message_count"] == 2


def test_saved_messages_keep_sources_and_grounding(client: TestClient) -> None:
    upload_text(client)
    conversation_id = ask(client, "When do I get my deposit back?")["conversation"]["id"]
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    user, assistant = detail["messages"]
    assert (user["role"], assistant["role"]) == ("user", "assistant")
    assert assistant["meta"]["grounding"]["verdict"] == "grounded"
    assert assistant["meta"]["sources"][0]["id"] == "S1"  # citations still open after a reload


def test_follow_up_uses_saved_history(client: TestClient, fake_llm: FakeLLM) -> None:
    conversation_id = ask(client, "What is the notice period?")["conversation"]["id"]
    ask(client, "And the deposit?", conversation_id)
    last_call = [detail for kind, detail in fake_llm.calls if kind == "chat"][-1]
    turns = [(m.role, m.text) for m in last_call["messages"]]
    assert turns[0] == ("user", "What is the notice period?")
    assert turns[1][0] == "model"
    assert "And the deposit?" in turns[2][1]
    assert client.get("/api/conversations").json()[0]["message_count"] == 4


def test_history_survives_a_server_restart(settings: Settings, fake_llm: FakeLLM) -> None:
    first = TestClient(create_app(settings, llm=fake_llm))
    conversation_id = ask(first, "Is the late fee legal?")["conversation"]["id"]
    token = first.cookies.get("lexiguide_sid")

    restarted = TestClient(create_app(settings, llm=fake_llm))  # same SQLite file, fresh process state
    restarted.cookies.set("lexiguide_sid", token, path="/api")
    assert [c["id"] for c in restarted.get("/api/conversations").json()] == [conversation_id]


def test_rename_and_delete(client: TestClient) -> None:
    conversation_id = ask(client, "Question")["conversation"]["id"]
    assert (
        client.patch(f"/api/conversations/{conversation_id}", json={"title": "  Deposit   dispute "}).status_code == 204
    )
    assert client.get("/api/conversations").json()[0]["title"] == "Deposit dispute"
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204
    assert client.get("/api/conversations").json() == []
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404


def test_clear_all_conversations(client: TestClient) -> None:
    ask(client, "One")
    ask(client, "Two")
    assert client.delete("/api/conversations").status_code == 204
    assert client.get("/api/conversations").json() == []


def test_conversations_are_private_to_their_session(settings: Settings, fake_llm: FakeLLM) -> None:
    app = create_app(settings, llm=fake_llm)
    alice, mallory = TestClient(app), TestClient(app)
    conversation_id = ask(alice, "Private question")["conversation"]["id"]
    assert mallory.get("/api/conversations").json() == []
    assert mallory.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert mallory.delete(f"/api/conversations/{conversation_id}").status_code == 404
    events = ask(mallory, "Let me in", conversation_id)
    assert "chat no longer exists" in events["error"]["message"]
    assert len(alice.get(f"/api/conversations/{conversation_id}").json()["messages"]) == 2
