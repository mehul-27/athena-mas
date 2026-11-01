"""Chat persistence: conversations, messages, history and totals."""

from __future__ import annotations

from athena.chat.store import ChatStore, derive_title


def _store(tmp_path) -> ChatStore:
    return ChatStore(tmp_path / "chat.db")


def test_conversation_lifecycle_and_rename(tmp_path) -> None:
    store = _store(tmp_path)
    conversation = store.create()

    assert conversation["title"] == "New chat"
    assert store.list() == [store.summary(conversation)]

    assert store.rename(conversation["id"], "  A better title  ") is True
    assert store.get(conversation["id"])["title"] == "A better title"
    assert store.rename(conversation["id"], "   ") is False

    deleted = store.delete(conversation["id"])
    assert deleted == {"deleted": True, "removed_messages": 0}
    assert store.get(conversation["id"]) is None


def test_messages_round_trip_and_totals(tmp_path) -> None:
    store = _store(tmp_path)
    conversation = store.create()

    user = store.add_message(conversation["id"], "user", "Hello")
    assistant = store.add_message(
        conversation["id"],
        "assistant",
        "Hi",
        provider="groq",
        model="groq/x",
        capabilities=["rag"],
        tools=["web_search"],
        sources=[{"kind": "web", "index": 1, "url": "https://example.test"}],
        tool_events=[{"name": "web_search", "status": "ok"}],
        usage={"tokens_in": 10, "tokens_out": 4},
        meta={"tool_note": ""},
    )

    messages = store.messages(conversation["id"])
    assert [message["id"] for message in messages] == [user["id"], assistant["id"]]
    assert messages[1]["usage"] == {"tokens_in": 10, "tokens_out": 4}
    assert messages[1]["sources"][0]["url"] == "https://example.test"
    assert messages[1]["tools"] == ["web_search"]

    summary = store.summary(conversation)
    assert summary["message_count"] == 2
    assert summary["total_tokens"] == 14

    updated = store.update_message(assistant["id"], content="Updated", status="ok")
    assert updated["content"] == "Updated"


def test_delete_removes_messages(tmp_path) -> None:
    store = _store(tmp_path)
    conversation = store.create()
    store.add_message(conversation["id"], "user", "Hello")

    result = store.delete(conversation["id"])

    assert result["removed_messages"] == 1
    assert store.get(conversation["id"]) is None


def test_history_is_bounded_and_ignores_streaming_placeholders(tmp_path) -> None:
    store = _store(tmp_path)
    conversation = store.create()
    store.add_message(conversation["id"], "user", "one")
    store.add_message(conversation["id"], "assistant", "two")
    store.add_message(conversation["id"], "assistant", "", status="streaming")
    store.add_message(conversation["id"], "user", "three")

    history = store.history(conversation["id"], limit=2, max_chars=1000)

    assert history == [
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    assert store.history(conversation["id"], max_chars=5) == [
        {"role": "user", "content": "three"}
    ]


def test_title_is_bounded() -> None:
    assert derive_title("") == "New chat"
    assert derive_title("one two three four five six seven") == "one two three four five six…"
    assert len(derive_title("x" * 100)) <= 60
