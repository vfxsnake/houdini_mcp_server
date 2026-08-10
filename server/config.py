"""Runtime configuration, overridable via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Houdini bridge (hwebserver). Stays on loopback -- the MCP server runs on
    # the same Windows host as Houdini, so it never needs a wider bind.
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8008
    bridge_timeout: float = 30.0

    # MCP server. Binds 0.0.0.0 so Claude Code in WSL can reach it via the
    # Windows host IP; Claude Desktop reaches it on localhost.
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 3000

    @property
    def bridge_api_url(self) -> str:
        """hwebserver exposes all apiFunction handlers under a single /api endpoint."""
        return f"http://{self.bridge_host}:{self.bridge_port}/api"


def load_config() -> Config:
    return Config(
        bridge_host=os.environ.get("HOUDINI_BRIDGE_HOST", Config.bridge_host),
        bridge_port=int(os.environ.get("HOUDINI_BRIDGE_PORT", Config.bridge_port)),
        bridge_timeout=float(
            os.environ.get("HOUDINI_BRIDGE_TIMEOUT", Config.bridge_timeout)
        ),
        mcp_host=os.environ.get("HOUDINI_MCP_HOST", Config.mcp_host),
        mcp_port=int(os.environ.get("HOUDINI_MCP_PORT", Config.mcp_port)),
    )
