"""Core-owned local sandbox for catalog CodeCheck execution (M1 PR 17).

Subprocess + timeout kill + streaming output cap + clean statuses. Isolation
levels (net/fs hard|soft|none) are recorded; trusted requires both hard.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence

from coscientist.checks.sandbox_probe import (
    Isolation,
    isolation_fully_hard,
    resolve_isolation,
)

log = logging.getLogger(__name__)

# Back-compat aliases (domain / kinds historically imported NetIsolation).
NetIsolation = Isolation
FsIsolation = Isolation

# Marker printed by the rlimit wrapper when MemoryError / alloc failure observed.
OOM_MARKER = b"COSCIENTIST_SANDBOX_OOM\n"
RLIMIT_FAIL_MARKER = b"COSCIENTIST_SANDBOX_RLIMIT_FAIL\n"


class SandboxStatus(StrEnum):
    OK = "ok"
    NONZERO = "nonzero"
    TIMEOUT = "timeout"
    OOM = "oom"
    CRASH = "crash"
    OUTPUT_EXCEEDED = "output_exceeded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    MALFORMED = "malformed"  # kinds map parse errors; not set by runner


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
_NET_TOOLS = frozenset({"curl", "wget", "nc", "ncat", "fetch"})

SOFT_ISOLATION_NOTE = (
    "isolation soft/none: network may be open (fetcher blocklist is bypassable); "
    "FS not mount-isolated — evidence not trusted"
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
    net_isolation: Isolation = "none"
    fs_isolation: Isolation = "none"
    work_dir: str | None = None
    core_outputs: dict[str, bytes] = field(default_factory=dict)

    @property
    def limits_fully_held(self) -> bool:
        return isolation_fully_hard(self.net_isolation, self.fs_isolation)


def limits_fully_held(
    net_isolation: Isolation,
    fs_isolation: Isolation = "hard",
) -> bool:
    """True only when both net and fs isolation are hard (MJ3)."""
    return isolation_fully_hard(net_isolation, fs_isolation)


def _blocked(command_text: str, *, allow_network: bool) -> str | None:
    low = command_text.lower()
    for pat in _BLOCKED_PATTERNS:
        if pat in low:
            return pat
    if not allow_network:
        first = Path(low.split()[0]).name if low.split() else ""
        if first in _NET_TOOLS:
            return first
    return None


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


def _read_capped(
    pipe: object | None,
    limit: int,
    sink: list[bytes],
    overflow: list[bool],
) -> None:
    """Read pipe incrementally; set overflow[0] when limit exceeded (MJ1)."""
    if pipe is None:
        return
    total = 0
    chunks: list[bytes] = []
    try:
        while True:
            chunk = pipe.read(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                overflow[0] = True
                keep = limit - (total - len(chunk))
                if keep > 0:
                    chunks.append(chunk[:keep])
                break
            chunks.append(chunk)
    except Exception:  # noqa: BLE001
        pass
    sink.append(b"".join(chunks))


def _wrapper_script(memory_mb: int, timeout_s: float) -> str:
    """Child wrapper: protect paths, set rlimits (fail loudly), then exec guest."""
    # Lazy import resource inside wrapper so host Windows import stays optional.
    return f"""
import os, sys, subprocess
try:
    import resource
except ImportError:
    sys.stderr.buffer.write({RLIMIT_FAIL_MARKER!r})
    sys.exit(75)
# Hard FS: cover host journal/CAS/meta with tmpfs inside the mount namespace.
if os.environ.get("COSCIENTIST_SANDBOX_FS") == "hard":
    for p in os.environ.get("COSCIENTIST_PROTECT_PATHS", "").split(":"):
        if p and os.path.isdir(p):
            subprocess.run(
                ["mount", "-t", "tmpfs", "-o", "size=1M,mode=755", "none", p],
                check=False,
                capture_output=True,
            )
as_bytes = {int(memory_mb)} * 1024 * 1024
try:
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    cpu = max(1, {int(timeout_s)} + 5)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
except (ValueError, OSError) as e:
    sys.stderr.buffer.write({RLIMIT_FAIL_MARKER!r})
    sys.stderr.write(str(e) + "\\n")
    sys.exit(75)
os.environ["COSCIENTIST_SANDBOX_CORE"] = os.environ.get(
    "COSCIENTIST_SANDBOX_CORE", "core"
)
argv = sys.argv[1:]
if not argv:
    sys.exit(64)
try:
    os.execvp(argv[0], argv)
except MemoryError:
    sys.stderr.buffer.write({OOM_MARKER!r})
    sys.exit(1)
except OSError as e:
    sys.stderr.write(str(e) + "\\n")
    sys.exit(127)
"""


def _result(**kw: object) -> SandboxResult:
    return SandboxResult(**kw)  # type: ignore[arg-type]


def _collect_core(work: Path, names: Sequence[str] | None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not names:
        return out
    core = work / "core"
    for name in names:
        path = core / Path(name).name
        if path.is_file():
            try:
                out[Path(name).name] = path.read_bytes()
            except OSError:
                pass
    return out


def run_sandbox(
    argv: Sequence[str],
    *,
    limits: SandboxLimits | None = None,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    inputs: Mapping[str, bytes] | None = None,
    core_files: Mapping[str, bytes] | None = None,
    collect_core: Sequence[str] | None = None,
    protect_paths: Sequence[str] | None = None,
) -> SandboxResult:
    """Run ``argv`` under limits. Never raises for guest failures.

    ``inputs`` → RO files in work/; ``core_files`` → initial files under work/core/
    (core-owned outputs directory; guest may write here). Host CAS/meta are never
    mounted into the guest workdir; ``protect_paths`` are tmpfs-covered when
    ``fs_isolation=hard``. Core outputs listed in ``collect_core`` are returned
    on the result (read before workdir cleanup).
    """
    lim = limits or SandboxLimits()
    net_i, fs_i, prefix = resolve_isolation(allow_network=lim.allow_network)

    if not argv:
        return _result(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            reason="sandbox_unavailable:empty_argv",
            duration_s=0.0,
            net_isolation=net_i,
            fs_isolation=fs_i,
        )

    blocked = _blocked(" ".join(str(a) for a in argv), allow_network=lim.allow_network)
    if blocked is not None:
        return _result(
            status=SandboxStatus.BLOCKED,
            exit_code=None,
            stdout=b"",
            stderr=f"blocked:{blocked}".encode(),
            reason=f"sandbox_blocked:{blocked}",
            duration_s=0.0,
            net_isolation=net_i,
            fs_isolation=fs_i,
        )

    own_tmp = cwd is None
    work: Path | None = None
    try:
        try:
            work = Path(cwd) if cwd is not None else Path(tempfile.mkdtemp(prefix="cos_sbx_"))
            work.mkdir(parents=True, exist_ok=True)
            core = work / "core"
            core.mkdir(parents=True, exist_ok=True)
            for name, data in (inputs or {}).items():
                path = work / Path(name).name
                path.write_bytes(data)
                path.chmod(0o444)
            for name, data in (core_files or {}).items():
                path = core / Path(name).name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        except OSError as e:
            return _result(
                status=SandboxStatus.UNAVAILABLE,
                exit_code=None,
                stdout=b"",
                stderr=str(e).encode(),
                reason=f"sandbox_unavailable:host_io:{e.__class__.__name__}",
                duration_s=0.0,
                net_isolation=net_i,
                fs_isolation=fs_i,
                work_dir=str(work) if work is not None else None,
            )

        assert work is not None
        wrapper = work / "_cos_wrapper.py"
        try:
            wrapper.write_text(_wrapper_script(lim.memory_mb, lim.timeout_s), encoding="utf-8")
        except OSError as e:
            return _result(
                status=SandboxStatus.UNAVAILABLE,
                exit_code=None,
                stdout=b"",
                stderr=str(e).encode(),
                reason=f"sandbox_unavailable:host_io:{e.__class__.__name__}",
                duration_s=0.0,
                net_isolation=net_i,
                fs_isolation=fs_i,
                work_dir=str(work),
            )

        full = [
            *prefix,
            "python3",
            str(wrapper),
            *[str(a) for a in argv],
        ]
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(work),
            "TMPDIR": str(work),
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "COSCIENTIST_SANDBOX_NET": net_i,
            "COSCIENTIST_SANDBOX_FS": fs_i,
            "COSCIENTIST_SANDBOX_CORE": str(core),
            "COSCIENTIST_SANDBOX_WORK": str(work),
            "COSCIENTIST_PROTECT_PATHS": ":".join(
                str(p) for p in (protect_paths or ()) if p
            ),
        }
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})
        if not lim.allow_network:
            child_env["http_proxy"] = "http://127.0.0.1:0"
            child_env["https_proxy"] = child_env["http_proxy"]
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
                start_new_session=True,  # avoid preexec_fn in threaded hosts
            )
        except OSError as e:
            return _result(
                status=SandboxStatus.UNAVAILABLE,
                exit_code=None,
                stdout=b"",
                stderr=str(e).encode(),
                reason=f"sandbox_unavailable:{e.__class__.__name__}",
                duration_s=0.0,
                net_isolation=net_i,
                fs_isolation=fs_i,
                work_dir=str(work),
            )

        out_sink: list[bytes] = []
        err_sink: list[bytes] = []
        overflow = [False]
        t_out = threading.Thread(
            target=_read_capped,
            args=(proc.stdout, lim.max_output_bytes, out_sink, overflow),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_read_capped,
            args=(proc.stderr, lim.max_output_bytes, err_sink, overflow),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        timed_out = False
        # Poll so we can kill on streaming overflow before the wall timeout (MJ1).
        deadline = started + float(lim.timeout_s)
        while proc.poll() is None:
            if overflow[0]:
                _kill_group(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc)
                break
            time.sleep(0.02)
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        duration = time.monotonic() - started
        out_b = out_sink[0] if out_sink else b""
        err_b = err_sink[0] if err_sink else b""
        core_out = _collect_core(work, collect_core)
        if timed_out and not err_b:
            err_b = f"timed out after {lim.timeout_s}s".encode()

        def _fin(**extra: object) -> SandboxResult:
            return _result(
                stdout=out_b,
                stderr=err_b,
                duration_s=duration,
                net_isolation=net_i,
                fs_isolation=fs_i,
                work_dir=str(work),
                core_outputs=core_out,
                **extra,
            )

        if overflow[0]:
            # Streaming cap hit — kill already issued in the poll loop (MJ1).
            return _fin(
                status=SandboxStatus.OUTPUT_EXCEEDED,
                exit_code=proc.returncode,
                reason="sandbox_output_exceeded",
            )
        if timed_out:
            # We know we killpg'd for timeout — not OOM (B3).
            return _fin(
                status=SandboxStatus.TIMEOUT,
                exit_code=-1,
                reason="sandbox_timeout",
            )
        if RLIMIT_FAIL_MARKER in err_b:
            return _fin(
                status=SandboxStatus.UNAVAILABLE,
                exit_code=proc.returncode,
                reason="sandbox_unavailable:rlimit",
            )
        if OOM_MARKER in err_b:
            return _fin(
                status=SandboxStatus.OOM,
                exit_code=proc.returncode,
                reason="sandbox_oom",
            )

        code = proc.returncode if proc.returncode is not None else -1
        # Infra by signal only (returncode < 0). Positive codes (incl. 137) stay
        # scientific NONZERO — sys.exit(137) must not become OOM (B3).
        if code < 0:
            return _fin(
                status=SandboxStatus.CRASH,
                exit_code=code,
                reason="sandbox_crash",
            )
        if code == 0:
            return _fin(
                status=SandboxStatus.OK,
                exit_code=0,
                reason="ok",
            )
        return _fin(
            status=SandboxStatus.NONZERO,
            exit_code=code,
            reason=f"exit:{code}",
        )
    finally:
        if own_tmp and work is not None:
            import shutil

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


def read_core_output(work_dir: str | None, name: str) -> bytes | None:
    """Read a core-owned output file from a sandbox work dir (before cleanup)."""
    if not work_dir:
        return None
    path = Path(work_dir) / "core" / name
    if not path.is_file():
        return None
    return path.read_bytes()
