# houdini_mcp_server

my implementation of a mcp server for houdini, this is a companion, context aware bridge for an LLM.

Claude gets read-only context on a live Houdini session — what file is open, what
the node graph looks like, what's selected, what's erroring — plus the ability to
run Python in that session when you ask for it. The point is a consultant that can
see your scene, not a robot that builds it for you.

---

## Current status

**As of 2026-08-10 — feature-complete against the original design. The MCP server
(`server/`), the Houdini bridge (`bridge/`) and the documentation index (`server/docs.py`)
all work: nine resources and six tools, checked by three suites totalling 91 assertions,
all green — `tests/test_roundtrip.py` (27) against a mock with Houdini closed,
`tests/test_docs.py` (39) over 7,890 indexed help pages, `tests/test_live_bridge.py` (25)
against a live session via `hython`. Streamable HTTP is confirmed reachable from Claude
Desktop on Windows localhost and from Claude Code in WSL via the host IP. What remains is
not construction but exercise: the GUI main-thread path has never run, since all
development was headless, and the client configs below have never held a real
conversation.**

The build order in CLAUDE.md called for the bridge first, then the server against a live
session. It ran the other way round: the server was built first against a **mock bridge**
speaking the real wire protocol, which let the whole MCP layer be finished and tested with
Houdini closed. `tests/mock_bridge.py` became the executable specification the real bridge
was then written to satisfy — and both suites assert the same contract, so the mock cannot
quietly drift. The bridge itself never needed a GUI to develop: `hython tests/live_scene.py`
builds a known scene and serves the bridge headlessly.

Overall arc: expose a live Houdini session to Claude as read-only context plus one
deliberate escape hatch (`execute_python`), then add the SideFX reference documentation so
answers are grounded in the manual rather than recalled. Both phases are now built; what
is left is using it and finding out where it is wrong.

- **Environment — complete.** Windows conda env `houdini_mcp`, Python 3.13.14, fastmcp
  3.4.6, httpx 0.28.1. The server runs as a **Windows** process on the same loopback as
  Houdini, which is what allows the bridge to stay bound to `127.0.0.1` rather than opening
  up. WSL and Windows do not share a loopback — this bit everything that tried to test
  across the boundary, and is why the test scripts run Windows-side.

- **MCP server — complete.** `server/main.py` (FastMCP app, streamable HTTP),
  `config.py` (env-overridable hosts/ports), `houdini_client.py`, `resources.py`,
  `tools.py`. Transport is **streamable HTTP, not the SSE** the original design named —
  SSE is deprecated in both the MCP spec and FastMCP. Bridge errors are mapped to readable
  MCP errors, and a refused connection becomes "is Houdini running with the bridge loaded?"
  rather than a transport stack trace.

- **Houdini bridge — complete.** `bridge/houdini_bridge.py` registers eight
  `apiFunction` handlers under the `houdini` namespace. `hwebserver.apiFunction` turned out
  to be **RPC through a single `POST /api`, not the REST routes** the design assumed, so the
  client speaks `json=[name, args, kwargs]`. Handlers dispatch through
  `hdefereval.executeInMainThreadWithResult` — which is GUI-only (it needs `hou.ui`), blocks
  until Houdini's event loop is idle, and deadlocks if called from the main thread; all three
  cases are handled, though **the GUI main-thread path is the one part still unexercised**,
  since development ran headless.

  Three things only the real API could teach. **`cookTime()` does not exist** anywhere in
  HOM — `cookCount()` is the only cook telemetry, and the first draft called a method that
  was never there. **A stock `geo` node has 90 parameters and zero non-default ones**, so
  filtering to non-defaults returned a bare `{}` that read as "this node has no parameters";
  `node_info` now also reports `parms_total` and takes `include_defaults`. And **a camera
  contains real SOPs** — `/obj/cam1` holds `camOrigin`/`file1`/`xform1` building its frustum
  wireframe, so `displayNode()` hands back a genuine `SopNode` with ~336 points. Nothing
  structural separates it from a geometry object; only the object *type* does. Geometry
  resolution is gated on an allowlist, and the refusal names the guide SOP so it can still
  be requested deliberately.

- **Docs index — complete.** `server/docs.py` indexes the help shipped with the installed
  Houdini into SQLite FTS5: 7,890 pages across four namespaces, built in ~2s, cached
  outside the repo. Four `docs://` templates plus `search_docs` and `get_doc`. No scraping,
  nothing vendored.

  Two findings shaped it. **FTS5 ANDs its terms by default**, which made natural questions
  useless — "how do I read a point attribute" matched almost nothing until terms were
  OR-ed and left to bm25 to rank. And **SideFX ships superseded node pages alongside
  current ones**, suffixed with `-`: `sop/scatter-` is the old doc, `sop/scatter` is
  version 2.0. The old page is a third the size, so bm25 ranked it *first* — the precise
  failure this index exists to prevent, since it would have Claude confidently citing
  parameters that no longer exist. Superseded pages are now demoted and flagged.

  A fourth namespace, `docs://nodes/`, was added beyond the three the design named. Search
  surfaces node pages constantly and without it those hits would be unreadable — the
  design's three namespaces cannot cover what its own search returns.

Design notes and the running list of HOM gotchas live in `CLAUDE.md` (untracked — it is
gitignored).

---

## Layout

```
houdini_mcp_server/
├── bridge/
│   ├── houdini_bridge.py   runs inside Houdini; the only file importing hou
│   └── README.md           install routes, API surface, threading notes
├── server/
│   ├── main.py             entry point, FastMCP app, streamable HTTP transport
│   ├── config.py           hosts/ports, overridable by environment variable
│   ├── houdini_client.py   HTTP client speaking hwebserver's RPC convention
│   ├── docs.py             help discovery, FTS5 index, search, page retrieval
│   ├── resources.py        scene:// and docs:// resources
│   └── tools.py            scene tools + search_docs, get_doc
├── tests/
│   ├── mock_bridge.py      stdlib-only fake Houdini; the wire-protocol spec
│   ├── test_roundtrip.py   27 checks against the mock, no Houdini required
│   ├── test_docs.py        39 checks over the shipped help, no session needed
│   ├── live_scene.py       builds a known scene, serves the bridge from hython
│   └── test_live_bridge.py 25 checks against real HOM
└── pyproject.toml
```

The **bridge** is the only component that imports `hou`; everything else talks to it over
localhost HTTP. That split is what lets the entire MCP layer be developed and tested with
Houdini closed.

## Environment

The server runs as a **Windows** process, on the same host and loopback as Houdini.
That's what lets the bridge stay bound to `127.0.0.1`.

```bash
conda create -n houdini_mcp python=3.13 -y
conda activate houdini_mcp
pip install fastmcp httpx
```

Currently pinned in dev to fastmcp 3.4.6 / httpx 0.28.1 on Python 3.13.

## Running

Two processes: something serving the bridge, and the MCP server.

**Against real Houdini** — start the bridge, then the server, then point a client at it.

1. In Houdini (Windows ▸ Python Source Editor, Apply):

   ```python
   exec(open(r"C:\DEV\houdini_mcp_server\scripts\start_bridge.py").read())
   ```

   `exec` rather than a paste so it always runs the current script. Expect
   `[houdini_bridge] starting on http://127.0.0.1:8008/api`.

2. In its own terminal — it runs in the foreground and must stay open:

   ```bash
   ./scripts/start_server.sh      # or scripts\start_server.bat on Windows
   ```

3. Register it with a client, per [Connecting clients](#connecting-clients) below.

The server starts fine without Houdini and only fails when it reaches for the bridge,
so "client connects but every scene query errors" means the bridge is missing, not the
server. The error names the bridge URL, which is how you tell the two apart.

**Against real Houdini, headless** — no GUI required, good for development:

```bash
hython tests/live_scene.py            # builds a known scene, serves on 8009
python tests/test_live_bridge.py      # 25 checks against real HOM
```

**Without Houdini running** — the mock stands in for the session, and the docs suite
needs only the install:

```bash
python tests/mock_bridge.py --port 8008
python -m server.main --port 3000
python tests/test_roundtrip.py        # 27 checks, no Houdini
python tests/test_docs.py             # 39 checks over the shipped help
```

`tests/mock_bridge.py` is the executable specification of the wire protocol; the mock and
live suites assert the same contract, so the mock can't quietly drift from the real bridge.

Configuration, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `HOUDINI_BRIDGE_HOST` | `127.0.0.1` | Where Houdini's `hwebserver` listens |
| `HOUDINI_BRIDGE_PORT` | `8008` | `hwebserver.run()`'s own default |
| `HOUDINI_BRIDGE_TIMEOUT` | `30.0` | Seconds before a bridge call gives up |
| `HOUDINI_MCP_HOST` | `0.0.0.0` | Wide bind so WSL can reach it |
| `HOUDINI_MCP_PORT` | `3000` | MCP endpoint is `http://<host>:3000/mcp` |

## Connecting clients

**Claude Desktop** (Windows, same machine) — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "houdini": {
      "type": "http",
      "url": "http://127.0.0.1:3000/mcp"
    }
  }
}
```

**Claude Code** (WSL) — reaches the Windows host by IP, not localhost:

```bash
claude mcp add --transport http --scope user houdini "http://$(ip route show default | awk '{print $3}'):3000/mcp"
```

That IP (`172.22.192.1` at time of writing) is assigned by WSL and **changes when you
reboot**, so prefer the command substitution over pasting a literal. Enabling mirrored
networking in `.wslconfig` would let WSL use `127.0.0.1` instead and make this stable.
Drop `--scope user` to keep the server to the current project instead of every directory.

MCP servers are read at startup, so **a session already running won't see it** — exit,
add, and relaunch. Then `/mcp` should list `houdini` as connected; if it shows a
connection failure, the server isn't running or the host IP moved.

## What the server exposes

Resources — read-only, safe to poll:

| URI | Contents |
|---|---|
| `scene://info` | hip path, Houdini version, FPS, frame range, current frame |
| `scene://graph` | node tree from `/`, three levels deep |
| `scene://selected` | full detail on the currently selected nodes |
| `scene://errors` | cook errors and warnings across the scene |
| `scene://geometry/{path}` | counts, attributes, groups, bbox — path without the leading slash, e.g. `scene://geometry/obj/geo1/OUT` |
| `docs://hom/{topic}` | HOM Python reference, e.g. `docs://hom/hou/Node` |
| `docs://vex/{topic}` | VEX functions, e.g. `docs://vex/functions/length` |
| `docs://apex/{topic}` | APEX operators and concepts, e.g. `docs://apex/apex/Abs` |
| `docs://nodes/{topic}` | node reference, all contexts, e.g. `docs://nodes/sop/scatter` |

Tools:

| Tool | Notes |
|---|---|
| `get_node_info(path, include_defaults)` | read-only |
| `get_parm_value(node, parm)` | read-only; returns value, raw expression, keyframed/default state |
| `get_node_tree(root, depth)` | read-only; for looking inside a specific subnetwork |
| `search_docs(query, namespace, limit)` | read-only; full-text over the shipped help |
| `get_doc(namespace, topic)` | read-only; reads one help page in full |
| `execute_python(code)` | **mutates your open scene.** Annotated destructive. Save first. |

### Documentation

The docs come from the help **shipped with the installed Houdini**
(`$HFS/houdini/help/*.zip`), so they match the build in use rather than whatever the
model remembers. Nothing is scraped and nothing is vendored into the repo.

7,890 pages are indexed into SQLite FTS5 — 966 HOM, 1,178 VEX, 713 APEX, 5,033 node
pages. The index builds in about 2 seconds on first use and is cached at
`~/.houdini_mcp/docs_index.sqlite` (override with `HOUDINI_DOCS_INDEX`). Pages are read
from the zips on demand, so the 41 MB index is all that ever hits disk. If the schema
changes, a stale index detects it and rebuilds itself.

Houdini's install is auto-detected; set `HOUDINI_HELP_DIR` (or `HFS`) to override, and on
a machine with several versions installed the newest wins.

Two details worth knowing about search. Query terms are **OR-ed rather than AND-ed**,
because FTS5's default AND makes a natural question like "how do I read a point
attribute" match almost nothing; bm25 still ranks pages matching more terms first. And
**superseded node pages are demoted** — SideFX suffixes them with `-`, so `sop/scatter-`
is the old page and `sop/scatter` is version 2.0. The old page is shorter and would
otherwise win on bm25, which is exactly how a model ends up quoting parameters that no
longer exist.

## Note on the bridge protocol

CLAUDE.md sketches REST endpoints (`GET /scene_info`, `GET /node_info?path=…`).
Houdini's `hwebserver.apiFunction` doesn't work that way — it's RPC through a single
endpoint:

```
POST /api
json=["houdini.node_info", [], {"path": "/obj/geo1"}]
```

`houdini_client.py` speaks that convention, so the bridge should register
`apiFunction` handlers named `houdini.scene_info`, `houdini.node_tree`,
`houdini.node_info`, `houdini.selected`, `houdini.geometry`, `houdini.errors`,
`houdini.parm` and `houdini.execute`. `hwebserver.urlHandler` is the alternative if
real REST routes are ever wanted.

Error mapping follows hwebserver: `hwebserver.APIError` → HTTP 422 with a
`{"error": …}` body, anything else → 500. The client turns both into readable
messages, and a refused connection into "is Houdini running with the bridge loaded?".

`tests/mock_bridge.py` is the executable specification of all of this — the real
bridge needs to match it, and if it can't, the mock was wrong.

---

## Build order

The sequence from CLAUDE.md, with steps 1 and 2 swapped in practice — the server was built
first against the mock, and the bridge written afterwards to satisfy it.

| # | Step | Status |
|---|------|--------|
| 1 | Houdini bridge — `hwebserver` handlers, tested with curl | Complete (built third) |
| 2 | MCP server skeleton — fastmcp, one resource, one tool | Complete |
| 3 | Wire them together — confirm round-trip | Complete (mock and real HOM) |
| 4 | Remaining resources and tools | Complete |
| 5 | Docs index — populate and expose `docs://`, `search_docs` | Complete |
| 6 | Client config — Claude Desktop + Claude Code | Drafted above, not yet used in anger |

### What's left, concretely

- **Exercise the GUI main-thread path.** Everything so far ran headless, so
  `hdefereval.executeInMainThreadWithResult` has never actually been used. Load the bridge
  into a graphical session and re-run `python tests/test_live_bridge.py --port 8008`.
- **Use it in anger** from both Claude Desktop and Claude Code, on a real scene rather than
  the five-node fixture, and see which resources are actually worth polling and whether
  `search_docs` returns what a real question needs.
- **Consider a `docs://` search resource** or richer excerpts if search results turn out to
  need more context than the current summary line gives.
- **Open question:** auth on the MCP server (probably overkill for local dev), and whether
  to enable WSL mirrored networking so the host IP stops changing on reboot.
