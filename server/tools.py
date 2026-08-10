"""MCP tools that act on, or query, the live Houdini session."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .houdini_client import BridgeError, HoudiniClient


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def register(mcp: FastMCP, client: HoudiniClient) -> None:
    @mcp.tool(
        name="execute_python",
        description=(
            "Run Python inside the live Houdini session and return stdout, "
            "stderr and the value of the last expression. The `hou` module is "
            "already imported. This mutates the artist's open scene -- prefer "
            "the read-only resources and tools for inspection, and tell the "
            "user to save before running anything destructive."
        ),
        annotations={
            "title": "Execute Python in Houdini",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        },
    )
    async def execute_python(code: str) -> str:
        try:
            return _dump(await client.execute(code))
        except BridgeError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="get_node_info",
        description=(
            "Detailed information for one node: type, parameters, inputs and "
            "outputs, flags and cook state. Path is absolute, e.g. /obj/geo1. "
            "By default only parameters the artist has changed from their "
            "defaults are returned, since a plain geo node has ~90 parameters "
            "and none of them changed; set include_defaults=True to see all."
        ),
        annotations={
            "title": "Get Houdini node info",
            "readOnlyHint": True,
            "idempotentHint": True,
        },
    )
    async def get_node_info(path: str, include_defaults: bool = False) -> str:
        try:
            return _dump(await client.node_info(path, include_defaults))
        except BridgeError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="get_parm_value",
        description=(
            "Read a single parameter's value, along with its raw expression and "
            "whether it is keyframed or at its default. Example: node=/obj/geo1, "
            "parm=tx."
        ),
        annotations={
            "title": "Get Houdini parameter value",
            "readOnlyHint": True,
            "idempotentHint": True,
        },
    )
    async def get_parm_value(node: str, parm: str) -> str:
        try:
            return _dump(await client.parm(path=node, name=parm))
        except BridgeError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="get_node_tree",
        description=(
            "Node graph below a given path, to a given depth. Use this instead "
            "of the scene://graph resource when you need to look inside a "
            "specific subnetwork, e.g. root=/obj/geo1, depth=2."
        ),
        annotations={
            "title": "Get Houdini node tree",
            "readOnlyHint": True,
            "idempotentHint": True,
        },
    )
    async def get_node_tree(root: str = "/", depth: int = 3) -> str:
        try:
            return _dump(await client.node_tree(root=root, depth=depth))
        except BridgeError as exc:
            raise ToolError(str(exc)) from exc
