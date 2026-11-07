"""The optional token gate: off by default, constant-time, cookie or header.

No network: the crew-server probe is stubbed, so every assertion is about the
middleware's decision.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from athena.api import auth
from athena.api.app import create_app

TOKEN = "s3cret-shared-token-0123456789"
PROTECTED = "/api/questions"


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    monkeypatch.delenv(auth.ENV_TOKEN, raising=False)
    return TestClient(create_app())


def _basic(value: str) -> dict[str, str]:
    return {
        "Authorization": "Basic "
        + base64.b64encode(value.encode()).decode()
    }


# --- off by default ---------------------------------------------------------


def test_without_the_env_var_nothing_is_gated(client: TestClient) -> None:
    assert auth.auth_enabled() is False
    assert client.get(PROTECTED).status_code == 200
    assert client.get("/api/runs").status_code == 200


def test_login_is_a_noop_when_the_gate_is_off(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --- enabled ----------------------------------------------------------------


def test_anonymous_api_read_is_refused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    response = client.get(PROTECTED)

    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert TOKEN not in response.text


def test_bearer_token_is_accepted(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    response = client.get(PROTECTED, headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200


def test_basic_token_is_accepted_in_both_shapes(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    assert client.get(PROTECTED, headers=_basic(f"{TOKEN}:")).status_code == 200
    assert client.get(PROTECTED, headers=_basic(f"anyone:{TOKEN}")).status_code == 200
    assert client.get(PROTECTED, headers=_basic("wrong:wrong")).status_code == 401
    assert client.get(PROTECTED, headers={"Authorization": "Basic !!!"}).status_code == 401


def test_explicit_header_is_accepted(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    ok = client.get(PROTECTED, headers={"X-Athena-Token": TOKEN})
    bad = client.get(PROTECTED, headers={"X-Athena-Token": TOKEN + "x"})

    assert ok.status_code == 200
    assert bad.status_code == 401


def test_wrong_token_is_refused_everywhere(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    assert client.get("/api/runs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post(
        "/api/runs",
        json={"question": "hi", "pattern": "sequential"},
        headers={"Authorization": "Bearer nope"},
    ).status_code == 401
    assert client.get("/api/settings/profiles").status_code == 401


def test_health_probes_are_exempt(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    assert client.get("/healthz").status_code == 200
    # readyz is 200 or 503 depending on the environment, never 401.
    assert client.get("/readyz").status_code in {200, 503}


def test_static_assets_are_exempt_but_the_app_shell_is_not(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    assert client.get("/style.css").status_code == 200
    assert client.get("/logic.js").status_code == 200

    shell = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert shell.status_code == 303
    assert shell.headers["location"].startswith("/login")


def test_browser_api_call_gets_json_not_a_redirect(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    response = client.get(PROTECTED, headers={"accept": "text/html"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


# --- the /login?token= cookie flow -----------------------------------------


def test_login_form_never_contains_the_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    response = client.get("/login")

    assert response.status_code == 200
    assert 'name="token"' in response.text
    assert TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_login_with_the_token_sets_an_httponly_cookie(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    response = client.get(f"/login?token={TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert auth.COOKIE_NAME in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie

    # The cookie is enough for the API and for the page.
    assert client.get(PROTECTED).status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200


def test_login_with_a_bad_token_is_refused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    response = client.get("/login?token=wrong", follow_redirects=False)

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert TOKEN not in response.text


def test_login_cannot_redirect_off_site(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)

    for target in ("https://evil.example/", "//evil.example/x"):
        response = client.get(
            f"/login?token={TOKEN}&next={target}", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_logout_clears_the_cookie(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)
    client.get(f"/login?token={TOKEN}", follow_redirects=False)
    assert client.get(PROTECTED).status_code == 200

    response = client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert client.get(PROTECTED).status_code == 401


# --- the primitive ----------------------------------------------------------


def test_token_comparison_is_exact_and_empty_safe() -> None:
    assert auth.token_matches(TOKEN, TOKEN) is True
    assert auth.token_matches(TOKEN + " ", TOKEN) is False
    assert auth.token_matches("", TOKEN) is False
    assert auth.token_matches(TOKEN, "") is False
    assert auth.token_matches("tökén", "tökén") is True
    assert auth.token_matches("tökén", "token") is False


def test_secure_cookie_is_set_behind_https(monkeypatch) -> None:
    monkeypatch.setenv(auth.ENV_TOKEN, TOKEN)
    monkeypatch.setattr("athena.api.app._crew_server_up", lambda *a, **k: False)
    client = TestClient(create_app(), base_url="https://athena-mas.fly.dev")

    response = client.get(f"/login?token={TOKEN}", follow_redirects=False)

    assert "secure" in response.headers["set-cookie"].lower()
