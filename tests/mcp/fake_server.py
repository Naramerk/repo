"""In-process fake MCP server over a duplex message transport (no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

Handler = Callable[[str, dict[str, Any] | None], dict[str, Any] | None]


class FakeStdioServer:
    """Paired client/server memory transports speaking newline-delimited JSON-RPC."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._to_server: asyncio.Queue[bytes] = asyncio.Queue()
        self._to_client: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.client = _QueueTransport(self._to_server, self._to_client)

    async def __aenter__(self) -> FakeStdioServer:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            raw = await self._to_server.get()
            message = json.loads(raw.decode("utf-8"))
            method = message.get("method")
            if not isinstance(method, str):
                continue
            params = message.get("params")
            if "id" not in message:
                # notification
                self._handler(method, params if isinstance(params, dict) else None)
                continue
            result = self._handler(method, params if isinstance(params, dict) else None)
            if result is None:
                result = {}
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": result,
            }
            await self._to_client.put(json.dumps(reply, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


class _QueueTransport:
    def __init__(
        self,
        outbound: asyncio.Queue[bytes],
        inbound: asyncio.Queue[bytes],
    ) -> None:
        self._outbound = outbound
        self._inbound = inbound
        self._closed = False

    async def send(self, message: bytes) -> None:
        if self._closed:
            raise RuntimeError("closed")
        await self._outbound.put(message)

    async def receive(self, *, max_bytes: int, timeout_s: float) -> bytes:
        if self._closed:
            raise RuntimeError("closed")
        raw = await asyncio.wait_for(self._inbound.get(), timeout=timeout_s)
        if len(raw) > max_bytes:
            raise ValueError("max_bytes exceeded")
        return raw

    async def aclose(self) -> None:
        self._closed = True


def default_handler(method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    if method == "initialize":
        assert params is not None
        return {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.0.1"},
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "echo",
                    "description": "echo args",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ]
        }
    if method == "tools/call":
        assert params is not None
        text = params.get("arguments", {}).get("text", "")
        return {
            "content": [{"type": "text", "text": str(text)}],
            "isError": False,
        }
    raise AssertionError(f"unexpected method {method}")
