"""Stdio MCP transport: child in its own process group; SIGTERM then SIGKILL; reap."""

from __future__ import annotations

import asyncio
import os
import signal

from coscientist.mcp.errors import (
    McpConfigError,
    McpResponseLimitError,
    McpTimeoutError,
    McpTransportError,
)


class StdioTransport:
    """Newline-delimited JSON-RPC over a subprocess stdio pair."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        terminate_grace_s: float,
        kill_grace_s: float,
    ) -> None:
        self._proc = proc
        self._terminate_grace_s = terminate_grace_s
        self._kill_grace_s = kill_grace_s
        self._closed = False
        self._buf = bytearray()

    @classmethod
    async def start(
        cls,
        *,
        command: str,
        args: tuple[str, ...],
        env_keys: tuple[str, ...],
        terminate_grace_s: float,
        kill_grace_s: float,
        connect_timeout_s: float,
    ) -> StdioTransport:
        env = _child_env(env_keys)
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    command,
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                ),
                timeout=connect_timeout_s,
            )
        except TimeoutError as exc:
            raise McpTimeoutError("stdio server spawn exceeded connect_timeout_s") from exc
        except OSError as exc:
            raise McpTransportError(f"failed to spawn stdio server: {exc.strerror}", retryable=False) from exc
        return cls(proc, terminate_grace_s=terminate_grace_s, kill_grace_s=kill_grace_s)

    async def send(self, message: bytes) -> None:
        if self._closed or self._proc.stdin is None:
            raise McpTransportError("stdio transport is closed", retryable=False)
        try:
            self._proc.stdin.write(message)
            if not message.endswith(b"\n"):
                self._proc.stdin.write(b"\n")
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise McpTransportError("stdio stdin write failed", retryable=False) from exc

    async def receive(self, *, max_bytes: int, timeout_s: float) -> bytes:
        if self._closed or self._proc.stdout is None:
            raise McpTransportError("stdio transport is closed", retryable=False)
        try:
            return await asyncio.wait_for(
                self._read_line(max_bytes=max_bytes),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            await self.aclose()
            raise McpTimeoutError("stdio receive exceeded configured timeout") from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _reap_process_group(
            self._proc,
            terminate_grace_s=self._terminate_grace_s,
            kill_grace_s=self._kill_grace_s,
        )

    async def _read_line(self, *, max_bytes: int) -> bytes:
        assert self._proc.stdout is not None
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                if len(line) > max_bytes:
                    raise McpResponseLimitError(
                        "stdio response line exceeds max_response_bytes",
                        limit=max_bytes,
                    )
                return line
            if len(self._buf) > max_bytes:
                raise McpResponseLimitError(
                    "stdio response exceeds max_response_bytes before newline",
                    limit=max_bytes,
                )
            chunk = await self._proc.stdout.read(min(65536, max_bytes - len(self._buf) + 1))
            if not chunk:
                if self._buf:
                    raise McpTransportError(
                        "stdio stdout closed before complete message",
                        retryable=False,
                    )
                raise McpTransportError("stdio stdout closed", retryable=False)
            self._buf.extend(chunk)


def _child_env(env_keys: tuple[str, ...]) -> dict[str, str]:
    env: dict[str, str] = {}
    missing: list[str] = []
    for key in env_keys:
        value = os.environ.get(key)
        if value is None:
            missing.append(key)
        else:
            env[key] = value
    if missing:
        # Names only — never values.
        raise McpConfigError("stdio env_keys missing from environment: " + ", ".join(missing))
    return env


async def _reap_process_group(
    proc: asyncio.subprocess.Process,
    *,
    terminate_grace_s: float,
    kill_grace_s: float,
) -> None:
    if proc.returncode is not None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=terminate_grace_s)
        return
    except TimeoutError:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=kill_grace_s)
    except TimeoutError as exc:
        raise McpTimeoutError("stdio server did not exit after SIGKILL within kill_grace_s") from exc


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if proc.pid is None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
