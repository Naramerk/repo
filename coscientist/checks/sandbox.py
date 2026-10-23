"""Core-owned sandbox + separate catalog verifier (M1 PR17).

Agent jail (``run_sandbox``): bwrap --clearenv, system RO, only work RW,
--unshare-net/--unshare-pid/--unshare-ipc/uts/cgroup-try, --disable-userns,
--cap-drop ALL, --new-session, --die-with-parent.

Verifier jail (``run_verifier``): fresh bwrap; agent outputs/scorer/holdout/tests
RO; only results RW; read reward/result files — never trust stdout. No agent
code runs during verification.

Fail-closed: without verified hard FS isolation (real bwrap probe), do **not**
execute guest code — return ``sandbox_unavailable``. ``trusted`` requires both
net and fs hard. Infra only from the wrapper private channel (rlimit), host
timeout kill, or delegated cgroup OOM; guest exit codes never map to infra.
Guest output overflow is scientific (``output_exceeded``).
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
import uuid
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

# Wrapper private-channel magic (FD, not exit code). Guest exit 75 is scientific.
_INFRA_MAGIC = b"COS_SBX_INFRA_v1:"
_INFRA_RLIMIT = b"COS_SBX_INFRA_v1:rlimit\n"
# Exit after writing private channel (ignored for classification if magic present).
_WRAPPER_ABORT_EXIT = 75


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


# Infra: host timeout / delegated cgroup OOM / private-channel rlimit /
# blocked / unavailable. OUTPUT_EXCEEDED is scientific (guest's fault).
INFRA_STATUSES = frozenset(
    {
        SandboxStatus.TIMEOUT,
        SandboxStatus.OOM,
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
    """setrlimit / join cgroup via private channel, then exec.

    Rlimit failure writes magic to ``COSCIENTIST_INFRA_FD`` then aborts.
    Guest exit codes alone never classify as infra.
    """
    return f"""
import os, sys
infra_fd = os.environ.get("COSCIENTIST_INFRA_FD")
cg = os.environ.get("COSCIENTIST_CGROUP")
if cg:
    try:
        with open(os.path.join(cg, "cgroup.procs"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
try:
    import resource
except ImportError:
    if infra_fd is not None:
        try:
            os.write(int(infra_fd), {_INFRA_RLIMIT!r})
        except OSError:
            pass
    sys.exit({_WRAPPER_ABORT_EXIT})
as_bytes = {int(memory_mb)} * 1024 * 1024
try:
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    cpu = max(1, {int(timeout_s)} + 5)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
except (ValueError, OSError):
    if infra_fd is not None:
        try:
            os.write(int(infra_fd), {_INFRA_RLIMIT!r})
        except OSError:
            pass
    sys.exit({_WRAPPER_ABORT_EXIT})
argv = sys.argv[1:]
if not argv:
    sys.exit(64)
if infra_fd is not None:
    try:
        os.close(int(infra_fd))
    except OSError:
        pass
    os.environ.pop("COSCIENTIST_INFRA_FD", None)
os.environ.pop("COSCIENTIST_CGROUP", None)
try:
    os.execvp(argv[0], argv)
except MemoryError:
    # Without delegated cgroup this stays scientific (host notes OOM not detectable).
    sys.exit(1)
except OSError as e:
    sys.stderr.write(str(e) + "\\n")
    sys.exit(127)
"""


def _current_cgroup_path() -> Path | None:
    base = Path("/sys/fs/cgroup")
    if not (base / "cgroup.controllers").is_file():
        return None
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        # v2: 0::/path
        if line.startswith("0::"):
            rel = line.split("::", 1)[1].lstrip("/")
            return base / rel if rel else base
    return base


def _cgroup_delegation_available() -> bool:
    """True when we can create a child leaf and write memory.max (delegated)."""
    cur = _current_cgroup_path()
    if cur is None or not cur.is_dir():
        return False
    probe = cur / f"cos_sbx_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        probe.mkdir(exist_ok=False)
        (probe / "memory.max").write_text("1048576")
        return True
    except OSError:
        return False
    finally:
        try:
            if probe.exists():
                for child in probe.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                probe.rmdir()
        except OSError:
            pass


def _cgroup_prepare_leaf(memory_mb: int) -> Path | None:
    """Create a memory-limited leaf under a delegated cgroup v2 subtree."""
    if not _cgroup_delegation_available():
        return None
    cur = _current_cgroup_path()
    if cur is None:
        return None
    leaf = cur / f"cos_sbx_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        leaf.mkdir(exist_ok=False)
        controllers = (cur / "cgroup.controllers").read_text()
        if "memory" in controllers:
            try:
                (cur / "cgroup.subtree_control").write_text("+memory")
            except OSError:
                pass
        (leaf / "memory.max").write_text(str(max(1, int(memory_mb)) * 1024 * 1024))
        return leaf
    except OSError:
        try:
            if leaf.exists():
                leaf.rmdir()
        except OSError:
            pass
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


def _cgroup_remove_leaf(leaf: Path | None) -> None:
    if leaf is None:
        return
    try:
        for child in leaf.iterdir():
            try:
                child.unlink()
            except IsADirectoryError:
                pass
            except OSError:
                pass
        leaf.rmdir()
    except OSError:
        pass


def _result(**kw: object) -> SandboxResult:
    return SandboxResult(**kw)  # type: ignore[arg-type]


def _watch_process(
    proc: subprocess.Popen[bytes],
    *,
    lim: SandboxLimits,
) -> tuple[bool, bool, bytes, bytes]:
    """Return (timed_out, overflow, stdout, stderr)."""
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
    )


def _classify(
    *,
    timed_out: bool,
    overflow: bool,
    proc: subprocess.Popen[bytes],
    cgroup_leaf: Path | None,
    oom_detectable: bool,
    infra_msg: bytes,
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

    # Private channel first — guest cannot forge without the FD.
    if infra_msg.startswith(_INFRA_MAGIC):
        kind = infra_msg.split(b":", 1)[-1].strip().decode("ascii", "replace")
        return fin(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=proc.returncode,
            reason=f"sandbox_unavailable:{kind or 'wrapper'}",
        )
    if overflow:
        # Guest-caused output flood — scientific, not infra.
        return fin(
            status=SandboxStatus.OUTPUT_EXCEEDED,
            exit_code=proc.returncode,
            reason="output_exceeded",
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
    # Guest signals / nonzero exits (including forged 75) are scientific.
    if code == 0:
        return fin(status=SandboxStatus.OK, exit_code=0, reason="ok")
    return fin(
        status=SandboxStatus.NONZERO,
        exit_code=code,
        reason=f"exit:{code}",
    )


def _unavailable(
    *,
    reason: str,
    net_i: Isolation,
    fs_i: Isolation,
    note: str | None,
    stderr: bytes = b"",
) -> SandboxResult:
    return _result(
        status=SandboxStatus.UNAVAILABLE,
        exit_code=None,
        stdout=b"",
        stderr=stderr,
        reason=reason,
        duration_s=0.0,
        net_isolation=net_i,
        fs_isolation=fs_i,
        note=note,
    )


def _run_jailed(
    *,
    cmd: list[str],
    lim: SandboxLimits,
    net_i: Isolation,
    fs_i: Isolation,
    work_dir: str | None,
    result_files_dir: Path | None,
    collect: Sequence[str] | None,
    extra_env: Mapping[str, str] | None = None,
) -> SandboxResult:
    """Spawn bwrap+wrapper with private infra FD and optional cgroup leaf."""
    note = soft_isolation_note(net_i, fs_i)
    leaf = _cgroup_prepare_leaf(lim.memory_mb)
    oom_det = leaf is not None
    if not oom_det:
        note = f"{note}; OOM not detectable" if note else "OOM not detectable"

    r_fd, w_fd = os.pipe()
    os.set_inheritable(w_fd, True)
    os.set_inheritable(r_fd, False)

    env_pairs: list[tuple[str, str]] = [("COSCIENTIST_INFRA_FD", str(w_fd))]
    if leaf is not None:
        env_pairs.append(("COSCIENTIST_CGROUP", str(leaf)))
    if extra_env:
        env_pairs.extend((str(k), str(v)) for k, v in extra_env.items())

    # Re-inject env into bwrap argv: cmd already built; append --setenv before --.
    if "--" in cmd:
        idx = cmd.index("--")
        injected: list[str] = []
        for k, v in env_pairs:
            injected.extend(["--setenv", k, v])
        cmd = [*cmd[:idx], *injected, *cmd[idx:]]
    else:
        return _unavailable(
            reason="sandbox_unavailable:bad_argv",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
        )

    started = time.monotonic()
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=None,
                env=None,  # clearenv inside bwrap; host env not passed
                start_new_session=True,
                pass_fds=(w_fd,),
            )
        except OSError as e:
            return _unavailable(
                reason=f"sandbox_unavailable:{e.__class__.__name__}",
                net_i=net_i,
                fs_i=fs_i,
                note=note,
                stderr=str(e).encode(),
            )
        finally:
            try:
                os.close(w_fd)
            except OSError:
                pass

        timed_out, overflow, out_b, err_b = _watch_process(proc, lim=lim)
        infra_msg = b""
        try:
            # Non-blocking drain of private channel.
            import select

            if select.select([r_fd], [], [], 0)[0]:
                infra_msg = os.read(r_fd, 256)
        except OSError:
            infra_msg = b""
        finally:
            try:
                os.close(r_fd)
            except OSError:
                pass

        files: dict[str, bytes] = {}
        if result_files_dir is not None and collect:
            files = _collect_results(result_files_dir, collect)
        return _classify(
            timed_out=timed_out,
            overflow=overflow,
            proc=proc,
            cgroup_leaf=leaf,
            oom_detectable=oom_det,
            infra_msg=infra_msg,
            out_b=out_b,
            err_b=err_b,
            duration=time.monotonic() - started,
            net_i=net_i,
            fs_i=fs_i,
            work_dir=work_dir,
            result_files=files,
        )
    finally:
        _cgroup_remove_leaf(leaf)


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
    """Agent jail. Journal/CAS are not mounted (protect_paths ignored).

    Fail-closed without hard FS isolation — no guest exec.
    """
    del core_files, collect_core, protect_paths
    lim = limits or SandboxLimits()
    net_i, fs_i = resolve_isolation(allow_network=lim.allow_network)
    note = soft_isolation_note(net_i, fs_i)

    if not argv:
        return _unavailable(
            reason="sandbox_unavailable:empty_argv",
            net_i=net_i,
            fs_i=fs_i,
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

    # Fail-closed: soft / no bwrap → do not execute agent code.
    if fs_i != "hard":
        return _unavailable(
            reason="sandbox_unavailable:isolation",
            net_i=net_i,
            fs_i=fs_i,
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
        prefix = build_agent_bwrap(
            work=work, allow_network=lim.allow_network, extra_env=extra_env
        )
        cmd = [
            *prefix,
            "--",
            "python3",
            "/work/_cos_wrapper.py",
            *[str(a) for a in argv],
        ]
        return _run_jailed(
            cmd=cmd,
            lim=lim,
            net_i=net_i,
            fs_i=fs_i,
            work_dir=str(work),
            result_files_dir=None,
            collect=None,
        )
    except OSError as e:
        return _unavailable(
            reason=f"sandbox_unavailable:host_io:{e.__class__.__name__}",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            stderr=str(e).encode(),
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
    tests: Mapping[str, bytes] | None = None,
    collect: Sequence[str] = ("reward.txt",),
    extra_ro_files: Mapping[str, bytes] | None = None,
) -> SandboxResult:
    """Catalog verifier in a fresh jail. Agent outputs/tests RO; only results RW.

    Reads ``collect`` via O_NOFOLLOW — never stdout. No agent code as entrypoint.
    Fail-closed without hard isolation.
    """
    lim = limits or SandboxLimits()
    net_i, fs_i = resolve_isolation(allow_network=False)
    note = soft_isolation_note(net_i, fs_i)

    if not scorer_argv:
        return _unavailable(
            reason="sandbox_unavailable:missing_scorer",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
        )

    if fs_i != "hard":
        return _unavailable(
            reason="sandbox_unavailable:isolation",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
        )

    root = Path(tempfile.mkdtemp(prefix="cos_ver_"))
    try:
        inputs_dir = root / "inputs"
        results_dir = root / "results"
        tests_dir = root / "tests"
        inputs_dir.mkdir()
        results_dir.mkdir()
        tests_dir.mkdir()
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

        tests_path = None
        if tests:
            for name, data in tests.items():
                # Keep relative path structure under tests_dir (no escape).
                rel = Path(name)
                if rel.is_absolute() or ".." in rel.parts:
                    continue
                dest = tests_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest.chmod(0o444)
            tests_path = tests_dir

        wrapper = results_dir / "_cos_wrapper.py"
        wrapper.write_text(
            _wrapper_script(memory_mb=lim.memory_mb, timeout_s=lim.timeout_s),
            encoding="utf-8",
        )

        guest_argv = [str(a) for a in scorer_argv]
        if scorer_path is not None:
            guest_argv = [
                "/scorer"
                if (a == str(scorer_path) or a.endswith("scorer.py") or a == "/scorer")
                else a
                for a in guest_argv
            ]
        prefix = build_verifier_bwrap(
            results=results_dir,
            inputs=inputs_dir,
            scorer=scorer_path,
            holdout=holdout_path,
            tests=tests_path,
            allow_network=False,
        )
        cmd = [
            *prefix,
            "--",
            "python3",
            "/results/_cos_wrapper.py",
            *guest_argv,
        ]
        return _run_jailed(
            cmd=cmd,
            lim=lim,
            net_i=net_i,
            fs_i=fs_i,
            work_dir=str(results_dir),
            result_files_dir=results_dir,
            collect=collect,
        )
    except OSError as e:
        return _unavailable(
            reason=f"sandbox_unavailable:host_io:{e.__class__.__name__}",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            stderr=str(e).encode(),
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
