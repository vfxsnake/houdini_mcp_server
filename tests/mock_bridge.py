"""A fake Houdini bridge for developing the MCP server without Houdini open.

Speaks the same wire protocol as `hwebserver.apiFunction`: a single POST /api
endpoint taking a `json` form field of `[function_name, args, kwargs]`, and
returning either the JSON result or a 422 `{"error": ...}` body.

Stdlib only, so it runs anywhere (WSL or Windows) with no dependencies:

    python tests/mock_bridge.py --port 8008
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --- Canned scene ---------------------------------------------------------
# A plausible little scene: a scattered-copy setup, a camera, and one node
# deliberately left in an error state so the errors path has something to show.

_SCENE = {
    "hip_file": "C:/DEV/houdini_projects/mock_scene.hip",
    "hip_name": "mock_scene.hip",
    "houdini_version": "22.0.368",
    "fps": 24.0,
    "frame_range": [1001, 1100],
    "current_frame": 1024,
    "is_saved": False,
    "job": "C:/DEV/houdini_projects",
}

_NODES: dict[str, dict[str, Any]] = {
    "/obj": {
        "path": "/obj",
        "name": "obj",
        "type": "obj",
        "category": "Manager",
        "parms": {},
        "inputs": [],
        "outputs": [],
        "children": ["geo1", "cam1"],
        "flags": {"display": True, "render": True, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/out": {
        "path": "/out",
        "name": "out",
        "type": "out",
        "category": "Manager",
        "parms": {},
        "inputs": [],
        "outputs": [],
        "children": ["mantra1"],
        "flags": {"display": True, "render": True, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/obj/geo1": {
        "path": "/obj/geo1",
        "name": "geo1",
        "type": "geo",
        "category": "Object",
        "parms": {"tx": 0.0, "ty": 1.5, "tz": 0.0, "scale": 1.0},
        "inputs": [],
        "outputs": [],
        "children": ["box1", "scatter1", "copytopoints1", "OUT"],
        "flags": {"display": True, "render": True, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/obj/geo1/box1": {
        "path": "/obj/geo1/box1",
        "name": "box1",
        "type": "box",
        "category": "Sop",
        "parms": {"sizex": 1.0, "sizey": 1.0, "sizez": 1.0, "divrate": 3},
        "inputs": [],
        "outputs": ["/obj/geo1/scatter1"],
        "children": [],
        "flags": {"display": False, "render": False, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/obj/geo1/scatter1": {
        "path": "/obj/geo1/scatter1",
        "name": "scatter1",
        "type": "scatter::2.0",
        "category": "Sop",
        "parms": {"npts": 500, "seed": 0, "relaxiterations": 10},
        "inputs": ["/obj/geo1/box1"],
        "outputs": ["/obj/geo1/copytopoints1"],
        "children": [],
        "flags": {"display": False, "render": False, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/obj/geo1/copytopoints1": {
        "path": "/obj/geo1/copytopoints1",
        "name": "copytopoints1",
        "type": "copytopoints::2.0",
        "category": "Sop",
        "parms": {"targetgroup": "", "useidattrib": True, "pack": False},
        "inputs": ["/obj/geo1/box1", "/obj/geo1/scatter1"],
        "outputs": ["/obj/geo1/OUT"],
        "children": [],
        "flags": {"display": False, "render": False, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/obj/geo1/OUT": {
        "path": "/obj/geo1/OUT",
        "name": "OUT",
        "type": "null",
        "category": "Sop",
        "parms": {"copyinput": True},
        "inputs": ["/obj/geo1/copytopoints1"],
        "outputs": [],
        "children": [],
        "flags": {"display": True, "render": True, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/obj/cam1": {
        "path": "/obj/cam1",
        "name": "cam1",
        "type": "cam",
        "category": "Object",
        "parms": {"tx": 0.0, "ty": 2.0, "tz": 8.0, "focal": 50.0, "aperture": 41.4},
        "inputs": [],
        "outputs": [],
        "children": [],
        "flags": {"display": True, "render": True, "bypass": False},
        "cook_state": {"cooked": True, "cook_count": 1, "errors": 0, "warnings": 0},
    },
    "/out/mantra1": {
        "path": "/out/mantra1",
        "name": "mantra1",
        "type": "ifd",
        "category": "Driver",
        "parms": {"camera": "/obj/cam_missing", "vm_picture": "$HIP/render/$HIPNAME.$F4.exr"},
        "inputs": [],
        "outputs": [],
        "children": [],
        "flags": {"display": True, "render": True, "bypass": False},
        "cook_state": {"cooked": False, "cook_count": 0, "errors": 1, "warnings": 0},
    },
}

# Which SOP each requestable node resolves to. Objects resolve through their
# display flag; a camera deliberately does not, because its frustum guide SOPs
# would otherwise report as real geometry (see GEOMETRY_OBJECT_TYPES in the
# bridge).
_GEOMETRY_SOURCE = {
    "/obj/geo1": "/obj/geo1/OUT",
    "/obj/geo1/OUT": "/obj/geo1/OUT",
    "/obj/geo1/scatter1": "/obj/geo1/scatter1",
}

_GEOMETRY: dict[str, dict[str, Any]] = {
    "/obj/geo1/OUT": {
        "path": "/obj/geo1/OUT",
        "counts": {"points": 4000, "prims": 3000, "vertices": 12000},
        "bounding_box": {"min": [-2.5, 0.0, -2.5], "max": [2.5, 1.0, 2.5]},
        "attributes": {
            "point": [
                {"name": "P", "type": "float", "size": 3},
                {"name": "N", "type": "float", "size": 3},
                {"name": "pscale", "type": "float", "size": 1},
            ],
            "prim": [{"name": "shop_materialpath", "type": "string", "size": 1}],
            "vertex": [{"name": "uv", "type": "float", "size": 3}],
            "detail": [{"name": "varmap", "type": "string", "size": 1}],
        },
        "groups": {"point": [], "prim": ["sides", "top"], "edge": [], "vertex": []},
    },
    "/obj/geo1/scatter1": {
        "path": "/obj/geo1/scatter1",
        "counts": {"points": 500, "prims": 0, "vertices": 0},
        "bounding_box": {"min": [-0.5, -0.5, -0.5], "max": [0.5, 0.5, 0.5]},
        "attributes": {
            "point": [{"name": "P", "type": "float", "size": 3}],
            "prim": [],
            "vertex": [],
            "detail": [],
        },
        "groups": {"point": [], "prim": [], "edge": [], "vertex": []},
    },
}

_SELECTED = ["/obj/geo1/scatter1"]


class APIError(Exception):
    """Mirrors hwebserver.APIError -> HTTP 422."""


# --- Handlers -------------------------------------------------------------


def _require_node(path: str) -> dict[str, Any]:
    if path not in _NODES:
        raise APIError(f"Node not found: {path}")
    return _NODES[path]


def scene_info() -> dict[str, Any]:
    return dict(_SCENE)


def node_tree(root: str = "/", depth: int = 3) -> dict[str, Any]:
    root = root.rstrip("/") or "/"

    def children_of(parent: str) -> list[str]:
        prefix = "/" if parent == "/" else parent + "/"
        return [
            p
            for p in _NODES
            if p.startswith(prefix) and "/" not in p[len(prefix) :]
        ]

    def build(path: str, level: int) -> dict[str, Any]:
        node = _NODES.get(path)
        entry: dict[str, Any] = {
            "path": path,
            "name": path.rsplit("/", 1)[-1] or "/",
            "type": node["type"] if node else "root",
        }
        if level < depth:
            kids = [build(c, level + 1) for c in children_of(path)]
            if kids:
                entry["children"] = kids
        elif children_of(path):
            entry["children_truncated"] = len(children_of(path))
        return entry

    if root != "/" and root not in _NODES:
        raise APIError(f"Node not found: {root}")
    return build(root, 0)


def _described(path: str, include_defaults: bool = False) -> dict[str, Any]:
    """Mirror the real bridge, which reports non-default parms unless asked."""
    node = dict(_require_node(path))
    node["parms_shown"] = "all" if include_defaults else "non-default only"
    node["parms_total"] = len(node["parms"])
    return node


def node_info(path: str, include_defaults: bool = False) -> dict[str, Any]:
    return _described(path, include_defaults)


def selected() -> dict[str, Any]:
    return {"count": len(_SELECTED), "nodes": [_described(p) for p in _SELECTED]}


def geometry(path: str) -> dict[str, Any]:
    node = _require_node(path)

    if node["type"] == "cam":
        raise APIError(
            f"{path} is a 'cam' object, not a geometry container -- its display "
            f"node ({path}/xform1) produces viewport guide geometry. "
            f"Request that SOP directly if you really want it."
        )

    source = _GEOMETRY_SOURCE.get(path)
    if source is None:
        raise APIError(
            f"Node has no cooked geometry: {path} (type {node['type']}). "
            f"Point this at a SOP, or at an object containing one."
        )

    result = dict(_GEOMETRY[source])
    result["path"] = path
    result["geometry_from"] = source
    return result


def errors() -> dict[str, Any]:
    return {
        "errors": [
            {
                "path": "/out/mantra1",
                "severity": "error",
                "message": "Camera /obj/cam_missing does not exist.",
            }
        ],
        "warnings": [
            {
                "path": "/obj/geo1/copytopoints1",
                "severity": "warning",
                "message": "Point attribute 'orient' not found; using default orientation.",
            }
        ],
    }


def parm(path: str, name: str) -> dict[str, Any]:
    node = _require_node(path)
    if name not in node["parms"]:
        raise APIError(
            f"Parameter '{name}' not found on {path}. "
            f"Available: {sorted(node['parms'])}"
        )
    value = node["parms"][name]
    return {
        "node": path,
        "parm": name,
        "value": value,
        "raw": str(value),
        "is_default": name in ("tx", "tz"),
        "is_keyframed": False,
        "type": type(value).__name__,
    }


def execute(code: str) -> dict[str, Any]:
    """Echo back what a real bridge would return, without evaluating anything."""
    return {
        "success": True,
        "stdout": f"[mock bridge] would execute in Houdini:\n{code}\n",
        "stderr": "",
        "result": None,
        "note": "This is the mock bridge; no code ran in Houdini.",
    }


_API = {
    "houdini.scene_info": scene_info,
    "houdini.node_tree": node_tree,
    "houdini.node_info": node_info,
    "houdini.selected": selected,
    "houdini.geometry": geometry,
    "houdini.errors": errors,
    "houdini.parm": parm,
    "houdini.execute": execute,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[mock bridge] {fmt % args}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.split("?")[0].rstrip("/") != "/api":
            self._send_json({"error": f"Not found: {self.path}"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        fields = urllib.parse.parse_qs(raw)

        if "json" not in fields:
            self._send_json({"error": "Missing 'json' form field"}, 422)
            return

        try:
            name, args, kwargs = json.loads(fields["json"][0])
        except (ValueError, TypeError) as exc:
            self._send_json({"error": f"Malformed json field: {exc}"}, 422)
            return

        handler = _API.get(name)
        if handler is None:
            self._send_json({"error": f"No such API function: {name}"}, 422)
            return

        try:
            self._send_json(handler(*args, **kwargs))
        except APIError as exc:
            self._send_json({"error": str(exc)}, 422)
        except Exception as exc:  # mirrors hwebserver's generic 500
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Houdini bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock Houdini bridge listening on http://{args.host}:{args.port}/api")
    print(f"Functions: {', '.join(sorted(_API))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
