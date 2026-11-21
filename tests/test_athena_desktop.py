import concurrent.futures
import http.client
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.e2e
def test_desktop_backend_is_local_and_closes_with_parent(tmp_path):
    args = [sys.executable, "-u", "scripts/athena_desktop.py", "--data-dir", str(tmp_path), "--port"]
    env = {**os.environ, "ATHENA_ACCESS_TOKEN": "desktop-test"}
    with subprocess.Popen(
        [*args, "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env,
    ) as backend:
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                ready = executor.submit(backend.stdout.readline)
                try:
                    marker = ready.result(timeout=20).strip()
                except TimeoutError:
                    backend.kill()
                    raise
            assert marker.startswith("ATHENA_DESKTOP_PORT=")
            port = int(marker.split("=")[1])

            def request(method, path, headers=None, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
                try:
                    connection.request(method, path, headers=headers or {}, body=body)
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            assert request("GET", "/healthz") == (200, b'{"status":"ok"}')
            assert request("GET", "/healthz", {"Host": "example.invalid"})[0] == 403
            assert request("POST", "/api/personal/reminders", {"Origin": "https://example.invalid"})[0] == 403
            headers = {"Authorization": "Bearer desktop-test", "Origin": f"http://127.0.0.1:{port}"}
            assert b"Follow-ups" in request("GET", "/", headers)[1]
            assert b"report-theme" in request("GET", "/api/replay/hierarchical/report", headers)[1]
            headers["Content-Type"] = "application/json"
            reminder = json.dumps({"title": "Sample", "due_at": "2030-01-01T09:00:00Z"})
            assert request("POST", "/api/personal/reminders", headers, reminder)[0] == 201
            assert (tmp_path / "personal.db").exists()

            second = subprocess.run(
                [*args, str(port)], input="", capture_output=True, text=True, env=env, timeout=10,
            )
            assert second.returncode != 0
            assert "could not bind loopback port" in second.stderr

            backend.stdin.close()
            assert backend.wait(timeout=12) == 0
            with pytest.raises(OSError):
                request("GET", "/healthz")
        finally:
            if backend.poll() is None:
                backend.kill()
