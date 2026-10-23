"""Core-owned sandbox + separate catalog verifier (M1 PR17).

Agent jail (``run_sandbox``): bwrap --clearenv, system RO, only work RW,
--unshare-net/--unshare-pid, --die-with-parent.

Verifier jail (``run_verifier``): fresh bwrap; agent outputs/scorer/holdout RO;
only results RW; read reward/result files — never trust stdout. No agent code
runs during verification.

trusted requires both net and fs hard after a real bwrap probe. No bwrap →
soft, trusted=False. Infra only from host-observed timeout kill or cgroup OOM;
guest self-signals are scientific.
"""

from __future__ import annotations

import logging
import os
import signal
import stat as statmod
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
    build_agent_bwrap,
    build_verifier_bwrap,
    bwrap_available,
    isolation_fully_hard,
    resolve_isolation,
)

log = logging.getLogger(__name__)

NetIsolation = Isolation
FsIsolation = Isolation

# Wrapper exits with this before exec when setrlimit fails (host-visible).
RLIMIT_EXIT = 75


class SandboxStatus(StrEnum):
    OK = "ok"
    NONZERO = "nonzero"
    TIMEOUT = "timeout"
    OOM = "oom"
    CRASH = "crash"
    OUTPUT_EXCEEDED = "output_exceeded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    MALFORMED = "malformed"


# Guest self-signal/kill is scientific (NONZERO). Only host timeout / cgroup OOM
# / output cap / blocked / unavailable are infra.
INFRA_STATUSES = frozenset(
    {
        SandboxStatus.TIMEOUT,
        SandboxStatus.OOM,
        SandboxStatus.OUTPUT_EXCEEDED,
        SandboxStatus.UNAVAILABLE,
        SandboxStatus.BLOCKED,
    }
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MEMORY_MB = 256
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
_TERM_GRACE_S = 0.5

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


def soft_isolation_note(net: Isolation, fs: Isolation) -> str | None:
    if isolation_fully_hard(net, fs):
        return None
    parts: list[str] = []
    if net != "hard":
        parts.append("network may be open (fetcher blocklist is bypassable)")
    if fs != "hard":
        parts.append("FS not mount-isolated")
    if not bwrap_available():
        parts.append("isolation unavailable (no bubblewrap)")
    return "isolation soft/unavailable: " + "; ".join(parts) + " — evidence not trusted"


SOFT_ISOLATION_NOTE = soft_isolation_note("soft", "soft") or (
    "isolation soft/unavailable — evidence not trusted"
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
    result_files: dict[str, bytes] = field(default_factory=dict)
    oom_detectable: bool = False
    note: str | None = None

    @property
    def limits_fully_held(self) -> bool:
        return isolation_fully_hard(self.net_isolation, self.fs_isolation)

    @property
    def core_outputs(self) -> dict[str, bytes]:
        return self.result_files


def limits_fully_held(
    net_isolation: Isolation,
    fs_isolation: Isolation = "none",
) -> bool:
    """True only when both are hard. Default fs is none (never assume hard)."""
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


def _kill_group_term_then_kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=_TERM_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
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


def _read_nofollow(path: Path) -> bytes | None:
    """Read a regular file without following symlinks (lstat + O_NOFOLLOW)."""
    try:
        st = os.lstat(path)
        if not statmod.S_ISREG(st.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            parts: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                parts.append(chunk)
            return b"".join(parts)
        finally:
            os.close(fd)
    except OSError:
        return None


def _collect_results(results_dir: Path, names: Sequence[str] | None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not names:
        return out
    for name in names:
        data = _read_nofollow(results_dir / Path(name).name)
        if data is not None:
            out[Path(name).name] = data
    return out


def _wrapper_script(*, memory_mb: int, timeout_s: float) -> str:
    """setrlimit then exec; rlimit failure → exit 75 (host classifies)."""
    return f"""
import os, sys
try:
    import resource
except ImportError:
    sys.exit({RLIMIT_EXIT})
as_bytes = {int(memory_mb)} * 1024 * 1024
try:
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    cpu = max(1, {int(timeout_s)} + 5)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
except (ValueError, OSError):
    sys.exit({RLIMIT_EXIT})
argv = sys.argv[1:]
if not argv:
    sys.exit(64)
try:
    os.execvp(argv[0], argv)
except MemoryError:
    # Without cgroup this stays scientific (host notes OOM not detectable).
    sys.exit(1)
except OSError as e:
    sys.stderr.write(str(e) + "\\n")
    sys.exit(127)
"""


def _cgroup_oom_setup(pid: int, memory_mb: int) -> Path | None:
    """Move pid into a memory-limited cgroup v2 leaf; return leaf path or None."""
    base = Path("/sys/fs/cgroup")
    if not (base / "cgroup.controllers").is_file():
        return None
    try:
        leaf = base / f"cos_sbx_{os.getpid()}_{pid}"
        leaf.mkdir(exist_ok=True)
        controllers = (base / "cgroup.controllers").read_text()
        if "memory" in controllers:
            (leaf / "cgroup.subtree_control").write_text("+memory")
        (leaf / "memory.max").write_text(str(max(1, int(memory_mb)) * 1024 * 1024))
        (leaf / "cgroup.procs").write_text(str(pid))
        return leaf
    except OSError:
        return None


def _cgroup_oom_killed(leaf: Path | None) -> bool:
    if leaf is None:
        return False
    try:
        events = (leaf / "memory.events").read_text()
        for line in events.splitlines():
            if line.startswith("oom_kill"):
                return int(line.split()[-1]) > 0
    except (OSError, ValueError):
        return False
    return False


def _result(**kw: object) -> SandboxResult:
    return SandboxResult(**kw)  # type: ignore[arg-type]


def _watch_process(
    proc: subprocess.Popen[bytes],
    *,
    lim: SandboxLimits,
    cgroup_leaf: Path | None,
) -> tuple[bool, bool, bytes, bytes, bool]:
    """Return (timed_out, overflow, stdout, stderr, oom_detectable)."""
    out_sink: list[bytes] = []
    err_sink: list[bytes] = []
    overflow = [False]
    threading.Thread(
        target=_read_capped,
        args=(proc.stdout, lim.max_output_bytes, out_sink, overflow),
        daemon=True,
    ).start()
    threading.Thread(
        target=_read_capped,
        args=(proc.stderr, lim.max_output_bytes, err_sink, overflow),
        daemon=True,
    ).start()
    timed_out = False
    started = time.monotonic()
    deadline = started + float(lim.timeout_s)
    while proc.poll() is None:
        if overflow[0]:
            _kill_group_term_then_kill(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_group_term_then_kill(proc)
            break
        time.sleep(0.02)
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return (
        timed_out,
        overflow[0],
        out_sink[0] if out_sink else b"",
        err_sink[0] if err_sink else b"",
        cgroup_leaf is not None,
    )


def _classify(
    *,
    timed_out: bool,
    overflow: bool,
    proc: subprocess.Popen[bytes],
    cgroup_leaf: Path | None,
    oom_detectable: bool,
    out_b: bytes,
    err_b: bytes,
    duration: float,
    net_i: Isolation,
    fs_i: Isolation,
    work_dir: str | None,
    result_files: dict[str, bytes],
) -> SandboxResult:
    note = soft_isolation_note(net_i, fs_i)
    if not oom_detectable:
        note = f"{note}; OOM not detectable" if note else "OOM not detectable"

    def fin(**extra: object) -> SandboxResult:
        return _result(
            stdout=out_b,
            stderr=err_b,
            duration_s=duration,
            net_isolation=net_i,
            fs_isolation=fs_i,
            work_dir=work_dir,
            result_files=result_files,
            oom_detectable=oom_detectable,
            note=note,
            **extra,
        )

    if overflow:
        return fin(
            status=SandboxStatus.OUTPUT_EXCEEDED,
            exit_code=proc.returncode,
            reason="sandbox_output_exceeded",
        )
    if timed_out:
        return fin(
            status=SandboxStatus.TIMEOUT,
            exit_code=-1,
            reason="sandbox_timeout",
        )
    if _cgroup_oom_killed(cgroup_leaf):
        return fin(
            status=SandboxStatus.OOM,
            exit_code=proc.returncode,
            reason="sandbox_oom",
        )
    code = proc.returncode if proc.returncode is not None else -1
    if code == RLIMIT_EXIT:
        return fin(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=code,
            reason="sandbox_unavailable:rlimit",
        )
    # Guest signals / nonzero exits are scientific — not infra.
    if code == 0:
        return fin(status=SandboxStatus.OK, exit_code=0, reason="ok")
    return fin(
        status=SandboxStatus.NONZERO,
        exit_code=code,
        reason=f"exit:{code}",
    )


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
    """Agent jail. Journal/CAS are not mounted (protect_paths ignored)."""
    del core_files, collect_core, protect_paths
    lim = limits or SandboxLimits()
    net_i, fs_i = resolve_isolation(allow_network=lim.allow_network)
    hard = isolation_fully_hard(net_i, fs_i)
    note = soft_isolation_note(net_i, fs_i)

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
            note=note,
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
            note=note,
        )

    own = cwd is None
    work = Path(cwd) if cwd is not None else Path(tempfile.mkdtemp(prefix="cos_agent_"))
    try:
        work.mkdir(parents=True, exist_ok=True)
        for name, data in (inputs or {}).items():
            p = work / Path(name).name
            p.write_bytes(data)
            p.chmod(0o444)
        wrapper = work / "_cos_wrapper.py"
        wrapper.write_text(
            _wrapper_script(memory_mb=lim.memory_mb, timeout_s=lim.timeout_s),
            encoding="utf-8",
        )
        extra_env = [(str(k), str(v)) for k, v in (env or {}).items()]
        if hard:
            prefix = build_agent_bwrap(
                work=work, allow_network=lim.allow_network, extra_env=extra_env
            )
            cmd = [*prefix, "--", "python3", "/work/_cos_wrapper.py", *[str(a) for a in argv]]
            popen_cwd = None
            popen_env = None
        else:
            cmd = ["python3", str(wrapper), *[str(a) for a in argv]]
            popen_cwd = str(work)
            popen_env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(work),
                "TMPDIR": str(work),
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                **dict(extra_env),
            }

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=popen_cwd,
                env=popen_env,
                start_new_session=True,
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
                note=note,
            )
        leaf = _cgroup_oom_setup(proc.pid, lim.memory_mb)
        timed_out, overflow, out_b, err_b, oom_det = _watch_process(
            proc, lim=lim, cgroup_leaf=leaf
        )
        return _classify(
            timed_out=timed_out,
            overflow=overflow,
            proc=proc,
            cgroup_leaf=leaf,
            oom_detectable=oom_det,
            out_b=out_b,
            err_b=err_b,
            duration=time.monotonic() - started,
            net_i=net_i,
            fs_i=fs_i,
            work_dir=str(work),
            result_files={},
        )
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
            note=note,
        )
    finally:
        if own:
            import shutil

            shutil.rmtree(work, ignore_errors=True)


def run_verifier(
    scorer_argv: Sequence[str],
    *,
    inputs: Mapping[str, bytes],
    limits: SandboxLimits | None = None,
    holdout: bytes | None = None,
    scorer_bytes: bytes | None = None,
    collect: Sequence[str] = ("reward.txt",),
    extra_ro_files: Mapping[str, bytes] | None = None,
) -> SandboxResult:
    """Catalog verifier in a fresh jail. Agent outputs RO; only results RW.

    Reads ``collect`` via O_NOFOLLOW — never stdout. No agent code runs.
    """
    lim = limits or SandboxLimits()
    net_i, fs_i = resolve_isolation(allow_network=False)
    hard = isolation_fully_hard(net_i, fs_i)
    note = soft_isolation_note(net_i, fs_i)

    if not scorer_argv:
        return _result(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            reason="sandbox_unavailable:missing_scorer",
            duration_s=0.0,
            net_isolation=net_i,
            fs_isolation=fs_i,
            note=note,
        )

    root = Path(tempfile.mkdtemp(prefix="cos_ver_"))
    try:
        inputs_dir = root / "inputs"
        results_dir = root / "results"
        inputs_dir.mkdir()
        results_dir.mkdir()
        for name, data in inputs.items():
            p = inputs_dir / Path(name).name
            p.write_bytes(data)
            p.chmod(0o444)
        for name, data in (extra_ro_files or {}).items():
            p = inputs_dir / Path(name).name
            p.write_bytes(data)
            p.chmod(0o444)

        holdout_path = None
        if holdout is not None:
            holdout_path = root / "holdout.bin"
            holdout_path.write_bytes(holdout)
            holdout_path.chmod(0o444)

        scorer_path = None
        if scorer_bytes is not None:
            scorer_path = root / "scorer.py"
            scorer_path.write_bytes(scorer_bytes)
            scorer_path.chmod(0o555)

        wrapper = results_dir / "_cos_wrapper.py"
        wrapper.write_text(
            _wrapper_script(memory_mb=lim.memory_mb, timeout_s=lim.timeout_s),
            encoding="utf-8",
        )

        guest_argv = [str(a) for a in scorer_argv]
        if hard:
            prefix = build_verifier_bwrap(
                results=results_dir,
                inputs=inputs_dir,
                scorer=scorer_path,
                holdout=holdout_path,
                allow_network=False,
            )
            if scorer_path is not None:
                guest_argv = [
                    "/scorer"
                    if (a == str(scorer_path) or a.endswith("scorer.py") or a == "/scorer")
                    else a
                    for a in guest_argv
                ]
            cmd = [
                *prefix,
                "--",
                "python3",
                "/results/_cos_wrapper.py",
                *guest_argv,
            ]
            popen_cwd = None
            popen_env = None
        else:
            # Soft: remap guest paths (/scorer, /results/…) to host paths.
            if scorer_path is not None:
                guest_argv = [
                    str(scorer_path)
                    if (a == "/scorer" or a.endswith("scorer.py"))
                    else a
                    for a in guest_argv
                ]
            cmd = ["python3", str(wrapper), *guest_argv]
            popen_cwd = str(results_dir)
            popen_env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(results_dir),
                "TMPDIR": str(results_dir),
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "COSCIENTIST_INPUTS": str(inputs_dir),
                "COSCIENTIST_RESULTS": str(results_dir),
            }
            if holdout_path is not None:
                popen_env["COSCIENTIST_HOLDOUT"] = str(holdout_path)
            if scorer_path is not None:
                popen_env["COSCIENTIST_SCORER"] = str(scorer_path)

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=popen_cwd,
                env=popen_env,
                start_new_session=True,
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
                note=note,
            )
        leaf = _cgroup_oom_setup(proc.pid, lim.memory_mb)
        timed_out, overflow, out_b, err_b, oom_det = _watch_process(
            proc, lim=lim, cgroup_leaf=leaf
        )
        files = _collect_results(results_dir, collect)
        return _classify(
            timed_out=timed_out,
            overflow=overflow,
            proc=proc,
            cgroup_leaf=leaf,
            oom_detectable=oom_det,
            out_b=out_b,
            err_b=err_b,
            duration=time.monotonic() - started,
            net_i=net_i,
            fs_i=fs_i,
            work_dir=str(results_dir),
            result_files=files,
        )
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
            note=note,
        )
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


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
    if not work_dir:
        return None
    return _read_nofollow(Path(work_dir) / name)
