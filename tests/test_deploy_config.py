"""Deployment config: host allowlist, container files, CI contract.

Everything here is a static or in-process assertion. No build, no network, no
cloud CLI: the point is that a public deployment cannot silently regress to
"localhost only" or ship a secret inside the image.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from athena.api import guards
from athena.api.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    monkeypatch.delenv(guards.ENV_ALLOWED_HOSTS, raising=False)
    return TestClient(create_app())


# --- host allowlist ---------------------------------------------------------


def test_defaults_are_unchanged(monkeypatch) -> None:
    monkeypatch.delenv(guards.ENV_ALLOWED_HOSTS, raising=False)

    assert guards.configured_hosts() == set(guards.ALLOWED_HOSTS)
    assert guards.host_allowed("localhost")
    assert guards.host_allowed("127.0.0.1")
    assert guards.host_allowed("testserver")
    assert not guards.host_allowed("athena-mas.fly.dev")
    assert not guards.host_allowed("")


def test_env_adds_hosts_without_dropping_the_local_ones(monkeypatch) -> None:
    monkeypatch.setenv(
        guards.ENV_ALLOWED_HOSTS, " Athena-Mas.fly.dev , athena.example.com,,"
    )
    allowed = guards.configured_hosts()

    assert {"athena-mas.fly.dev", "athena.example.com"} <= allowed
    assert {"localhost", "127.0.0.1", "testserver"} <= allowed
    assert guards.host_allowed("ATHENA-MAS.fly.dev.", allowed)


def test_wildcard_env_entries_match_subdomains(monkeypatch) -> None:
    monkeypatch.setenv(guards.ENV_ALLOWED_HOSTS, "*.fly.dev,.example.com")

    assert guards.host_allowed("athena-mas.fly.dev")
    assert guards.host_allowed("fly.dev")
    assert guards.host_allowed("a.b.example.com")
    assert not guards.host_allowed("evil-fly.dev")


def test_public_write_needs_the_host_in_the_allowlist(client: TestClient) -> None:
    refused = client.post(
        "/api/runs",
        json={"question": "hi", "pattern": "sequential"},
        headers={"host": "athena-mas.fly.dev"},
    )
    assert refused.status_code == 400
    assert "Host" in refused.json()["detail"]


def test_allowed_public_host_reaches_the_handler(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(guards.ENV_ALLOWED_HOSTS, "athena-mas.fly.dev")

    response = client.post(
        "/api/runs",
        json={"question": "hi", "pattern": "nope"},
        headers={"host": "athena-mas.fly.dev"},
    )

    # Past the guard: the request now fails on its own merits, not on Host.
    assert response.status_code == 422
    assert "unknown pattern" in response.json()["detail"]


def test_cross_origin_write_is_still_refused_with_a_public_host(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(guards.ENV_ALLOWED_HOSTS, "athena-mas.fly.dev")

    response = client.post(
        "/api/runs",
        json={"question": "hi", "pattern": "sequential"},
        headers={"host": "athena-mas.fly.dev", "origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_same_origin_write_passes_without_being_allowlisted(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(guards.ENV_ALLOWED_HOSTS, "athena-mas.fly.dev")

    response = client.post(
        "/api/runs",
        json={"question": "hi", "pattern": "nope"},
        headers={"host": "athena-mas.fly.dev", "origin": "https://athena-mas.fly.dev"},
    )

    assert response.status_code == 422, "same-origin is never cross-site"


def test_get_with_an_unknown_host_is_refused_by_the_settings_guard(
    client: TestClient,
) -> None:
    response = client.get("/api/settings/profiles", headers={"host": "evil.example"})
    assert response.status_code == 400


# --- container files --------------------------------------------------------


def test_dockerfile_is_pinned_non_root_and_health_checked() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.13-slim" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert "USER athena" in dockerfile, "the container must not run as root"
    assert "HEALTHCHECK" in dockerfile and "/healthz" in dockerfile
    assert "ATHENA_DATA_DIR=/app/data" in dockerfile
    assert "requirements.txt" in dockerfile


def test_dockerfile_never_bakes_a_secret_or_local_state() -> None:
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    instructions = [
        line.strip() for line in lines if line.strip() and not line.strip().startswith("#")
    ]

    for forbidden in (
        "ATHENA_ACCESS_TOKEN=",
        "LLM_API_KEY=",
        "COPY .env",
        "COPY data/settings",
        "COPY . .",
        "master.key",
        "secrets.enc.json",
    ):
        assert not [line for line in instructions if forbidden in line], forbidden


def test_dockerignore_keeps_secrets_and_local_state_out_of_the_context() -> None:
    patterns = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    for required in (
        ".env",
        ".venv/",
        "data/settings/",
        "**/master.key",
        "**/secrets.enc.json",
        "*.db",
        ".git/",
        "tests/",
    ):
        assert required in patterns, f"missing .dockerignore rule: {required}"


def test_entrypoint_supervises_the_crew_server_then_execs_uvicorn() -> None:
    path = ROOT / "docker" / "entrypoint.sh"
    script = path.read_text(encoding="utf-8")

    assert script.startswith("#!/bin/sh"), "a sh entrypoint needs no interpreter to boot"
    assert "\r\n" not in script, "a CRLF entrypoint fails in a linux container"
    assert "set -eu" in script
    assert "python -m athena.agents.crew_server" in script
    assert "wait \"$child\"" in script, "the supervisor must notice a crash"
    assert "restarting in" in script, "a dead crew server must be restarted"
    assert "ATHENA_CREW_MAX_BACKOFF_S" in script, "a crash loop must back off"
    assert "exec uvicorn athena.api.app:create_app --factory" in script
    assert "--host 0.0.0.0" in script and '--port "${PORT}"' in script
    assert "is not writable" in script, "a missing volume must fail loudly"
    assert "ATHENA_ACCESS_TOKEN" not in script


def test_fly_config_is_one_machine_one_volume() -> None:
    with (ROOT / "fly.toml").open("rb") as handle:
        config = tomllib.load(handle)

    http = config["http_service"]
    assert config["app"] == "athena-mas"
    assert http["internal_port"] == 8080
    assert http["min_machines_running"] == 1
    assert http["auto_stop_machines"] is False, "a run in progress must not be stopped"
    assert http["auto_start_machines"] is False
    assert http["force_https"] is True
    assert http["checks"][0]["path"] == "/healthz"
    assert config["mounts"][0]["destination"] == "/app/data"
    assert config["vm"][0]["memory"] == "2gb"
    assert config["vm"][0]["cpus"] == 2
    assert "/app/data" in config["env"]["ATHENA_DATA_DIR"]
    assert "ATHENA_ACCESS_TOKEN" not in config["env"], "secrets live in fly secrets"


def test_render_blueprint_is_the_documented_alternative() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")

    assert "runtime: docker" in text
    assert "dockerfilePath: ./Dockerfile" in text
    assert "healthCheckPath: /healthz" in text
    assert "mountPath: /app/data" in text
    assert "numInstances: 1" in text
    assert "ATHENA_ACCESS_TOKEN" in text and "sync: false" in text


def test_runtime_dependencies_are_pinned() -> None:
    pins = [
        line.split("==")[0].strip().lower()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.strip().startswith("#")
    ]

    for name in ("cryptography", "httpx", "uvicorn", "fastapi"):
        assert name in pins, f"{name} must be pinned for a reproducible image"


def test_ci_runs_the_offline_suite_and_never_deploys() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m pytest -q" in text
    assert "logic.test.js" in text and "app.composer.test.js" in text
    assert "SEARCH_PROVIDER: disabled" in text
    for forbidden in ("fly deploy", "docker push", "render deploy", "AWS_SECRET"):
        assert forbidden not in text
