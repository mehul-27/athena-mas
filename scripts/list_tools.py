"""List the tools the three MCP servers expose, as JSON.

    python scripts/list_tools.py

Used by the API's read-only Tools panel. Nothing can be registered here: the
servers are the ones the agents already use.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from athena.agents.researcher import SERVERS  # noqa: E402
from athena.tools.settings import REPO_ROOT  # noqa: E402

SERVER_NAMES = {"rag": "rag", "web": "web", "calc": "calc"}
TIMEOUT_S = 30


async def _tools_for(module: str) -> list[dict]:
    from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
    from mcp import StdioServerParameters

    module_name = SERVERS[module]
    toolset = MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", module_name],
                cwd=str(REPO_ROOT),
                env=None,
            ),
            timeout=TIMEOUT_S,
        )
    )
    tools = await toolset.get_tools()
    listed = [{"name": tool.name, "description": (tool.description or "").split("\n")[0][:160]}
              for tool in tools]
    await toolset.close()
    return listed


async def collect() -> list[dict]:
    servers = []
    for key, module in SERVER_NAMES.items():
        servers.append({"name": f"{key} (MCP)", "tools": await _tools_for(key)})
    return servers


def main() -> int:
    try:
        servers = asyncio.run(collect())
    except Exception as exc:  # noqa: BLE001 - the panel shows "unavailable"
        print(json.dumps({"servers": [], "error": f"{type(exc).__name__}: {exc}"[:200]}))
        return 0
    print(json.dumps({"servers": servers}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
