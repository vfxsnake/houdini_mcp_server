"""Houdini-side bridge for the MCP server.

Runs *inside* Houdini's Python environment and is the only component that
imports ``hou``. Registers ``hwebserver.apiFunction`` handlers under the
``houdini`` namespace, matching the wire protocol in tests/mock_bridge.py.

Load it from Houdini's Python Source Editor, a shelf tool, or a startup script
(see bridge/README.md), then:

    import houdini_bridge
    houdini_bridge.start()
"""

from __future__ import annotations

import ast
import contextlib
import functools
import io
import threading
import traceback
from typing import Any, Callable

import hou
import hwebserver

try:
    import hdefereval
except ImportError:  # non-graphical session
    hdefereval = None

DEFAULT_PORT = 8008
NAMESPACE = "houdini"

# A scene walk should never wedge the server on a pathological file.
MAX_NODES_SCANNED = 20000


# --- Main-thread dispatch -------------------------------------------------
#
# hwebserver calls handlers on worker threads, but HOM is only safe on the main
# thread. hdefereval bridges that -- at the cost of blocking until Houdini's
# event loop goes idle, which is why the client uses a generous timeout.


def _is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _run_on_main_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Execute func where HOM is safe, whatever session type we're in."""
    # hdefereval needs hou.ui.addEventLoopCallback, so it only exists with a UI.
    # In hython there are no worker-thread HOM concerns to solve anyway.
    if hdefereval is None or not hou.isUIAvailable():
        return func(*args, **kwargs)

    # Calling executeInMainThreadWithResult *from* the main thread waits on a
    # queue only the main thread can drain: guaranteed deadlock.
    if _is_main_thread():
        return func(*args, **kwargs)

    # Bind the arguments rather than forwarding them: the dispatcher's own
    # signature is executeInMainThreadWithResult(code, *args, **kwargs), so a
    # handler kwarg named "code" -- houdini.execute has one -- would collide
    # with its first parameter and raise TypeError before ever running.
    return hdefereval.executeInMainThreadWithResult(
        functools.partial(func, *args, **kwargs))


def houdini_api(func: Callable[..., Any]) -> Callable[..., Any]:
    """Register func as houdini.<name>, running its body on the main thread."""

    @functools.wraps(func)
    def wrapper(request: Any, **kwargs: Any) -> Any:
        try:
            return _run_on_main_thread(func, **kwargs)
        except hwebserver.APIError:
            raise
        except hou.ObjectWasDeleted as exc:
            raise hwebserver.APIError(f"Node was deleted while reading it: {exc}")
        except hou.OperationFailed as exc:
            raise hwebserver.APIError(f"Houdini operation failed: {exc}")
        except Exception as exc:
            # Surface the real reason rather than a bare 500.
            raise hwebserver.APIError(f"{type(exc).__name__}: {exc}")

    return hwebserver.apiFunction(namespace=NAMESPACE)(wrapper)


# --- Helpers --------------------------------------------------------------


def _require_node(path: str) -> hou.Node:
    node = hou.node(path)
    if node is None:
        raise hwebserver.APIError(f"Node not found: {path}")
    return node


def _parm_value(parm: hou.Parm) -> Any:
    """Read a parm without letting one bad expression sink the whole response."""
    try:
        value = parm.eval()
    except Exception as exc:
        return f"<error evaluating: {exc}>"

    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    return str(value)


def _node_parms(node: hou.Node, include_defaults: bool = False) -> dict[str, Any]:
    """Non-default parms by default -- a /obj node has hundreds otherwise."""
    parms: dict[str, Any] = {}
    for parm in node.parms():
        try:
            if not include_defaults and parm.isAtDefault():
                continue
        except Exception:
            pass
        parms[parm.name()] = _parm_value(parm)
    return parms


def _node_flags(node: hou.Node) -> dict[str, bool]:
    """Flags vary by node type, so probe rather than assume."""
    flags: dict[str, bool] = {}
    for label, getter in (
        ("display", "isDisplayFlagSet"),
        ("render", "isRenderFlagSet"),
        ("bypass", "isBypassed"),
        ("template", "isTemplateFlagSet"),
        ("selectable", "isSelectableTemplateFlagSet"),
    ):
        method = getattr(node, getter, None)
        if method is None:
            continue
        try:
            flags[label] = bool(method())
        except Exception:
            continue
    return flags


def _cook_state(node: hou.Node) -> dict[str, Any]:
    state: dict[str, Any] = {}
    try:
        state["errors"] = len(node.errors())
        state["warnings"] = len(node.warnings())
    except Exception:
        state["errors"] = 0
        state["warnings"] = 0
    # HOM has no per-node cook *time*; cookCount is the only cook telemetry.
    try:
        state["cook_count"] = node.cookCount()
        state["cooked"] = node.cookCount() > 0
    except Exception:
        pass
    return state


def _describe_node(node: hou.Node, include_defaults: bool = False) -> dict[str, Any]:
    parms = _node_parms(node, include_defaults)
    total = len(node.parms()) if hasattr(node, "parms") else 0
    return {
        "path": node.path(),
        "name": node.name(),
        "type": node.type().name(),
        "category": node.type().category().name(),
        "parms": parms,
        # A fresh node can have 90 parms and zero non-default ones, so say so
        # explicitly rather than returning a bare {} that reads as "no parms".
        "parms_shown": "all" if include_defaults else "non-default only",
        "parms_total": total,
        "inputs": [n.path() for n in node.inputs() if n is not None],
        "outputs": [n.path() for n in node.outputs()],
        "children": [c.name() for c in node.children()],
        "flags": _node_flags(node),
        "cook_state": _cook_state(node),
    }


# Object types whose display SOP is real geometry rather than viewport guides.
# A camera is the cautionary case: /obj/cam1 contains genuine SOPs (camOrigin,
# file1, xform1) that build its frustum wireframe, so displayNode() hands back
# a real SopNode with ~336 points. Structurally it is indistinguishable from a
# geometry object; only the object type tells them apart.
GEOMETRY_OBJECT_TYPES = frozenset(
    {"geo", "subnet", "instance", "dopnet", "sopsolver"}
)


def _resolve_geometry(node: hou.Node) -> tuple[hou.Geometry, hou.Node]:
    """Resolve a node to real SOP geometry, and say which SOP it came from."""
    source: hou.Node | None = None
    if isinstance(node, hou.SopNode):
        source = node
    elif isinstance(node, hou.ObjNode):
        display = node.displayNode()
        if isinstance(display, hou.SopNode):
            if node.type().name() in GEOMETRY_OBJECT_TYPES:
                source = display
            else:
                # Don't pass guide geometry off as the object's own.
                raise hwebserver.APIError(
                    f"{node.path()} is a '{node.type().name()}' object, not a "
                    f"geometry container -- its display node "
                    f"({display.path()}) produces viewport guide geometry. "
                    f"Request that SOP directly if you really want it."
                )

    if source is None:
        raise hwebserver.APIError(
            f"Node has no cooked geometry: {node.path()} "
            f"(type {node.type().name()}). Point this at a SOP, or at an "
            f"object containing one."
        )

    try:
        geo = source.geometry()
    except Exception as exc:
        raise hwebserver.APIError(f"Could not read geometry from {source.path()}: {exc}")

    if geo is None:
        raise hwebserver.APIError(f"Node has not cooked any geometry: {source.path()}")
    return geo, source


def _describe_attribs(attribs: Any) -> list[dict[str, Any]]:
    described = []
    for attrib in attribs:
        entry = {"name": attrib.name()}
        try:
            entry["type"] = attrib.dataType().name().lower()
        except Exception:
            entry["type"] = "unknown"
        try:
            entry["size"] = attrib.size()
        except Exception:
            pass
        described.append(entry)
    return described


# --- API handlers ---------------------------------------------------------


@houdini_api
def scene_info() -> dict[str, Any]:
    frame_range = hou.playbar.frameRange()
    return {
        "hip_file": hou.hipFile.path(),
        "hip_name": hou.hipFile.basename(),
        "houdini_version": hou.applicationVersionString(),
        "fps": hou.fps(),
        "frame_range": [int(frame_range[0]), int(frame_range[1])],
        "current_frame": hou.frame(),
        "is_saved": not hou.hipFile.hasUnsavedChanges(),
        "job": hou.getenv("JOB") or "",
    }


@houdini_api
def node_tree(root: str = "/", depth: int = 3) -> dict[str, Any]:
    root_node = _require_node(root)

    def build(node: hou.Node, level: int) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path": node.path(),
            "name": node.name() or "/",
            "type": node.type().name(),
        }
        children = node.children()
        if not children:
            return entry
        if level < depth:
            entry["children"] = [build(c, level + 1) for c in children]
        else:
            entry["children_truncated"] = len(children)
        return entry

    return build(root_node, 0)


@houdini_api
def node_info(path: str, include_defaults: bool = False) -> dict[str, Any]:
    return _describe_node(_require_node(path), include_defaults)


@houdini_api
def selected() -> dict[str, Any]:
    nodes = hou.selectedNodes()
    return {"count": len(nodes), "nodes": [_describe_node(n) for n in nodes]}


@houdini_api
def geometry(path: str) -> dict[str, Any]:
    node = _require_node(path)
    geo, source = _resolve_geometry(node)
    bbox = geo.boundingBox()
    return {
        "path": node.path(),
        # Asking an object for geometry resolves through its display flag, so
        # be explicit about which SOP actually produced these numbers.
        "geometry_from": source.path(),
        "counts": {
            "points": len(geo.iterPoints()),
            "prims": len(geo.iterPrims()),
            "vertices": geo.intrinsicValue("vertexcount"),
        },
        "bounding_box": {
            "min": list(bbox.minvec()),
            "max": list(bbox.maxvec()),
        },
        "attributes": {
            "point": _describe_attribs(geo.pointAttribs()),
            "prim": _describe_attribs(geo.primAttribs()),
            "vertex": _describe_attribs(geo.vertexAttribs()),
            "detail": _describe_attribs(geo.globalAttribs()),
        },
        "groups": {
            "point": [g.name() for g in geo.pointGroups()],
            "prim": [g.name() for g in geo.primGroups()],
            "edge": [g.name() for g in geo.edgeGroups()],
            "vertex": [g.name() for g in geo.vertexGroups()],
        },
    }


@houdini_api
def errors(root: str = "/") -> dict[str, Any]:
    root_node = _require_node(root)
    collected_errors: list[dict[str, str]] = []
    collected_warnings: list[dict[str, str]] = []
    scanned = 0
    truncated = False

    for node in root_node.allSubChildren(top_down=True, recurse_in_locked_nodes=False):
        scanned += 1
        if scanned > MAX_NODES_SCANNED:
            truncated = True
            break
        try:
            node_errors = node.errors()
            node_warnings = node.warnings()
        except Exception:
            continue
        for message in node_errors:
            collected_errors.append(
                {"path": node.path(), "severity": "error", "message": message}
            )
        for message in node_warnings:
            collected_warnings.append(
                {"path": node.path(), "severity": "warning", "message": message}
            )

    result: dict[str, Any] = {
        "errors": collected_errors,
        "warnings": collected_warnings,
    }
    if truncated:
        result["truncated"] = f"stopped after scanning {MAX_NODES_SCANNED} nodes"
    return result


@houdini_api
def parm(path: str, name: str) -> dict[str, Any]:
    node = _require_node(path)
    target = node.parm(name) or node.parmTuple(name)
    if target is None:
        available = sorted(p.name() for p in node.parms())
        raise hwebserver.APIError(
            f"Parameter '{name}' not found on {path}. "
            f"Available: {available[:40]}"
            + (f" (+{len(available) - 40} more)" if len(available) > 40 else "")
        )

    if isinstance(target, hou.ParmTuple):
        value = [_parm_value(p) for p in target]
        raw = [p.rawValue() for p in target]
        is_default = all(p.isAtDefault() for p in target)
        keyframed = any(len(p.keyframes()) > 0 for p in target)
    else:
        value = _parm_value(target)
        raw = target.rawValue()
        is_default = target.isAtDefault()
        keyframed = len(target.keyframes()) > 0

    return {
        "node": node.path(),
        "parm": name,
        "value": value,
        "raw": raw,
        "is_default": is_default,
        "is_keyframed": keyframed,
        "type": type(value).__name__,
    }


def _execute_code(code: str) -> dict[str, Any]:
    """Exec the body, then eval a trailing expression so the user sees its value."""
    stdout, stderr = io.StringIO(), io.StringIO()
    namespace: dict[str, Any] = {"hou": hou, "__name__": "__houdini_mcp__"}
    result: Any = None

    try:
        parsed = ast.parse(code)
    except SyntaxError as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"SyntaxError: {exc}",
            "result": None,
        }

    tail = None
    if parsed.body and isinstance(parsed.body[-1], ast.Expr):
        tail = ast.Expression(parsed.body.pop().value)

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(parsed, "<mcp>", "exec"), namespace)
            if tail is not None:
                result = eval(compile(tail, "<mcp>", "eval"), namespace)
    except Exception:
        return {
            "success": False,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue() + traceback.format_exc(),
            "result": None,
        }

    if not isinstance(result, (int, float, str, bool, list, dict, type(None))):
        result = repr(result)

    return {
        "success": True,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "result": result,
    }


@houdini_api
def execute(code: str) -> dict[str, Any]:
    return _execute_code(code)


# --- Lifecycle ------------------------------------------------------------


def start(port: int = DEFAULT_PORT, debug: bool = False) -> None:
    """Start the bridge web server.

    In a graphical session this returns immediately, leaving the server running
    in a background thread. In hython it blocks until interrupted.
    """
    in_background = hou.isUIAvailable()
    print(f"[houdini_bridge] starting on http://127.0.0.1:{port}/api")
    print(f"[houdini_bridge] registered: {NAMESPACE}.<scene_info|node_tree|"
          f"node_info|selected|geometry|errors|parm|execute>")
    hwebserver.run(port=port, debug=debug, in_background=in_background)


def stop() -> None:
    hwebserver.requestShutdown()
    print("[houdini_bridge] shutdown requested")


if __name__ == "__main__":
    start()
