"""Manual offline chat server: a fake OpenAI-compatible provider + the Athena API.

    .\\.venv\\Scripts\\python scripts\\manual_chat_server.py

This is for local UI verification only. It prints the fake provider base URL;
create a profile in Settings → Add model pointing at it (model: openai/fake-model,
localhost HTTP is allowed, no key needed), then open http://127.0.0.1:8010/.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import uvicorn  # noqa: E402

from athena.api.app import create_app  # noqa: E402
from tests.fake_provider import FakeProvider  # noqa: E402


def main() -> int:
    provider = FakeProvider(
        answer="Hello from the offline provider. This reply is streamed token by token.",
        port=18081,
    ).start()
    print(f"FAKE_BASE={provider.base_url}", flush=True)
    print("MODEL=openai/fake-model", flush=True)
    print("Open http://127.0.0.1:8010/", flush=True)
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=8010, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        provider.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
