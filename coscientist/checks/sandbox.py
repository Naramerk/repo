"""Core-owned local sandbox for catalog CodeCheck execution (M1 PR 17).

Follows the old CoScientist code-exec pattern (subprocess + timeout kill,
output cap, clean terminal statuses) without HTTP: ``core``/``checks`` must
not import ``httpx``. Limits: time, memory (RLIMIT_AS), output size, and no
network unless the catalog check sets ``allow_network=True``.

``net_isolation`` records what was actually achieved (``hard``|``soft``|``none``).
``trusted`` on sandbox artifacts requires full limit enforcement — soft network
fallback never silently upgrades to trusted.
"""

from __future__ import annotations

import logging
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Mapping, Sequence

log = logging.getLogger(__name__)

NetIsolation = Literal["hard", "soft", "none"]


class SandboxStatus(StrEnum):
    OK = "ok"
    NONZERO = "nonzero"
    TIMEOUT = "timeout"
    OOM = "oom"
    CRASH = "crash"
    OUTPUT_EXCEEDED = "output_exceeded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


# Infra failures close the attempt as result_class=infra (never hang/crash host).
INFRA_STATUSES = frozenset(
    {
        SandboxStatus.TIMEOUT,
        SandboxStatus.OOM,
        SandboxStatus.CRASH,
        SandboxStatus.OUTPUT_EXCEEDED,
        SandboxStatus.UNAVAILABLE,
        SandboxStatus.BLOCKED,
    }
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MEMORY_MB = 256
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000

_BLOCKED_PATTERNS = (
    "rm --no-preserve-root",
    "mkfs",
    "/dev/sd",
    ":(){",
    "shutdown",
    "reboot",
    "poweroff",
)
_NET_BLOCKED_PATTERNS = (
    "curl ",
    "curl\t",
    "wget ",
    "wget\t",
    "nc ",
    "ncat ",
)

_OOM_CODES = frozenset({-signal.SIGKILL, signal.SIGKILL, 128 + signal.SIGKILL, 137})
_CRASH_CODES = frozenset({-signal.SIGSEGV, signal.SIGSEGV, 128 + signal.SIGSEGV, 139})

SOFT_ISOLATION_NOTE = (
    "net_isolation=soft; catalog required no network but hard unshare "
    "unavailable — evidence not trusted"
)


@dataclass(frozen=True)
class SandboxLimits:
    timeout_s: float = DEFAULT_TIMEOUT_S
    memory_mb: int = DEFAULT_MEMORY_MB
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    allow_network: bool = False


@dataclass(frozen=True)
class SandboxResult:
    status: SandboxStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    reason: str
    duration_s: float
    net_isolation: NetIsolation = "none"

    @property
    def limits_fully_held(self) -> bool:
        """True when catalog limits including network policy were fully enforced."""
        return limits_fully_held(self.net_isolation)


def limits_fully_held(net_isolation: NetIsolation) -> bool:
    """Soft isolation means the no-network catalog limit did not fully hold."""
    return net_isolation != "soft"


def _cap(data: bytes, limit: int) -> tuple[bytes, bool]:
    if len(data) <= limit:
        return data, False
    return data[:limit] + b"\n...[truncated]", True


def _blocked(command_text: str, *, allow_network: bool) -> str | None:
    low = command_text.lower()
    for pat in _BLOCKED_PATTERNS:
        if pat in low:
            return pat
    if not allow_network:
        first = Path(low.split()[0]).name if low.split() else ""
        if first in {"curl", "wget", "nc", "ncat", "fetch"}:
            return first
        for pat in _NET_BLOCKED_PATTERNS:
            if pat in low:
                return pat.strip()
    return None


def _preexec(memory_mb: int, timeout_s: float) -> None:
    as_bytes = max(1, int(memory_mb)) * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    except (ValueError, OSError):
        pass
    try:
        cpu = max(1, int(timeout_s) + 5)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    except (ValueError, OSError):
        pass
    os.setsid()


def _network_prefix() -> list[str] | None:
    """Return argv prefix that drops network, or None if hard isolation unavailable."""
    unshare = shutil.which("unshare")
    if unshare is None:
        return None
    candidates = [
        [unshare, "--user", "--net", "--map-root-user", "--"],
        [unshare, "--net", "--"],
    ]
    for prefix in candidates:
        try:
            probe = subprocess.run(
                [*prefix, "true"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return prefix
    return None


def resolve_net_isolation(*, allow_network: bool) -> tuple[NetIsolation, list[str]]:
    """Decide isolation level and optional unshare argv prefix.

    Returns ``(level, prefix)``. ``allow_network=True`` → ``none`` / empty prefix.
    Otherwise prefer hard unshare; if forbidden, soft (empty prefix) with warning.
    """
    if allow_network:
        return "none", []
    net = _network_prefix()
    if net is not None:
        return "hard", list(net)
    log.warning(
        "sandbox soft net_isolation: hard unshare unavailable; "
        "evidence will not be trusted (proxy poison + fetcher blocklist only)"
    )
    return "soft", []


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _result(
    *,
    status: SandboxStatus,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    reason: str,
    duration_s: float,
    net_isolation: NetIsolation,
) -> SandboxResult:
    return SandboxResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        reason=reason,
        duration_s=duration_s,
        net_isolation=net_isolation,
    )


def run_sandbox(
    argv: Sequence[str],
    *,
    limits: SandboxLimits | None = None,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    inputs: Mapping[str, bytes] | None = None,
) -> SandboxResult:
    """Run ``argv`` under enforced limits; never raises for guest failures.

    RO inputs: files are written then chmod'd read-only under a temp workspace
    (or ``cwd`` when provided). ``net_isolation`` is always recorded.
    """
    lim = limits or SandboxLimits()
    isolation, prefix = resolve_net_isolation(allow_network=lim.allow_network)

    if not argv:
        return _result(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            reason="sandbox_unavailable:empty_argv",
            duration_s=0.0,
            net_isolation=isolation,
        )

    cmd_text = " ".join(str(a) for a in argv)
    blocked = _blocked(cmd_text, allow_network=lim.allow_network)
    if blocked is not None:
        return _result(
            status=SandboxStatus.BLOCKED,
            exit_code=None,
            stdout=b"",
            stderr=f"blocked:{blocked}".encode(),
            reason=f"sandbox_blocked:{blocked}",
            duration_s=0.0,
            net_isolation=isolation,
        )

    own_tmp = cwd is None
    work = Path(cwd) if cwd is not None else Path(tempfile.mkdtemp(prefix="cos_sbx_"))
    try:
        work.mkdir(parents=True, exist_ok=True)
        for name, data in (inputs or {}).items():
            safe = Path(name).name
            path = work / safe
            path.write_bytes(data)
            path.chmod(0o444)

        full = [*prefix, *[str(a) for a in argv]]
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(work),
            "TMPDIR": str(work),
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "COSCIENTIST_SANDBOX_NET": isolation,
        }
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})
        if not lim.allow_network:
            child_env["http_proxy"] = "http://127.0.0.1:0"
            child_env["https_proxy"] = "http://127.0.0.1:0"
            child_env["HTTP_PROXY"] = child_env["http_proxy"]
            child_env["HTTPS_PROXY"] = child_env["https_proxy"]
            child_env["NO_PROXY"] = "*"

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                full,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(work),
                env=child_env,
                preexec_fn=lambda: _preexec(lim.memory_mb, lim.timeout_s),
            )
        except OSError as e:
            return _result(
                status=SandboxStatus.UNAVAILABLE,
                exit_code=None,
                stdout=b"",
                stderr=str(e).encode(),
                reason=f"sandbox_unavailable:{e.__class__.__name__}",
                duration_s=0.0,
                net_isolation=isolation,
            )

        try:
            out_b, err_b = proc.communicate(timeout=float(lim.timeout_s))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                out_b, err_b = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                out_b, err_b = b"", b""
            duration = time.monotonic() - started
            out_b, _ = _cap(out_b or b"", lim.max_output_bytes)
            err_b, _ = _cap(err_b or b"", lim.max_output_bytes)
            return _result(
                status=SandboxStatus.TIMEOUT,
                exit_code=-1,
                stdout=out_b,
                stderr=err_b or f"timed out after {lim.timeout_s}s".encode(),
                reason="sandbox_timeout",
                duration_s=duration,
                net_isolation=isolation,
            )
        except Exception as e:  # noqa: BLE001
            _kill_group(proc)
            return _result(
                status=SandboxStatus.CRASH,
                exit_code=-1,
                stdout=b"",
                stderr=str(e).encode(),
                reason=f"sandbox_crash:{e.__class__.__name__}",
                duration_s=time.monotonic() - started,
                net_isolation=isolation,
            )

        duration = time.monotonic() - started
        out_b, out_x = _cap(out_b or b"", lim.max_output_bytes)
        err_b, err_x = _cap(err_b or b"", lim.max_output_bytes)
        if out_x or err_x:
            return _result(
                status=SandboxStatus.OUTPUT_EXCEEDED,
                exit_code=proc.returncode,
                stdout=out_b,
                stderr=err_b,
                reason="sandbox_output_exceeded",
                duration_s=duration,
                net_isolation=isolation,
            )

        code = proc.returncode if proc.returncode is not None else -1
        if code == 0:
            return _result(
                status=SandboxStatus.OK,
                exit_code=0,
                stdout=out_b,
                stderr=err_b,
                reason="ok",
                duration_s=duration,
                net_isolation=isolation,
            )
        if code in _OOM_CODES:
            return _result(
                status=SandboxStatus.OOM,
                exit_code=code,
                stdout=out_b,
                stderr=err_b,
                reason="sandbox_oom",
                duration_s=duration,
                net_isolation=isolation,
            )
        if code in _CRASH_CODES or code < 0:
            return _result(
                status=SandboxStatus.CRASH,
                exit_code=code,
                stdout=out_b,
                stderr=err_b,
                reason="sandbox_crash",
                duration_s=duration,
                net_isolation=isolation,
            )
        return _result(
            status=SandboxStatus.NONZERO,
            exit_code=code,
            stdout=out_b,
            stderr=err_b,
            reason=f"exit:{code}",
            duration_s=duration,
            net_isolation=isolation,
        )
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)


def limits_from_check(
    *,
    timeout_s: float | None,
    memory_mb: int | None,
    max_output_bytes: int | None,
    allow_network: bool = False,
) -> SandboxLimits:
    return SandboxLimits(
        timeout_s=float(timeout_s) if timeout_s is not None else DEFAULT_TIMEOUT_S,
        memory_mb=int(memory_mb) if memory_mb is not None else DEFAULT_MEMORY_MB,
        max_output_bytes=(
            int(max_output_bytes)
            if max_output_bytes is not None
            else DEFAULT_MAX_OUTPUT_BYTES
        ),
        allow_network=bool(allow_network),
    )
