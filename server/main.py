"""Entry point for the Houdini MCP server.

Runs as a standalone Windows process alongside Houdini. Speaks streamable HTTP
so Claude Desktop (Windows, via localhost) and Claude Code (WSL, via the
Windows host IP) can both connect to the same server.
"""

from __future__ import annotations

import argparse

from fastmcp import FastMCP

from . import resources, tools
from .config import Config, load_config
from .houdini_client import HoudiniClient

INSTRUCTIONS = """
This server gives you read-only context on a live Houdini session, plus the
ability to run Python in it when asked.

Use it as an expert Houdini consultant would: look before you act. Read
`scene://info` to learn what file is open, `scene://selected` to see what the
artist is working on, and `scene://errors` when something is broken. Reach for
`execute_python` only when the user actually asks you to change the scene, and
remind them to save first.

Node paths are absolute and Houdini-style, e.g. /obj/geo1/OUT.
""".strip()


def build_server(config: Config | None = None) -> tuple[FastMCP, HoudiniClient]:
    config = config or load_config()
    client = HoudiniClient(config)

    mcp = FastMCP(name="houdini", instructions=INSTRUCTIONS)
    resources.register(mcp, client)
    tools.register(mcp, client)
    return mcp, client


def main() -> None:
    config = load_config()

    parser = argparse.ArgumentParser(description="Houdini MCP server")
    parser.add_argument("--host", default=config.mcp_host)
    parser.add_argument("--port", type=int, default=config.mcp_port)
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=config.bridge_port,
        help="Port of Houdini's hwebserver bridge (default: 8008)",
    )
    args = parser.parse_args()

    config = Config(
        bridge_host=config.bridge_host,
        bridge_port=args.bridge_port,
        bridge_timeout=config.bridge_timeout,
        mcp_host=args.host,
        mcp_port=args.port,
    )

    mcp, _client = build_server(config)

    print(f"Houdini MCP server on http://{config.mcp_host}:{config.mcp_port}/mcp")
    print(f"Talking to Houdini bridge at {config.bridge_api_url}")
    mcp.run(transport="http", host=config.mcp_host, port=config.mcp_port)


if __name__ == "__main__":
    main()
