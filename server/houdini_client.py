"""HTTP client for the Houdini bridge.

Houdini's ``hwebserver.apiFunction`` is an RPC mechanism, not a REST router:
every handler is reached by POSTing to a single ``/api`` endpoint with a
``json`` form field holding ``[function_name, args, kwargs]``. This module is
the only place that knows that convention.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Config

NAMESPACE = "houdini"


class BridgeError(RuntimeError):
    """The bridge was reached but the call failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BridgeUnavailable(BridgeError):
    """The bridge could not be reached at all."""


class HoudiniClient:
    """Calls apiFunction handlers registered by the bridge inside Houdini."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._config.bridge_timeout,
                # hwebserver only returns JSON-shaped errors when the client
                # says it accepts JSON; otherwise it renders an HTML page.
                headers={"Accept": "application/json, */*"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, function: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke ``houdini.<function>`` in the live Houdini session."""
        client = await self._get_client()
        payload = json.dumps([f"{NAMESPACE}.{function}", args, kwargs])

        try:
            response = await client.post(
                self._config.bridge_api_url, data={"json": payload}
            )
        except httpx.RequestError as exc:
            raise BridgeUnavailable(
                f"Cannot reach the Houdini bridge at {self._config.bridge_api_url}. "
                "Is Houdini running with the bridge loaded? "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        if response.status_code >= 400:
            raise BridgeError(self._extract_error(response), response.status_code)

        try:
            return response.json()
        except ValueError as exc:
            raise BridgeError(
                f"Bridge returned a non-JSON response: {response.text[:500]!r}"
            ) from exc

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        """Pull the message out of hwebserver's ``{"error": ...}`` body."""
        try:
            body = response.json()
        except ValueError:
            detail = response.text[:500]
        else:
            detail = body.get("error", body) if isinstance(body, dict) else body

        if response.status_code == 422:
            # hwebserver maps hwebserver.APIError -> 422, so this is an error the
            # bridge raised deliberately.
            return f"Houdini bridge rejected the call: {detail}"
        return f"Houdini bridge error (HTTP {response.status_code}): {detail}"

    # -- Bridge API surface -------------------------------------------------

    async def scene_info(self) -> Any:
        return await self.call("scene_info")

    async def node_tree(self, root: str = "/", depth: int = 3) -> Any:
        return await self.call("node_tree", root=root, depth=depth)

    async def node_info(self, path: str, include_defaults: bool = False) -> Any:
        return await self.call(
            "node_info", path=path, include_defaults=include_defaults
        )

    async def selected(self) -> Any:
        return await self.call("selected")

    async def geometry(self, path: str) -> Any:
        return await self.call("geometry", path=path)

    async def errors(self) -> Any:
        return await self.call("errors")

    async def parm(self, path: str, name: str) -> Any:
        return await self.call("parm", path=path, name=name)

    async def execute(self, code: str) -> Any:
        return await self.call("execute", code=code)
