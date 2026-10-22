"""Stage 3: a tiny MCP server (official `mcp` SDK, FastMCP, stdio).

Exposes one tool, ``add_numbers``. The ADK supervisor attaches this file as an
MCP toolset (see spike/adk_agent.py), so the arithmetic runs in a separate
process over the Model Context Protocol rather than as a local Python call.

Run standalone (usually not needed - ADK starts it):
    .venv\\Scripts\\python spike\\mcp_server.py
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("athena-tools")


@mcp.tool()
def add_numbers(a: float, b: float) -> float:
    """Add two numbers and return the sum.

    Args:
        a: The first number.
        b: The second number.
    """
    return float(a) + float(b)


if __name__ == "__main__":
    mcp.run(transport="stdio")
