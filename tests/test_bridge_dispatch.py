"""Exercise the bridge's GUI-only code paths without a GUI.

The other suites cannot reach these. tests/test_roundtrip.py talks to a mock
over HTTP and never loads the bridge at all; tests/test_live_bridge.py loads it
under hython, where `hou.isUIAvailable()` is False and `_run_on_main_thread`
returns before `hdefereval` is ever touched. That blind spot is exactly where
the `code`-collision bug lived, undetected by 91 green checks, until a real
graphical session hit it.

So: stub `hou`, `hwebserver` and `hdefereval` in sys.modules and import the
bridge against them. The stub for `executeInMainThreadWithResult` deliberately
names its first parameter `code`, as the real one does -- that name *is* the
bug, and a stub that renamed it would test nothing.

Stdlib only, no Houdini, no conda env:

    python3 tests/test_bridge_dispatch.py
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --- Stubs ----------------------------------------------------------------


class _APIError(Exception):
    """Stands in for hwebserver.APIError (422 to the client)."""


class _StubRequest:
    """What hwebserver hands a handler. `address=None` models a build that
    doesn't expose clientAddress at all."""

    def __init__(self, address: str | None = "127.0.0.1", port: int = 51000):
        self._address = address
        self._port = port

    def clientAddress(self):  # noqa: N802 -- matches the C++ API
        if self._address is None:
            raise AttributeError("clientAddress")
        return (self._address, self._port)


class _NoAddressRequest:
    """An older build with no clientAddress method."""


def _install_stubs() -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    hou = types.ModuleType("hou")
    hou.ObjectWasDeleted = type("ObjectWasDeleted", (Exception,), {})
    hou.OperationFailed = type("OperationFailed", (Exception,), {})
    hou.isUIAvailable = lambda: True

    hwebserver = types.ModuleType("hwebserver")
    hwebserver.APIError = _APIError
    hwebserver.apiFunction = lambda namespace=None: (lambda fn: fn)
    hwebserver.run_calls = []
    hwebserver.settings_calls = []
    hwebserver.run = lambda **kw: hwebserver.run_calls.append(kw)
    hwebserver.setSettingsForPort = (
        lambda settings, port_name: hwebserver.settings_calls.append((settings, port_name))
    )
    hwebserver.requestShutdown = lambda: None

    hdefereval = types.ModuleType("hdefereval")
    hdefereval.calls = []

    def executeInMainThreadWithResult(code, *args, **kwargs):  # noqa: N802
        # The real signature, parameter name and all. Forwarding a handler
        # kwarg named "code" through this is what used to raise TypeError.
        hdefereval.calls.append(code)
        return code(*args, **kwargs)

    hdefereval.executeInMainThreadWithResult = executeInMainThreadWithResult

    sys.modules["hou"] = hou
    sys.modules["hwebserver"] = hwebserver
    sys.modules["hdefereval"] = hdefereval
    return hou, hwebserver, hdefereval


# --- Checks ---------------------------------------------------------------


def main() -> int:
    hou, hwebserver, hdefereval = _install_stubs()
    sys.path.insert(0, str(ROOT / "bridge"))
    import houdini_bridge as bridge  # noqa: E402

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}  {detail}")

    def on_worker_thread(fn, *args, **kwargs):
        """Call fn off the main thread, the way hwebserver really does."""
        box: dict[str, object] = {}

        def run():
            try:
                box["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 -- re-raised below
                box["error"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=10)
        if worker.is_alive():
            raise AssertionError("handler never returned -- deadlock?")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["value"]

    print("\n== Main-thread dispatch ==")

    hdefereval.calls.clear()
    # The regression itself: executeInMainThreadWithResult(code, *args, **kwargs)
    # collides with houdini.execute(code=...) unless the args are bound first.
    # Caught here rather than allowed to abort, so one break doesn't hide the rest.
    try:
        result = on_worker_thread(
            bridge.execute, _StubRequest(), code="value = 6 * 7\nvalue")
    except _APIError as exc:
        result = {"success": False, "result": None, "error": str(exc)}
    check("execute(code=...) survives the parameter-name collision",
          result["success"] is True, str(result))
    check("the executed code actually ran", result["result"] == 42, str(result["result"]))
    check("worker-thread request goes through hdefereval", len(hdefereval.calls) == 1,
          f"{len(hdefereval.calls)} dispatches")
    check("the dispatcher gets one bound callable, no loose kwargs",
          bool(hdefereval.calls) and callable(hdefereval.calls[0]))

    hdefereval.calls.clear()
    direct = bridge.execute(_StubRequest(), code="1 + 1")
    check("a main-thread call bypasses the dispatcher instead of deadlocking",
          not hdefereval.calls and direct["result"] == 2, str(direct))

    hou.isUIAvailable = lambda: False
    hdefereval.calls.clear()
    headless = on_worker_thread(bridge.execute, _StubRequest(), code="'hython'")
    check("no UI means no hdefereval, even on a worker thread",
          not hdefereval.calls and headless["result"] == "hython", str(headless))
    hou.isUIAvailable = lambda: True

    saved, bridge.hdefereval = bridge.hdefereval, None
    missing = on_worker_thread(bridge.execute, _StubRequest(), code="'no module'")
    check("an absent hdefereval module falls through to a direct call",
          missing["result"] == "no module", str(missing))
    bridge.hdefereval = saved

    print("\n== Error translation ==")

    def raiser(exc):
        return lambda **kw: (_ for _ in ()).throw(exc)

    wrapped = bridge.houdini_api(raiser(hou.ObjectWasDeleted("gone")))
    try:
        on_worker_thread(wrapped, _StubRequest())
        check("a deleted node becomes an APIError", False, "no exception raised")
    except _APIError as exc:
        check("a deleted node becomes an APIError", "deleted while reading" in str(exc),
              str(exc))

    wrapped = bridge.houdini_api(raiser(ValueError("nope")))
    try:
        on_worker_thread(wrapped, _StubRequest())
        check("an unexpected error keeps its type name", False, "no exception raised")
    except _APIError as exc:
        check("an unexpected error keeps its type name", "ValueError: nope" in str(exc),
              str(exc))

    print("\n== Loopback-only access ==")

    bridge._loopback_only = True
    ok = bridge.execute(_StubRequest("127.0.0.1"), code="'served'")
    check("127.0.0.1 is served", ok["result"] == "served", str(ok))

    ok = bridge.execute(_StubRequest("::1"), code="'served'")
    check("::1 is served", ok["result"] == "served", str(ok))

    try:
        bridge.execute(_StubRequest("10.0.0.5"), code="'pwned'")
        check("an off-box caller is refused", False, "the request was served")
    except _APIError as exc:
        check("an off-box caller is refused", "loopback only" in str(exc), str(exc))
        check("the refusal names the caller", "10.0.0.5" in str(exc), str(exc))

    served = bridge.execute(_NoAddressRequest(), code="'unknown'")
    check("an unreadable address is served, not refused",
          served["result"] == "unknown", str(served))

    bridge._loopback_only = False
    opened = bridge.execute(_StubRequest("10.0.0.5"), code="'invited'")
    check("an explicitly opened bridge serves remote callers",
          opened["result"] == "invited", str(opened))
    bridge._loopback_only = True

    print("\n== Bind address ==")

    hou.isUIAvailable = lambda: True
    hwebserver.settings_calls.clear()
    hwebserver.run_calls.clear()
    bridge.start(port=8008)
    settings, port_name = hwebserver.settings_calls[0]
    check("start() binds loopback by default", settings["ADDRESS"] == "127.0.0.1",
          str(settings))
    check("the settings carry the port too", settings["PORT"] == 8008, str(settings))
    check("the default port's name is the empty string", port_name == "", repr(port_name))
    check("run() is still handed the port", hwebserver.run_calls[0]["port"] == 8008,
          str(hwebserver.run_calls[0]))
    check("a graphical session runs in the background",
          hwebserver.run_calls[0]["in_background"] is True, str(hwebserver.run_calls[0]))
    check("start() leaves the loopback guard armed", bridge._loopback_only is True)

    hwebserver.settings_calls.clear()
    bridge.start(port=8008, address="0.0.0.0")
    check("an explicit address is passed through",
          hwebserver.settings_calls[0][0]["ADDRESS"] == "0.0.0.0",
          str(hwebserver.settings_calls[0][0]))
    check("opening the address disarms the guard", bridge._loopback_only is False)

    # An hwebserver too old for per-port settings must still start, with the
    # handler check as the only defence -- and must say so.
    del hwebserver.setSettingsForPort
    hwebserver.run_calls.clear()
    bridge.start(port=8008)
    check("a build without setSettingsForPort still starts",
          len(hwebserver.run_calls) == 1, str(hwebserver.run_calls))
    check("...and re-arms the loopback guard", bridge._loopback_only is True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("All dispatch checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
