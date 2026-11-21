from __future__ import annotations

import json

import pytest

from athena.chat import personal_memory as memory


def test_memory_crud_is_bounded_and_persistent(tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    monkeypatch.setenv("ATHENA_PERSONAL_MEMORY_FILE", str(path))
    item = memory.add_item("preference", "Keep explanations concise.")
    updated = memory.update_item(item["id"], content="Use concise explanations.")
    assert updated["content"] == "Use concise explanations."
    assert memory.list_items()[0]["id"] == item["id"]
    assert "Use concise explanations." in memory.context_block()
    assert memory.delete_item(item["id"])
    assert memory.list_items() == []


def test_memory_rejects_secrets_and_caps_items(tmp_path):
    path = tmp_path / "memory.json"
    with pytest.raises(ValueError, match="credentials"):
        memory.add_item("preference", "My API key is sk-proj-12345678901234567890", path=path)
    with pytest.raises(ValueError, match="credentials"):
        memory.add_item("about_me", "My password is hunter2", path=path)
    for index in range(memory.MAX_ITEMS):
        memory.add_item("preference", f"Preference number {index}", path=path)
    with pytest.raises(ValueError, match="at most"):
        memory.add_item("preference", "One more preference", path=path)
    assert len(json.loads(path.read_text(encoding="utf-8"))["items"]) == memory.MAX_ITEMS


def test_learning_is_explicit_conservative_and_deduplicated(tmp_path):
    path = tmp_path / "memory.json"
    assert memory.learn_from_message("The new laptop is blue", path) is None
    first = memory.learn_from_message("I prefer concise answers.", path)
    second = memory.learn_from_message("I prefer concise answers.", path)
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert first["source"] == "learned"
    assert memory.learn_from_message("Remember that I use sk-live-12345678901234567890", path) is None


def test_learning_toggle_only_controls_automatic_capture(tmp_path):
    path = tmp_path / "memory.json"
    memory.set_enabled(False, path)
    assert memory.learn_from_message("I prefer concise answers.", path) is None
    assert memory.settings(path) == {"enabled": False}
    memory.add_item("preference", "Keep answers concise.", path=path)
    assert len(memory.list_items(path)) == 1


def test_learning_updates_the_users_name(tmp_path):
    path = tmp_path / "memory.json"
    first = memory.learn_from_message("My name is Kiran.", path)
    later = memory.learn_from_message("Call me Kir.", path)
    assert first["id"] == later["id"]
    assert [row["content"] for row in memory.list_items(path)] == ["My name is Kir"]


def test_old_chat_recall_is_bounded_to_same_conversation(tmp_path):
    from types import SimpleNamespace

    from athena.chat.service import _earlier_chat_recall
    from athena.chat.store import ChatStore

    store = ChatStore(tmp_path / "chat.db")
    first = store.create()
    second = store.create()
    store.add_message(first["id"], "user", "I am building a small garden greenhouse.")
    store.add_message(first["id"], "assistant", "The greenhouse needs ventilation and shade.")
    store.add_message(second["id"], "user", "My greenhouse budget is confidential.")
    ctx = SimpleNamespace(store=store, conversation_id=first["id"], history=[], user_message={},
                          model_text="How should the greenhouse be ventilated?")
    result = _earlier_chat_recall(ctx)
    assert "ventilation" in result
    assert "confidential" not in result
    assert len(result) <= 2400
