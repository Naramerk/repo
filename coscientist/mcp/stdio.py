"""Stdio MCP transport: child in its own process group; SIGTERM then SIGKILL; reap."""

from __future__ import annotations

import asyncio
import os
import signal

from coscientist.mcp.errors import (
    McpConfigError,
    McpProtocolError,
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
        process_group_poll_interval_s: float,
        read_chunk_bytes: int,
        max_stderr_bytes: int,
        pgid: int,
    ) -> None:
        self._proc = proc
        self._terminate_grace_s = terminate_grace_s
        self._kill_grace_s = kill_grace_s
        self._process_group_poll_interval_s = process_group_poll_interval_s
        self._read_chunk_bytes = read_chunk_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._pgid = pgid
        self._closed = False
        self._buf = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_overflow = False
        if proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def start(
        cls,
        *,
        command: str,
        args: tuple[str, ...],
        env_keys: tuple[str, ...],
        terminate_grace_s: float,
        kill_grace_s: float,
        process_group_poll_interval_s: float,
        read_chunk_bytes: int,
        max_stderr_bytes: int,
    ) -> StdioTransport:
        env = _child_env(env_keys)
        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise McpTransportError(
                f"failed to spawn stdio server: {exc.strerror}",
                retryable=False,
            ) from exc
        if proc.pid is None:
            raise McpTransportError("stdio server spawned without pid", retryable=False)
        try:
            pgid = os.getpgid(proc.pid)
        except OSError as exc:
            proc.kill()
            await proc.wait()
            raise McpTransportError("failed to read process group id", retryable=False) from exc
        return cls(
            proc,
            terminate_grace_s=terminate_grace_s,
            kill_grace_s=kill_grace_s,
            process_group_poll_interval_s=process_group_poll_interval_s,
            read_chunk_bytes=read_chunk_bytes,
            max_stderr_bytes=max_stderr_bytes,
            pgid=pgid,
        )

    async def send(self, message: bytes) -> None:
        if self._closed or self._proc.stdin is None:
            raise McpTransportError("stdio transport is closed", retryable=False)
        if self._stderr_overflow:
            await self.aclose()
            raise McpResponseLimitError(
                "stdio stderr exceeded max_stderr_bytes",
                limit=self._max_stderr_bytes,
            )
        if b"\n" in message:
            raise McpProtocolError("stdio JSON-RPC message must not contain embedded newlines")
        try:
            self._proc.stdin.write(message)
            self._proc.stdin.write(b"\n")
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise McpTransportError("stdio stdin write failed", retryable=False) from exc

    async def receive(self, *, max_bytes: int, timeout_s: float) -> bytes:
        if self._closed or self._proc.stdout is None:
            raise McpTransportError("stdio transport is closed", retryable=False)
        if self._stderr_overflow:
            await self.aclose()
            raise McpResponseLimitError(
                "stdio stderr exceeded max_stderr_bytes",
                limit=self._max_stderr_bytes,
            )
        try:
            return await asyncio.wait_for(
                self._read_line(max_bytes=max_bytes),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            raise McpTimeoutError("stdio receive exceeded configured timeout") from exc
        except (McpResponseLimitError, McpProtocolError, McpTransportError):
            self.reset_receive_buffer()
            await self.aclose()
            raise

    def reset_receive_buffer(self) -> None:
        self._buf.clear()

    async def join_outbound(self) -> None:
        """Stdio writes are drained in ``send``; nothing further to wait for."""
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _reap_process_group(
            self._proc,
            pgid=self._pgid,
            terminate_grace_s=self._terminate_grace_s,
            kill_grace_s=self._kill_grace_s,
            process_group_poll_interval_s=self._process_group_poll_interval_s,
        )
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

    async def _drain_stderr(self) -> None:
        """Keep reading stderr forever; past max_stderr_bytes, discard and flag overflow."""
        assert self._proc.stderr is not None
        total = 0
        try:
            while True:
                chunk = await self._proc.stderr.read(self._read_chunk_bytes)
                if not chunk:
                    return
                total += len(chunk)
                if total > self._max_stderr_bytes:
                    self._stderr_overflow = True
                    # Keep draining so the child never blocks on a full stderr pipe.
        except asyncio.CancelledError:
            return
        except OSError:
            return

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
            to_read = min(self._read_chunk_bytes, max_bytes - len(self._buf) + 1)
            chunk = await self._proc.stdout.read(to_read)
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
        raise McpConfigError("stdio env_keys missing from environment: " + ", ".join(missing))
    return env


async def _reap_process_group(
    proc: asyncio.subprocess.Process,
    *,
    pgid: int,
    terminate_grace_s: float,
    kill_grace_s: float,
    process_group_poll_interval_s: float,
) -> None:
    """Always signal the process group (even if the leader already exited)."""
    await _close_subprocess_pipes(proc)

    _killpg(pgid, signal.SIGTERM)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=terminate_grace_s)
        except TimeoutError:
            pass
    else:
        # Leader already gone — one poll interval for grandchildren, not full grace.
        await asyncio.sleep(process_group_poll_interval_s)

    _killpg(pgid, signal.SIGKILL)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + kill_grace_s
    while True:
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=process_group_poll_interval_s)
            except TimeoutError:
                pass
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            await _close_subprocess_pipes(proc)
            return
        except OSError as exc:
            raise McpTransportError(
                f"process group probe failed after SIGKILL: {exc}",
                retryable=False,
            ) from exc
        if loop.time() >= deadline:
            await _close_subprocess_pipes(proc)
            raise McpTimeoutError("stdio process group still alive after SIGKILL within kill_grace_s")
        await asyncio.sleep(process_group_poll_interval_s)


async def _close_subprocess_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close stdin via public StreamWriter API (readers have no public close)."""
    if proc.stdin is None:
        return
    try:
        proc.stdin.close()
        await proc.stdin.wait_closed()
    except OSError:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _killpg(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise McpTransportError(
            f"killpg({sig.name}) failed: {exc}",
            retryable=False,
        ) from exc
