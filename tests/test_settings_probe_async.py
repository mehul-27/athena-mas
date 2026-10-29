"""The Test endpoint must never block the API, and must always answer JSON."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import pytest

from athena.api import probe
from athena.api.app import create_app
from athena.settings import profiles as store
from athena.settings.vault import MemoryStore, default_vault, set_vault

SENTINEL = "sk-PROBE-SENTINEL-do-not-print"
SLEEP_2S = [sys.executable, "-c", "import time; time.sleep(2)"]


def _can_spawn() -> bool:
    """The test sandbox blocks child processes with pipes (EPERM)."""
    try:
        return sys.executable is not None and asyncio.run(_spawn_probe()) == 0
    except Exception:  # noqa: BLE001 - any failure means "cannot spawn"
        return False


async def _spawn_probe() -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await process.communicate()
    return process.returncode


CAN_SPAWN = _can_spawn()
needs_spawn = pytest.mark.skipif(
    not CAN_SPAWN, reason="the sandbox blocks child processes with pipes")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(store, "ACTIVE_FILE", tmp_path / "active.json")
    monkeypatch.setattr(store, "PRESETS_FILE", tmp_path / "presets.json")
    (tmp_path / "presets.json").write_text("[]", encoding="utf-8")
    set_vault(MemoryStore())
    yield
    set_vault(None)


@pytest.fixture()
def profile() -> dict:
    saved = store.upsert(store.Profile(
        name="Gateway", api_base="https://gateway.example/v1",
        model="openai/fake-model",
    ))
    default_vault().set(store.key_name(saved.id, "llm"), SENTINEL)
    return saved.public()


@pytest.fixture()
def client(monkeypatch) -> httpx.AsyncClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    app = create_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


def test_health_answers_while_a_probe_is_running(monkeypatch, profile) -> None:
    """/api/health must stay fast: the request handler must not block the loop."""
    async def slow(_env, timeout=120):
        await asyncio.sleep(2)
        return [{"path": "adk", "ok": True}]

    monkeypatch.setattr(probe, "run_preflight", slow)

    async def scenario() -> tuple[float, int]:
        import athena.api.app as app_module

        monkeypatch.setattr(app_module, "_crew_server_up", lambda *a, **k: False)
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            task = asyncio.create_task(
                client.post(f"/api/settings/profiles/{profile['id']}/test"))
            await asyncio.sleep(0.2)  # the probe is in flight
            started = time.perf_counter()
            health = await client.get("/api/health")
            elapsed = time.perf_counter() - started
            assert health.status_code == 200
            assert (await task).status_code == 200
            return elapsed, health.status_code

    elapsed, status = asyncio.run(scenario())

    assert status == 200
    assert elapsed < 0.5, f"health took {elapsed:.2f}s while a probe was running"

def test_a_second_test_for_the_same_profile_is_409(monkeypatch, profile) -> None:
    async def slow(_env, timeout=120):
        await asyncio.sleep(1)
        return [{"path": "adk", "ok": True}]

    monkeypatch.setattr(probe, "run_preflight", slow)

    async def scenario() -> tuple[int, int, str]:
        import athena.api.app as app_module

        monkeypatch.setattr(app_module, "_crew_server_up", lambda *a, **k: False)
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            first = asyncio.create_task(
                client.post(f"/api/settings/profiles/{profile['id']}/test"))
            await asyncio.sleep(0.2)
            second = await client.post(f"/api/settings/profiles/{profile['id']}/test")
            await first
            return first.result().status_code, second.status_code, second.text

    first_status, second_status, body = asyncio.run(scenario())

    assert first_status == 200
    assert second_status == 409
    assert "already running" in body

@needs_spawn
def test_a_timeout_returns_json_and_kills_the_child(monkeypatch) -> None:
    monkeypatch.setattr(probe, "PREFLIGHT_ARGV", SLEEP_2S)

    results = asyncio.run(probe.run_preflight({}, timeout=1))

    assert results[0]["ok"] is False
    assert results[0]["error_type"] == "TimeoutExpired"
    assert "did not finish" in results[0]["error"]


def test_an_exception_in_the_handler_still_returns_json(monkeypatch, profile) -> None:
    async def boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(probe, "run_preflight", boom)

    async def scenario() -> tuple[int, dict]:
        import athena.api.app as app_module

        monkeypatch.setattr(app_module, "_crew_server_up", lambda *a, **k: False)
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            response = await client.post(
                f"/api/settings/profiles/{profile['id']}/test")
            return response.status_code, json.loads(response.text)

    status, body = asyncio.run(scenario())

    assert status == 500
    assert "detail" in body
    assert SENTINEL not in json.dumps(body)


@needs_spawn
def test_the_probe_returns_parsed_json(monkeypatch) -> None:
    argv = [sys.executable, "-c",
            "print('[{\"path\": \"adk\", \"ok\": true}]')"]
    monkeypatch.setattr(probe, "PREFLIGHT_ARGV", argv)

    results = asyncio.run(probe.run_preflight({}))

    assert results == [{"path": "adk", "ok": True}]


@needs_spawn
def test_bad_json_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setattr(probe, "PREFLIGHT_ARGV",
                        [sys.executable, "-c", "print('not json')"])

    results = asyncio.run(probe.run_preflight({}))

    assert results[0]["error_type"] == "InvalidJson"


def test_preflight_accepts_a_leading_probe_diagnostic(monkeypatch) -> None:
    """A human-readable log line before the JSON body must not corrupt parsing."""
    payload = (
        b"usage scope analyst: calls=1 tokens=1/1 reasoning=0\n"
        b'[{"path": "adk-lite-llm", "ok": true}, '
        b'{"path": "crewai-server-style", "ok": true}]\n'
    )

    class FakeProcess:
        pid = 1
        returncode = 0

        async def communicate(self):
            return payload, b""

    async def fake_exec(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", fake_exec)

    results = asyncio.run(probe.run_preflight({}))

    assert [row["path"] for row in results] == [
        "adk-lite-llm", "crewai-server-style",
    ]
    assert all(row["ok"] for row in results)


def test_preflight_still_rejects_truncated_json(monkeypatch) -> None:
    payload = b"usage scope analyst: calls=1 tokens=1/1 reasoning=0\n[{\"path\": \"adk\""

    class FakeProcess:
        pid = 1
        returncode = 0

        async def communicate(self):
            return payload, b""

    async def fake_exec(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", fake_exec)

    results = asyncio.run(probe.run_preflight({}))

    assert results[0]["path"] == "preflight"
    assert results[0]["error_type"] == "InvalidJson"


def test_preflight_json_mode_keeps_stdout_machine_readable(monkeypatch, capsys) -> None:
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    import preflight
    from preflight_probes import ProbeResult

    def fake_collect(_built):
        print("usage scope analyst: calls=1 tokens=1/1 reasoning=0")
        return (
            [
                ProbeResult(path="adk-lite-llm", ok=True, host="api.commandcode.ai"),
                ProbeResult(path="crewai-server-style", ok=True, host="api.commandcode.ai"),
            ],
            ["openai/deepseek/deepseek-v4.1-flash"] * 2,
        )

    monkeypatch.setattr(preflight.llm, "configure_process", lambda: None)
    monkeypatch.setattr(preflight, "describe_paths", lambda: [])
    monkeypatch.setattr(preflight, "collect_live", fake_collect)

    code = preflight.main(["--json", "--live"])
    captured = capsys.readouterr()

    assert code == 0
    rows = json.loads(captured.out)
    assert [row["path"] for row in rows] == ["adk-lite-llm", "crewai-server-style"]
    assert "usage scope analyst" in captured.err
