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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROMPT_TOKENS = 123
COMPLETION_TOKENS = 45
REASONING_TOKENS = 7


def _value_for(spec: dict, overrides: dict | None = None, name: str = ""):
    if name == "expression":
        return "2 + 2"
    if name == "values":
        return [1, 2, 3, 4]
    if name == "horizon":
        return 2
    if overrides and name in overrides:
        return overrides[name]
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


def _object_for(schema: dict, overrides: dict | None = None) -> dict:
    properties = schema.get("properties", {})
    return {
        name: _value_for(spec, overrides, name)
        for name, spec in properties.items()
    }


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


def _has_tool_result(body: dict) -> bool:
    return any(
        message.get("role") == "tool"
        for message in (body.get("messages") or [])
    )


class FakeProvider:
    """Records every request; returns a fixed usage block."""

    def __init__(self, answer: str = "OK", key_points: list | None = None, port: int = 0) -> None:
        self.requests: list[dict] = []
        self.overrides = {"answer": answer, "key_points": key_points or [answer]}
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

                if body.get("stream"):
                    self._stream(body)
                    return

                if body.get("tools") and not _has_tool_result(body):
                    function = body["tools"][0].get("function", {})
                    arguments = _object_for(function.get("parameters") or {}, provider.overrides)
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
                    message = {"role": "assistant", "content": json.dumps(provider.overrides)}
                else:
                    message = {"role": "assistant", "content": provider.overrides["answer"]}

                payload = json.dumps(_completion(body.get("model", ""), message)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _stream(self, body: dict) -> None:
                model = body.get("model", "")
                tool_mode = bool(body.get("tools")) and not _has_tool_result(body)
                chunks = []
                if tool_mode:
                    function = body["tools"][0].get("function", {})
                    arguments = _object_for(function.get("parameters") or {}, provider.overrides)
                    chunks.append({
                        "id": "chatcmpl-fake-stream",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": function.get("name", "respond"),
                                             "arguments": json.dumps(arguments)},
                            }],
                        }, "finish_reason": None}],
                    })
                    chunks.append({
                        "id": "chatcmpl-fake-stream",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    })
                else:
                    answer = str(provider.overrides.get("answer") or "OK")
                    pieces = [answer[i:i + 12] for i in range(0, len(answer), 12)] or [""]
                    for index, piece in enumerate(pieces):
                        chunks.append({
                            "id": "chatcmpl-fake-stream",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": model,
                            "choices": [{"index": 0, "delta": {
                                "role": "assistant" if index == 0 else None,
                                "content": piece,
                            }, "finish_reason": None}],
                        })
                    chunks.append({
                        "id": "chatcmpl-fake-stream",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    })
                chunks.append({
                    "id": "chatcmpl-fake-stream",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": PROMPT_TOKENS,
                        "completion_tokens": COMPLETION_TOKENS,
                        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                        "completion_tokens_details": {"reasoning_tokens": REASONING_TOKENS},
                    },
                })
                payload = "".join(
                    "data: " + json.dumps(chunk) + "\n\n" for chunk in chunks
                ) + "data: [DONE]\n\n"
                body_bytes = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
