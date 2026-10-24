"""A tiny OpenAI-compatible provider for offline tests.

Answers ``POST /chat/completions`` with a canned non-streaming response that
contains a ``usage`` object, so the whole real stack (crewai -> litellm ->
openai SDK -> httpx) can be exercised without a network provider.

Structured output: instructor's litellm mode sends ``tools`` and expects a
``tool_calls`` reply, so when tools are present the arguments are filled from
the requested JSON schema.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PROMPT_TOKENS = 123
COMPLETION_TOKENS = 45
REASONING_TOKENS = 7


def _value_for(spec: dict):
    kind = spec.get("type")
    if kind == "string":
        return "OK"
    if kind == "integer":
        return 1
    if kind == "number":
        return 0.5
    if kind == "boolean":
        return True
    if kind == "array":
        item = spec.get("items", {})
        return [_value_for(item)] if item.get("type") else []
    if kind == "object":
        return _object_for(spec)
    return "OK"


def _object_for(schema: dict) -> dict:
    properties = schema.get("properties", {})
    return {name: _value_for(spec) for name, spec in properties.items()}


def _completion(model: str, message: dict) -> dict:
    return {
        "id": "chatcmpl-fake-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
            "completion_tokens_details": {"reasoning_tokens": REASONING_TOKENS},
        },
    }


class FakeProvider:
    """Records every request; returns a fixed usage block."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:  # keep test output clean
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                provider.requests.append(
                    {
                        "path": self.path,
                        "host": self.headers.get("Host", ""),
                        "model": body.get("model", ""),
                        "body": body,
                    }
                )

                if body.get("tools"):
                    function = body["tools"][0].get("function", {})
                    arguments = _object_for(function.get("parameters") or {})
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": function.get("name", "respond"),
                                         "arguments": json.dumps(arguments)},
                        }],
                    }
                elif body.get("response_format"):
                    message = {"role": "assistant",
                               "content": json.dumps({"text": "OK"})}
                else:
                    message = {"role": "assistant", "content": "OK"}

                payload = json.dumps(_completion(body.get("model", ""), message)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "FakeProvider":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeProvider":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
