"""Read-only MCP resources exposing the live Houdini session."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

from .docs import DocsIndex, NAMESPACES, format_page
from .houdini_client import BridgeError, HoudiniClient


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _normalize_node_path(path: str) -> str:
    """Resource URIs carry node paths without the leading slash."""
    path = path.strip()
    return path if path.startswith("/") else "/" + path


def _read_doc(docs: DocsIndex, namespace: str, topic: str) -> str:
    """Fetch a help page, falling back to suggestions rather than a bare miss."""
    topic = topic.strip().strip("/")
    try:
        page = docs.get(namespace, topic)
    except FileNotFoundError as exc:
        raise ResourceError(str(exc)) from exc

    if page is not None:
        return format_page(page)

    candidates = docs.resolve(namespace, topic)
    if len(candidates) == 1:
        found = docs.get(namespace, candidates[0]["topic"])
        if found is not None:
            return format_page(found)
    if candidates:
        listed = "\n".join(
            f"  docs://{c['namespace']}/{c['topic']} — {c['title']}"
            for c in candidates[:15]
        )
        raise ResourceError(
            f"No exact page 'docs://{namespace}/{topic}'. Did you mean:\n{listed}"
        )
    raise ResourceError(
        f"No page 'docs://{namespace}/{topic}'. Use the search_docs tool to find "
        f"the right topic — the {namespace} namespace is indexed but has no such entry."
    )


def register(mcp: FastMCP, client: HoudiniClient, docs: DocsIndex | None = None) -> None:
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

    if docs is None:
        return

    descriptions = {
        "hom": "HOM Python reference — hou.* classes and functions, e.g. "
               "docs://hom/hou/Node",
        "vex": "VEX function reference, e.g. docs://vex/functions/length",
        "apex": "APEX operator and script reference, e.g. docs://apex/apex/Abs",
        "nodes": "Node reference across all contexts, e.g. docs://nodes/sop/scatter",
    }

    def make_reader(namespace: str):
        # A closure per namespace, so the handler signature stays exactly the
        # single {topic*} the URI template declares.
        async def read(topic: str) -> str:
            return _read_doc(docs, namespace, topic)

        return read

    for namespace in NAMESPACES:
        mcp.resource(
            f"docs://{namespace}/{{topic*}}",
            name=f"SideFX {namespace} documentation",
            description=(
                f"{descriptions[namespace]} Pages come from the help shipped with "
                f"Houdini, so they match the installed version. Find topics with "
                f"the search_docs tool."
            ),
            mime_type="text/markdown",
        )(make_reader(namespace))
