#!/usr/bin/env bash
# Start the Houdini MCP server from WSL.
#
# The server itself still runs Windows-side -- this just invokes the Windows
# interpreter, because Houdini's bridge sits on the Windows loopback and WSL
# cannot reach it. Arguments pass through to server.main.
#
# Prints the host IP Claude Code should connect to, since WSL assigns it and it
# changes on reboot.

set -euo pipefail

PY="${HOUDINI_MCP_PYTHON:-/mnt/c/Users/$USER/.conda/envs/houdini_mcp/python.exe}"
[[ -x "$PY" ]] || PY="/mnt/c/Users/jazzj/.conda/envs/houdini_mcp/python.exe"

if [[ ! -x "$PY" ]]; then
    echo "[start_server] Python not found: $PY" >&2
    echo "[start_server] Set HOUDINI_MCP_PYTHON to the interpreter with fastmcp." >&2
    exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

host_ip="$(ip route show default 2>/dev/null | awk '{print $3; exit}' || true)"
if [[ -n "$host_ip" ]]; then
    echo "[start_server] from WSL, connect Claude Code to http://${host_ip}:3000/mcp"
fi

exec "$PY" -m server.main "$@"
