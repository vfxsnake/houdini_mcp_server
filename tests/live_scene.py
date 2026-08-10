"""Build a known scene and serve the real bridge from hython -- no GUI needed.

    hython tests/live_scene.py [--port 8009]

Pairs with tests/test_live_bridge.py, which asserts against exactly this scene.
Runs headless, so hdefereval is bypassed and handlers execute directly; a
graphical session exercises the main-thread path instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import hou  # noqa: E402


def build_scene() -> None:
    obj = hou.node("/obj")

    geo = obj.createNode("geo", "geo1")
    geo.parm("ty").set(1.5)  # one non-default parm to find

    box = geo.createNode("box")
    scatter = geo.createNode("scatter", "scatter1")
    scatter.setInput(0, box)
    try:
        scatter.parm("npts").set(500)
    except Exception:
        pass

    out = geo.createNode("null", "OUT")
    out.setInput(0, scatter)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)

    obj.createNode("cam", "cam1")  # its frustum guide SOPs are the tricky case

    rop = hou.node("/out").createNode("ifd", "mantra1")
    rop.parm("camera").set("/obj/cam_missing")

    geo.setSelected(True, clear_all_selected=True)
    out.cook(force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the bridge from hython")
    parser.add_argument("--port", type=int, default=8009)
    args = parser.parse_args()

    build_scene()

    import houdini_bridge

    print(f"[live_scene] UI available: {hou.isUIAvailable()}")
    houdini_bridge.start(port=args.port)


if __name__ == "__main__":
    main()
