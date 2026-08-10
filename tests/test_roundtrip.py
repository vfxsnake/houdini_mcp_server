"""End-to-end check: MCP client -> MCP server -> bridge wire protocol.

Runs the mock bridge on a real socket in a background thread, so the HTTP path
in houdini_client.py is genuinely exercised. The MCP side uses FastMCP's
in-memory transport, which still goes through full resource/tool dispatch.

    python -m pytest tests/ -v          (or just: python tests/test_roundtrip.py)
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402

from server.config import Config  # noqa: E402
from server.houdini_client import BridgeUnavailable, HoudiniClient  # noqa: E402
from server.main import build_server  # noqa: E402
from tests.mock_bridge import Handler  # noqa: E402


def start_mock_bridge() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


async def run_checks() -> int:
    bridge, port = start_mock_bridge()
    config = Config(bridge_port=port)
    mcp, houdini = build_server(config)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {label}")
        else:
            failures.append(f"{label}{' -- ' + detail if detail else ''}")
            print(f"  FAIL  {label} {detail}")

    try:
        async with Client(mcp) as client:
            print("\n== Discovery ==")
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            tool_list = await client.list_tools()

            uris = {str(r.uri) for r in resources}
            template_uris = {str(t.uriTemplate) for t in templates}
            tool_names = {t.name for t in tool_list}

            print(f"  resources: {sorted(uris)}")
            print(f"  templates: {sorted(template_uris)}")
            print(f"  tools:     {sorted(tool_names)}")

            check(
                "static resources registered",
                uris
                == {
                    "scene://info",
                    "scene://graph",
                    "scene://selected",
                    "scene://errors",
                },
                str(uris),
            )
            check(
                "geometry template registered",
                any("geometry" in u for u in template_uris),
                str(template_uris),
            )
            check(
                "tools registered",
                tool_names
                == {
                    "execute_python",
                    "get_node_info",
                    "get_parm_value",
                    "get_node_tree",
                },
                str(tool_names),
            )

            print("\n== Resources ==")
            info = json.loads((await client.read_resource("scene://info"))[0].text)
            check("scene://info returns hip file", info.get("hip_name") == "mock_scene.hip")
            check("scene://info returns fps", info.get("fps") == 24.0)

            graph = json.loads((await client.read_resource("scene://graph"))[0].text)
            check("scene://graph roots at /", graph.get("path") == "/")
            check("scene://graph nests children", "children" in graph)

            sel = json.loads((await client.read_resource("scene://selected"))[0].text)
            check("scene://selected returns a node", sel.get("count") == 1)
            check(
                "scene://selected node is scatter1",
                sel["nodes"][0]["name"] == "scatter1",
            )

            errs = json.loads((await client.read_resource("scene://errors"))[0].text)
            check("scene://errors returns errors", len(errs.get("errors", [])) == 1)
            check("scene://errors returns warnings", len(errs.get("warnings", [])) == 1)

            geo = json.loads(
                (await client.read_resource("scene://geometry/obj/geo1/OUT"))[0].text
            )
            check("geometry template resolves multi-segment path",
                  geo.get("path") == "/obj/geo1/OUT", str(geo)[:200])
            check("geometry returns point count", geo["counts"]["points"] == 4000)
            check("geometry names its source SOP",
                  geo.get("geometry_from") == "/obj/geo1/OUT", str(geo.get("geometry_from")))

            obj_geo = json.loads(
                (await client.read_resource("scene://geometry/obj/geo1"))[0].text
            )
            check("object resolves through its display flag",
                  obj_geo.get("geometry_from") == "/obj/geo1/OUT",
                  str(obj_geo.get("geometry_from")))

            print("\n== Tools ==")
            node = json.loads((await client.call_tool(
                "get_node_info", {"path": "/obj/geo1/scatter1"})).content[0].text)
            check("get_node_info returns type", node.get("type") == "scatter::2.0")
            check("get_node_info returns inputs", node["inputs"] == ["/obj/geo1/box1"])

            parm = json.loads((await client.call_tool(
                "get_parm_value", {"node": "/obj/geo1", "parm": "ty"})).content[0].text)
            check("get_parm_value returns value", parm.get("value") == 1.5)

            tree = json.loads((await client.call_tool(
                "get_node_tree", {"root": "/obj/geo1", "depth": 1})).content[0].text)
            check("get_node_tree honours root", tree.get("path") == "/obj/geo1")
            check("get_node_tree honours depth", "children" in tree)

            executed = json.loads((await client.call_tool(
                "execute_python", {"code": "hou.node('/obj')"})).content[0].text)
            check("execute_python round-trips", executed.get("success") is True)

            print("\n== Error propagation ==")
            try:
                await client.call_tool("get_node_info", {"path": "/obj/does_not_exist"})
                check("missing node raises", False, "no exception raised")
            except Exception as exc:  # noqa: BLE001 - want whatever the client raises
                check(
                    "missing node surfaces bridge message",
                    "Node not found" in str(exc),
                    str(exc)[:200],
                )

            try:
                await client.call_tool(
                    "get_parm_value", {"node": "/obj/geo1", "parm": "nope"})
                check("missing parm raises", False, "no exception raised")
            except Exception as exc:  # noqa: BLE001
                check(
                    "missing parm lists available parms",
                    "Available" in str(exc),
                    str(exc)[:200],
                )

            # Mirrors test_live_bridge.py: a camera's frustum guide SOPs must
            # not be reported as the camera's own geometry.
            try:
                await client.read_resource("scene://geometry/obj/cam1")
                check("camera guide geometry refused", False, "returned geometry")
            except Exception as exc:  # noqa: BLE001
                check("camera guide geometry refused",
                      "not a geometry container" in str(exc), str(exc)[:200])
    finally:
        await houdini.aclose()
        bridge.shutdown()

    print("\n== Bridge offline behaviour ==")
    offline = HoudiniClient(Config(bridge_port=1))
    try:
        await offline.scene_info()
        check("offline bridge raises", False, "no exception raised")
    except BridgeUnavailable as exc:
        check("offline bridge gives actionable message", "Is Houdini running" in str(exc))
    finally:
        await offline.aclose()

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


def test_roundtrip() -> None:
    assert asyncio.run(run_checks()) == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_checks()))
