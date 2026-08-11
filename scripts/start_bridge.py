"""Start the Houdini bridge from inside Houdini.

Paste into the **Python Source Editor** (Windows > Python Source Editor) and
Apply, or point a shelf tool at it. Not the Python Shell -- it is a REPL and
interleaves multi-line pastes into nonsense.

Safe to run twice: it checks whether the port is already serving and says so
instead of raising, which matters because hwebserver cannot be restarted within
a session (see bridge/README.md).
"""

import os
import socket
import sys

REPO = os.environ.get("HOUDINI_MCP_REPO", r"C:\DEV\houdini_mcp_server")
PORT = int(os.environ.get("HOUDINI_BRIDGE_PORT", "8008"))


def _port_is_serving(port: int) -> bool:
    """True if something already holds the port.

    Note this cannot distinguish a healthy bridge from the leaked socket left
    behind by a previous stop() -- both accept connections. If the bridge is
    unresponsive despite this reporting True, restart Houdini.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    bridge_dir = os.path.join(REPO, "bridge")
    if not os.path.isdir(bridge_dir):
        raise RuntimeError(
            f"No bridge/ directory at {bridge_dir!r}. Set HOUDINI_MCP_REPO or edit REPO."
        )
    if bridge_dir not in sys.path:
        sys.path.append(bridge_dir)

    if _port_is_serving(PORT):
        print(f"[start_bridge] port {PORT} is already in use -- not starting a second one.")
        print("[start_bridge] if the bridge does not answer, restart Houdini:")
        print("[start_bridge] a stopped hwebserver leaks its socket and cannot rebind.")
        return

    import houdini_bridge

    houdini_bridge.start(port=PORT)
    print(f"[start_bridge] loaded from {bridge_dir}")
    print("[start_bridge] to pick up code edits, restart Houdini -- do not use "
          "stop()/reload()/start().")


main()
