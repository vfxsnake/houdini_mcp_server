"""Exercise the MCP server against a REAL Houdini bridge.

Unlike test_roundtrip.py (which uses the mock), this needs a live bridge. The
easy way is headless, no GUI required:

    hython tests/live_scene.py            # builds a scene, serves on 8009
    python tests/test_live_bridge.py      # in another shell

It also works against a graphical Houdini session with the bridge loaded --
just point it at that port:

    python tests/test_live_bridge.py --port 8008
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402

from server.config import Config  # noqa: E402
from server.main import build_server  # noqa: E402


async def run_checks(port: int) -> int:
    mcp, houdini = build_server(Config(bridge_port=port))
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}  {detail}")

    try:
        async with Client(mcp) as client:
            print("\n== Scene ==")
            info = json.loads((await client.read_resource("scene://info"))[0].text)
            print(f"  hip={info['hip_name']} version={info['houdini_version']} "
                  f"fps={info['fps']} range={info['frame_range']}")
            check("houdini version reported", bool(info["houdini_version"]))
            check("fps is numeric", isinstance(info["fps"], (int, float)))
            check("frame range is a pair", len(info["frame_range"]) == 2)

            graph = json.loads((await client.read_resource("scene://graph"))[0].text)
            top = [c["name"] for c in graph.get("children", [])]
            check("graph roots at /", graph["path"] == "/")
            check("graph sees /obj", "obj" in top, str(top))

            print("\n== Nodes ==")
            node = json.loads(
                (await client.call_tool("get_node_info", {"path": "/obj/geo1"}))
                .content[0].text
            )
            check("node type resolved", node["type"] == "geo", node["type"])
            check("non-default parm surfaced", "ty" in node["parms"], str(node["parms"]))
            check("parms_total is the real count",
                  node["parms_total"] > 50, str(node["parms_total"]))
            check("no phantom cook_time_ms", "cook_time_ms" not in node["cook_state"])

            full = json.loads(
                (await client.call_tool(
                    "get_node_info", {"path": "/obj/geo1", "include_defaults": True}))
                .content[0].text
            )
            check("include_defaults widens the result",
                  len(full["parms"]) > len(node["parms"]),
                  f'{len(full["parms"])} vs {len(node["parms"])}')

            sel = json.loads((await client.read_resource("scene://selected"))[0].text)
            check("selection visible", sel["count"] >= 1, str(sel["count"]))

            print("\n== Geometry ==")
            geo = json.loads(
                (await client.read_resource("scene://geometry/obj/geo1/OUT"))[0].text
            )
            print(f"  points={geo['counts']['points']} from={geo['geometry_from']}")
            check("point count read", geo["counts"]["points"] == 500, str(geo["counts"]))
            check("P attribute present",
                  any(a["name"] == "P" for a in geo["attributes"]["point"]))
            check("bbox has 3 components", len(geo["bounding_box"]["min"]) == 3)
            check("geometry_from names the SOP",
                  geo["geometry_from"].endswith("/OUT"), geo["geometry_from"])

            obj_geo = json.loads(
                (await client.read_resource("scene://geometry/obj/geo1"))[0].text
            )
            check("object resolves through its display flag",
                  obj_geo["geometry_from"] == "/obj/geo1/OUT",
                  obj_geo["geometry_from"])

            print("\n== Parameters ==")
            parm = json.loads(
                (await client.call_tool(
                    "get_parm_value", {"node": "/obj/geo1", "parm": "ty"}))
                .content[0].text
            )
            check("value reads back", abs(parm["value"] - 1.5) < 1e-6, str(parm["value"]))
            check("default state reported", parm["is_default"] is False)

            tup = json.loads(
                (await client.call_tool(
                    "get_parm_value", {"node": "/obj/geo1", "parm": "t"}))
                .content[0].text
            )
            check("parm tuple resolves",
                  isinstance(tup["value"], list) and len(tup["value"]) == 3,
                  str(tup["value"]))

            print("\n== Execute ==")
            ex = json.loads(
                (await client.call_tool("execute_python", {
                    "code": "print('hi from houdini')\n"
                            "len(hou.node('/obj').children())"}))
                .content[0].text
            )
            check("stdout captured", "hi from houdini" in ex["stdout"], ex["stdout"])
            check("trailing expression returned", ex["result"] == 2, str(ex["result"]))

            broken = json.loads(
                (await client.call_tool("execute_python", {"code": "1/0"}))
                .content[0].text
            )
            check("failure reported", broken["success"] is False)
            check("traceback returned", "ZeroDivisionError" in broken["stderr"])

            print("\n== Errors ==")
            try:
                await client.call_tool("get_node_info", {"path": "/obj/nope"})
                check("missing node raises", False)
            except Exception as exc:  # noqa: BLE001
                check("missing node is readable", "Node not found" in str(exc),
                      str(exc)[:120])

            # A camera contains real SOPs building its frustum guide, so this
            # must be refused on semantics, not on structure.
            try:
                await client.read_resource("scene://geometry/obj/cam1")
                check("camera guide geometry refused", False,
                      "returned guide geometry as if it were real")
            except Exception as exc:  # noqa: BLE001
                check("camera guide geometry refused",
                      "not a geometry container" in str(exc), str(exc)[:120])
    finally:
        await houdini.aclose()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("All live checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Houdini bridge checks")
    parser.add_argument("--port", type=int, default=8009,
                        help="Bridge port (8009 for tests/live_scene.py, 8008 for a GUI session)")
    args = parser.parse_args()
    return asyncio.run(run_checks(args.port))


if __name__ == "__main__":
    raise SystemExit(main())
