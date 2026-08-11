# Houdini bridge

`houdini_bridge.py` runs **inside** Houdini and is the only file that imports
`hou`. It publishes the live session over `hwebserver` on localhost, which the
MCP server then consumes.

## Quick check, no GUI needed

The bridge works headless, which is the fastest way to confirm it's healthy:

```bash
hython tests/live_scene.py          # builds a known scene, serves on 8009
python tests/test_live_bridge.py    # 24 checks, in another shell
```

## Installing into a graphical session

Houdini has to be able to import the module, so put `bridge/` on the Python
path. Pick whichever suits you:

**A. `scripts/start_bridge.py`** — the usual way. Paste it into the Python
Source Editor and Apply, or point a shelf tool at it. It puts `bridge/` on the
path, starts on 8008, and is safe to run twice: it probes the port first and
reports rather than raising. `HOUDINI_MCP_REPO` and `HOUDINI_BRIDGE_PORT`
override the defaults.

**B. By hand**, if you want to see the moving parts:

```python
import sys
sys.path.append(r"C:\DEV\houdini_mcp_server\bridge")
import houdini_bridge
houdini_bridge.start()          # serves on 8008
```

Use the **Python Source Editor**, not the Python Shell: the Shell is a REPL and
interleaves multi-line pastes into nonsense. Apply the buffer exactly once — a
second Apply raises on the taken port (which `start_bridge.py` handles for you).

**To pick up edits to the bridge, restart Houdini.** Do *not* use
`stop()` → `importlib.reload()` → `start()`; see the restart note below.

**C. Start with Houdini** — a package file at
`%USERPROFILE%\Documents\houdini22.0\packages\houdini_mcp.json`:

```json
{
  "env": [
    { "PYTHONPATH": "C:/DEV/houdini_mcp_server/bridge" }
  ]
}
```

then `%USERPROFILE%\Documents\houdini22.0\scripts\456.py`:

```python
import houdini_bridge
houdini_bridge.start()
```

`456.py` runs on every scene load, and `hwebserver.run()` raises
`OperationFailed` if the port is taken, so guard it if you use this route:

```python
try:
    houdini_bridge.start()
except Exception as exc:
    print(f"[houdini_bridge] not started: {exc}")
```

## API surface

RPC, not REST. Everything goes to one endpoint:

```
POST http://127.0.0.1:8008/api
json=["houdini.node_info", [], {"path": "/obj/geo1"}]
```

| Function | Arguments |
|---|---|
| `houdini.scene_info` | — |
| `houdini.node_tree` | `root="/"`, `depth=3` |
| `houdini.node_info` | `path`, `include_defaults=False` |
| `houdini.selected` | — |
| `houdini.geometry` | `path` |
| `houdini.errors` | `root="/"` |
| `houdini.parm` | `path`, `name` |
| `houdini.execute` | `code` |

Errors follow hwebserver: `APIError` → 422 with `{"error": …}`, anything else
→ 500. The bridge converts unexpected exceptions into `APIError` so the client
gets the real reason instead of a bare 500.

Test it by hand:

```bash
curl -X POST http://127.0.0.1:8008/api -H 'Accept: application/json' \
  --data-urlencode 'json=["houdini.scene_info", [], {}]'
```

## Things worth knowing

**Threading.** `hwebserver` calls handlers on worker threads, but HOM is only
safe on the main thread, so handlers are dispatched through
`hdefereval.executeInMainThreadWithResult`. That has three consequences:

- It needs `hou.ui`, so it's GUI-only. Headless sessions call handlers directly,
  which is safe because there's no separate main thread to protect.
- It blocks until Houdini's event loop is idle. **A request made while Houdini is
  mid-cook will wait for that cook to finish** — hence the client's 30s timeout.
- Calling it *from* the main thread would deadlock, so the bridge checks and
  sidesteps that.

**The server cannot be restarted in the same session.** `stop()` calls
`hwebserver.requestShutdown()`, which tears down the server and its worker
threads but **leaves the listening socket bound to the process**. The port then
looks alive to `netstat` while nothing services it: connections are accepted and
never answered, so clients hang until they time out rather than failing fast.
`start()` cannot rebind that port afterwards. Diagnose it with
`threading.active_count()` in the Python Shell — a healthy background server
shows several threads, a wedged one shows `1`. Recovery is restarting Houdini;
a *different* port works within the session, since only the original is leaked.

**The bind address is not configurable.** `hwebserver.run()` takes no host or
interface argument, so the bridge listens on `0.0.0.0`, not loopback — despite
what the startup message prints. Since `execute` runs unsandboxed Python in the
live session, that is a real exposure on any untrusted network.
`request.clientAddress` is available if a loopback check is wanted; otherwise
block the port at the firewall.

**Cameras aren't geometry.** `/obj/cam1` contains genuine SOPs (`camOrigin`,
`file1`, `xform1`) that build its frustum wireframe, so `displayNode()` returns
a real `SopNode` with ~336 points. The bridge refuses those on the basis of the
object *type*, since nothing structural distinguishes them from a geometry
object. `GEOMETRY_OBJECT_TYPES` is the allowlist — extend it if you have a
custom object HDA that genuinely contains geometry.

**Parameters are filtered by default.** A stock `geo` node has 90 parameters and
typically zero non-default ones, so `node_info` returns only what's been changed,
plus `parms_total`. Pass `include_defaults=True` for the lot.

**Errors need a cook.** `scene://errors` reports what nodes recorded when they
last cooked. A ROP with a bad camera path shows nothing until something tries to
cook it — an empty result means "nothing has failed yet", not "nothing is wrong".

**`execute` is real.** It runs in the artist's live session with `hou` already
imported, and returns stdout, stderr and the value of a trailing expression.
Nothing is sandboxed. Save first.
