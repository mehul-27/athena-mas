from fastapi.testclient import TestClient

from athena.api.app import create_app
from athena.chat.store import ChatStore


def test_project_keeps_context_and_items_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DB", str(tmp_path / "chat.db"))
    client = TestClient(create_app())
    project = client.post("/api/workspaces", json={"name": "Seminar", "notes": "Use the approved outline."}).json()["workspace"]
    other = client.post("/api/workspaces", json={"name": "Unrelated"}).json()["workspace"]
    project_id = project["id"]
    store = ChatStore()
    first = store.create("Outline", workspace_id=project_id)
    second = client.post("/api/chat/conversations", json={"workspace_id": project_id}).json()["conversation"]
    outsider = store.create("Unrelated", workspace_id=other["id"])
    store.add_message(first["id"], "user", "The seminar uses the three-part outline.")
    store.add_message(outsider["id"], "user", "Unrelated private material.")

    scoped = client.get("/api/chat/conversations", params={"workspace_id": project_id}).json()["conversations"]
    assert {item["id"] for item in scoped} == {first["id"], second["id"]}
    assert second["workspace_id"] == project_id
    context = store.workspace_context(second["id"])
    assert "Use the approved outline." in context and "three-part outline" in context
    assert "Unrelated private material." not in context

    path = f"/api/workspaces/{project_id}/items"
    assert client.post(path, json={"kind": "report", "ref": "run-one", "label": "Outline report"}).status_code == 200
    assert "Outline report" in store.workspace_context(second["id"])
    assert client.delete(f"/api/workspaces/{project_id}").status_code == 200
    assert store.workspace_context(second["id"]) == ""
    assert client.get(f"/api/chat/conversations/{first['id']}").status_code == 200


def test_workflows_and_editable_memory_survive_new_app_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("ATHENA_DATA_DIR", str(tmp_path))
    first = TestClient(create_app())
    workflow = first.post("/api/workflows", json={"name": "Weekly review", "prompt": "Review {{input}}."})
    assert workflow.status_code == 201
    memory = first.post("/api/memory", json={"category": "preference", "content": "Start with a short summary."})
    assert memory.status_code == 201

    restarted = TestClient(create_app())
    assert restarted.get("/api/workflows").json()["workflows"][0]["name"] == "Weekly review"
    entry = restarted.get("/api/memory").json()["items"][0]
    changed = restarted.patch(f"/api/memory/{entry['id']}", json={"content": "Start with the answer."})
    assert changed.json()["item"]["content"] == "Start with the answer."
    assert restarted.put("/api/memory/settings", json={"enabled": False}).json() == {"enabled": False}
