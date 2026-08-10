"""MCP tools that act on, or query, the live Houdini session."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .docs import DocsIndex, NAMESPACES, format_page
from .houdini_client import BridgeError, HoudiniClient


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def register(mcp: FastMCP, client: HoudiniClient, docs: DocsIndex | None = None) -> None:
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

    if docs is None:
        return

    @mcp.tool(
        name="search_docs",
        description=(
            "Search the SideFX documentation that ships with the installed Houdini "
            "— HOM (Python), VEX, APEX and the node reference for every context. "
            "Returns matching topics with their doc URIs; read one in full with "
            "get_doc, or via its docs:// resource. Restrict with namespace='hom', "
            "'vex', 'apex' or 'nodes'. Plain questions work: the terms are OR-ed "
            "and results ranked, so 'how do I read a point attribute' is a fine "
            "query. Prefer this over recalling Houdini APIs from memory — it is "
            "version-matched to the user's install."
        ),
        annotations={
            "title": "Search Houdini documentation",
            "readOnlyHint": True,
            "idempotentHint": True,
        },
    )
    async def search_docs(query: str, namespace: str = "", limit: int = 10) -> str:
        if namespace and namespace not in NAMESPACES:
            raise ToolError(
                f"Unknown namespace {namespace!r}. Choose one of: "
                f"{', '.join(sorted(NAMESPACES))}, or omit it to search everything."
            )
        try:
            hits = docs.search(query, namespace=namespace or None, limit=limit)
        except (ValueError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

        if not hits:
            raise ToolError(
                f"No documentation matched {query!r}"
                + (f" in the {namespace} namespace." if namespace else ".")
            )
        return _dump(
            {
                "query": query,
                "count": len(hits),
                "results": [
                    {
                        "uri": f"docs://{h['namespace']}/{h['topic']}",
                        "namespace": h["namespace"],
                        "topic": h["topic"],
                        "title": h["title"],
                        "type": h["kind"],
                        "context": h["context"],
                        "summary": h["summary"],
                        "excerpt": h["excerpt"],
                    }
                    for h in hits
                ],
            }
        )

    @mcp.tool(
        name="get_doc",
        description=(
            "Read one documentation page in full, as shipped with the installed "
            "Houdini. Namespace is 'hom', 'vex', 'apex' or 'nodes'; topic is the "
            "path search_docs returned, e.g. namespace='hom', topic='hou/Node' or "
            "namespace='nodes', topic='sop/scatter'. A bare name like 'scatter' is "
            "resolved when unambiguous."
        ),
        annotations={
            "title": "Read a Houdini documentation page",
            "readOnlyHint": True,
            "idempotentHint": True,
        },
    )
    async def get_doc(namespace: str, topic: str) -> str:
        if namespace not in NAMESPACES:
            raise ToolError(
                f"Unknown namespace {namespace!r}. Choose one of: "
                f"{', '.join(sorted(NAMESPACES))}."
            )
        topic = topic.strip().strip("/")
        try:
            page = docs.get(namespace, topic)
            if page is not None:
                return format_page(page)
            candidates = docs.resolve(namespace, topic)
        except FileNotFoundError as exc:
            raise ToolError(str(exc)) from exc

        if len(candidates) == 1:
            found = docs.get(namespace, candidates[0]["topic"])
            if found is not None:
                return format_page(found)
        if candidates:
            listed = "\n".join(
                f"  {c['topic']} — {c['title']}" for c in candidates[:15]
            )
            raise ToolError(
                f"No exact page '{topic}' in {namespace}. Did you mean:\n{listed}"
            )
        raise ToolError(
            f"No page '{topic}' in the {namespace} namespace. "
            f"Use search_docs to find the right topic."
        )
