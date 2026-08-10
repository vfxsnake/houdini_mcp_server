"""Read-only MCP resources exposing the live Houdini session."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

from .houdini_client import BridgeError, HoudiniClient


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _normalize_node_path(path: str) -> str:
    """Resource URIs carry node paths without the leading slash."""
    path = path.strip()
    return path if path.startswith("/") else "/" + path


def register(mcp: FastMCP, client: HoudiniClient) -> None:
    @mcp.resource(
        "scene://info",
        name="Houdini scene info",
        description=(
            "Current .hip file path, Houdini version, FPS, frame range and "
            "playbar state of the live session."
        ),
        mime_type="application/json",
    )
    async def scene_info() -> str:
        try:
            return _dump(await client.scene_info())
        except BridgeError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource(
        "scene://graph",
        name="Houdini node graph",
        description=(
            "Node tree of the scene starting at /, a few levels deep. For "
            "deeper or narrower views use the get_node_info tool."
        ),
        mime_type="application/json",
    )
    async def scene_graph() -> str:
        try:
            return _dump(await client.node_tree(root="/", depth=3))
        except BridgeError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource(
        "scene://selected",
        name="Selected Houdini nodes",
        description=(
            "Type, parameters, connections and cook state of the nodes the "
            "artist currently has selected."
        ),
        mime_type="application/json",
    )
    async def selected() -> str:
        try:
            return _dump(await client.selected())
        except BridgeError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource(
        "scene://errors",
        name="Houdini cook errors",
        description="Cook errors and warnings gathered across the scene.",
        mime_type="application/json",
    )
    async def errors() -> str:
        try:
            return _dump(await client.errors())
        except BridgeError as exc:
            raise ResourceError(str(exc)) from exc

    @mcp.resource(
        "scene://geometry/{path*}",
        name="Houdini geometry summary",
        description=(
            "Point/primitive/vertex counts, attribute list, groups and bounding "
            "box for a SOP node. Give the node path without the leading slash, "
            "e.g. scene://geometry/obj/geo1/OUT"
        ),
        mime_type="application/json",
    )
    async def geometry(path: str) -> str:
        try:
            return _dump(await client.geometry(_normalize_node_path(path)))
        except BridgeError as exc:
            raise ResourceError(str(exc)) from exc
