"""Probes, lifespan hooks and the run-termination helper.

Offline: the crew-server card is stubbed everywhere, and the data directory is
a temporary one, so nothing here touches the real databases.
"""

from __future__ import annotations

import inspect
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from athena.api import health_routes, runs as runs_module
from athena.api.app import create_app


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "data"
    monkeypatch.setenv("ATHENA_DATA_DIR", str(target))
    monkeypatch.setenv("EVENTS_DB", str(target / "events.db"))
    monkeypatch.setattr(health_routes, "_probe_card", lambda *a, **k: (True, ""))
    health_routes.reset_card_cache()
    return target


@pytest.fixture()
def client(monkeypatch, data_dir) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    return TestClient(create_app())


# --- /healthz ---------------------------------------------------------------


def test_healthz_is_cheap_and_does_no_io(client: TestClient, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("/healthz must not probe anything")

    monkeypatch.setattr(health_routes, "_probe_card", explode)
    monkeypatch.setattr(health_routes, "volume_writable", explode)
    monkeypatch.setattr(health_routes, "database_openable", explode)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_never_gated_and_never_redirects(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "token")
    assert client.get("/healthz", headers={"accept": "text/html"}).status_code == 200


# --- /readyz ----------------------------------------------------------------


def test_readyz_reports_each_dependency(client: TestClient, data_dir) -> None:
    response = client.get("/readyz")
    body = response.json()

    assert response.status_code == 200, body
    assert body["status"] == "ready"
    assert body["checks"]["volume"]["ok"] is True
    assert body["checks"]["volume"]["path"] == str(data_dir)
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["crew_server"]["ok"] is True
    assert body["degraded"] is False
    assert body["version"] and body["commit"]


def test_readyz_carries_the_build_identity(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("ATHENA_VERSION", "1.2.3")
    monkeypatch.setenv("ATHENA_GIT_SHA", "abc1234")

    body = client.get("/readyz").json()

    assert body["version"] == "1.2.3"
    assert body["commit"] == "abc1234"


def test_readyz_is_503_when_the_volume_is_unusable(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(
        health_routes, "volume_writable", lambda *a, **k: (False, "PermissionError")
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not-ready"
    assert response.json()["checks"]["volume"]["ok"] is False


def test_readyz_is_503_when_the_database_cannot_open(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(
        health_routes, "database_openable", lambda *a, **k: (False, "OperationalError")
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["database"]["detail"] == "OperationalError"


def test_readyz_is_degraded_when_the_crew_server_is_down(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(health_routes, "_probe_card", lambda *a, **k: (False, "ConnectError"))
    health_routes.reset_card_cache()

    body = client.get("/readyz").json()

    assert body["status"] == "ready", "the API itself is still usable"
    assert body["degraded"] is True
    assert body["checks"]["crew_server"]["ok"] is False


def test_readyz_never_blocks_the_event_loop() -> None:
    # A sync handler runs in the threadpool, so a slow card probe cannot stall
    # other requests. An async handler would.
    assert inspect.iscoroutinefunction(health_routes.readyz) is False


def test_card_probe_is_cached_for_a_short_ttl(client: TestClient, monkeypatch) -> None:
    calls: list[int] = []

    def counter(*_args, **_kwargs):
        calls.append(1)
        return True, ""

    monkeypatch.setattr(health_routes, "_probe_card", counter)
    health_routes.reset_card_cache()

    assert client.get("/readyz").json()["checks"]["crew_server"]["cached"] is False
    second = client.get("/readyz").json()["checks"]["crew_server"]

    assert len(calls) == 1, "the second probe must come from the cache"
    assert second["cached"] is True


def test_api_health_is_untouched_for_the_ui(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert body["crew_server"] == "down"
    assert "patterns" in body and "replay_available" in body


# --- lifecycle --------------------------------------------------------------


def test_prepare_runtime_creates_the_writable_directories(
    data_dir, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    state = health_routes.prepare_runtime()

    assert state["ok"] is True, state["failures"]
    for relative in ("data/settings", "data/tmp", ".run/runs", ".run/api"):
        assert (tmp_path / relative).is_dir(), relative


def test_prepare_runtime_reports_an_unusable_volume(data_dir, monkeypatch) -> None:
    monkeypatch.setattr(
        health_routes, "volume_writable", lambda *a, **k: (False, "Read-only file system")
    )

    state = health_routes.prepare_runtime()

    assert state["ok"] is False
    assert any("not writable" in failure for failure in state["failures"])


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def terminate_all(self, timeout: float = 5.0) -> bool:
        self.calls.append(timeout)
        return True


def test_lifespan_startup_and_shutdown_run_both_hooks(
    data_dir, monkeypatch, tmp_path
) -> None:
    stopped: list[float] = []
    monkeypatch.setenv("ATHENA_CREW_PID_FILE", str(tmp_path / "crew.pid"))
    monkeypatch.setenv("ATHENA_CREW_STOP_FILE", str(tmp_path / "crew.stop"))
    monkeypatch.setattr(
        health_routes, "stop_crew_server", lambda timeout=5.0: stopped.append(timeout) or True
    )
    registry = _FakeRegistry()

    app = create_app(registry=registry)
    with TestClient(app) as running:
        assert running.get("/healthz").status_code == 200

    assert registry.calls == [health_routes.SHUTDOWN_TIMEOUT_S]
    assert stopped == [health_routes.SHUTDOWN_TIMEOUT_S]
    assert (tmp_path / "crew.pid").exists() is False


def test_stop_crew_server_is_a_noop_without_a_pid_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ATHENA_CREW_PID_FILE", str(tmp_path / "missing.pid"))
    monkeypatch.setenv("ATHENA_CREW_STOP_FILE", str(tmp_path / "crew.stop"))

    assert health_routes.stop_crew_server(timeout=0.1) is True
    assert (tmp_path / "crew.stop").exists(), "the supervisor is told to stop first"


def test_stop_crew_server_ignores_a_dead_pid(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "crew.pid"
    pid_file.write_text("999999999", encoding="utf-8")
    monkeypatch.setenv("ATHENA_CREW_PID_FILE", str(pid_file))
    monkeypatch.setenv("ATHENA_CREW_STOP_FILE", str(tmp_path / "crew.stop"))

    assert health_routes.stop_crew_server(timeout=0.1) is True


# --- run termination --------------------------------------------------------


def test_terminate_all_stops_a_real_run_subprocess(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runs_module, "RUN_DIR", tmp_path / "runs")
    registry = runs_module.RunRegistry(
        command=[sys.executable, "-c", "import time; time.sleep(60)"]
    )

    registry.start("a question", "sequential", tools=[])
    process = registry.active.process
    assert process.poll() is None

    started = time.monotonic()
    assert registry.terminate_all(timeout=5.0) is True
    elapsed = time.monotonic() - started

    assert process.poll() is not None, "the run process must be gone"
    assert elapsed < 5.0
    assert registry.refresh().finished is True


def test_terminate_all_with_no_active_run_is_true() -> None:
    assert runs_module.RunRegistry().terminate_all(timeout=0.1) is True


def test_terminate_all_kills_a_process_that_ignores_terminate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runs_module, "RUN_DIR", tmp_path / "runs")
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    registry = runs_module.RunRegistry(command=[sys.executable, "-c", script])
    registry.start("a question", "sequential", tools=[])

    started = time.monotonic()
    assert registry.terminate_all(timeout=0.5) is True

    assert registry.active.process.poll() is not None
    assert time.monotonic() - started < 5.0


# --- logging ----------------------------------------------------------------


def test_json_formatter_emits_one_object_with_extras() -> None:
    record = logging.LogRecord(
        "athena.api", logging.ERROR, __file__, 1, "boom %s", ("x",), None
    )
    record.event = "unhandled"
    record.path = "/api/runs"

    payload = health_routes.JsonFormatter().format(record)

    assert payload.startswith("{") and payload.endswith("}")
    assert '"level": "ERROR"' in payload
    assert '"msg": "boom x"' in payload
    assert '"event": "unhandled"' in payload
    assert '"path": "/api/runs"' in payload


def test_unhandled_errors_are_logged(client: TestClient, monkeypatch, caplog) -> None:
    import athena.api.app as app_module

    def boom() -> dict:
        raise RuntimeError("kaboom")

    # Scope the patch so it cannot leak into later tests in the same process.
    monkeypatch.setattr(app_module, "_snapshot", lambda *_a, **_k: boom())
    # The handler answers 500 and Starlette re-raises for the test client, so
    # this one test needs the client that does not re-raise.
    lenient = TestClient(create_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="athena.api"):
        response = lenient.get("/api/runs/nope")

    assert response.status_code == 500
    assert response.json()["detail"].startswith("RuntimeError")
    assert "Traceback" not in response.text
    assert any("unhandled error" in record.message for record in caplog.records)


def test_configure_logging_does_not_disturb_existing_handlers(monkeypatch) -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    health_routes.configure_logging()

    assert list(root.handlers) == before, "basicConfig must not be forced"


def test_subprocess_module_is_importable_for_the_run_command() -> None:
    # terminate_all relies on a real Popen, so prove the module can spawn one.
    process = subprocess.Popen([sys.executable, "-c", "print('ok')"], stdout=subprocess.PIPE)
    out, _ = process.communicate(timeout=30)
    assert process.returncode == 0 and out.strip() == b"ok"
