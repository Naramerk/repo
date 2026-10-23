"""Core-owned sandbox + separate catalog verifier (M1 PR17).

Agent jail (``run_sandbox``): bwrap --clearenv, system RO, only work RW,
--unshare-net/--unshare-pid/--unshare-ipc/uts/cgroup-try, --disable-userns,
--cap-drop ALL, --new-session, --die-with-parent.

Verifier jail (``run_verifier``): fresh bwrap; agent *data*/scorer/holdout/tests
RO; only results RW; read reward/result files — never trust stdout.
``metric_on_holdout`` runs only the catalog scorer (predictions are data; no
agent code). ``tests_pass`` imports agent modules → ``trusted=False``,
``countable=False`` (observation; tamper-proof tests = M2).

Fail-closed: without verified hard FS isolation (real bwrap probe), do **not**
execute guest code — return ``sandbox_unavailable``. ``trusted`` requires both
net and fs hard. Infra **only** via the private FD channel (wrapper rlimit
failure, cgroup attach failure, bwrap/launcher host errors) plus blocked /
unavailable / verified cgroup OOM — does **not** charge the retry budget.
Guest wall or CPU budget exhaustion is host-classified
(``idle_timeout`` / ``cpu_timeout`` / ``wall_timeout``): never countable toward
``k_ref``/``k_sup``; refute-role idle/timeout → ``refute_unresolved`` (hard-blocks
``supported``); idle → ``untestable_by_agent``. Cgroup ``cpu.max`` + ``cpu.stat``
bound the whole tree; CPU budget always strictly below wall. Seccomp → M2.
"""

from __future__ import annotations

import logging
import math
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
_INFRA_CGROUP_ATTACH = b"COS_SBX_INFRA_v1:cgroup_attach\n"
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


# Infra: private-FD channel / blocked / unavailable / verified cgroup OOM.
# Guest wall/CPU kill → TIMEOUT with host-classified reason (not countable).
INFRA_STATUSES = frozenset(
    {
        SandboxStatus.OOM,
        SandboxStatus.UNAVAILABLE,
        SandboxStatus.BLOCKED,
    }
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MEMORY_MB = 256
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
_TERM_GRACE_S = 0.5
# Host-side idle detection: cpu_usage/wall below this → idle_timeout (sabotage).
IDLE_CPU_WALL_RATIO = 0.05
TIMEOUT_KINDS = frozenset({"idle_timeout", "cpu_timeout", "wall_timeout"})

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

def agent_input_name_allowed(name: str, allowlist: frozenset[str] | None) -> bool:
    """Allowlist-only: ``name`` must appear in ``allowlist`` (basename match)."""
    if allowlist is None:
        return False
    base = Path(name).name
    if not base or base.startswith(".") or ".." in Path(name).parts:
        return False
    return base in allowlist


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
    # Host-measured CPU seconds for the cgroup tree (None if unavailable).
    cpu_usage_s: float | None = None
    # wall | cpu — which host budget tripped (None if not a timeout).
    timeout_kill: str | None = None

    @property
    def limits_fully_held(self) -> bool:
        return isolation_fully_hard(self.net_isolation, self.fs_isolation)

    @property
    def is_guest_timeout(self) -> bool:
        return self.status is SandboxStatus.TIMEOUT and self.reason in TIMEOUT_KINDS


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


def _cpu_budget_seconds(timeout_s: float) -> float:
    """CPU budget for the whole cgroup tree — always strictly below wall."""
    wall = float(timeout_s)
    if wall <= 0:
        return 0.05
    # 80% of wall, but at least 50ms less than wall when wall is short.
    budget = min(wall * 0.8, wall - 0.05)
    return max(0.05, budget)


def _rlimit_cpu_seconds(timeout_s: float, *, cgroup_live: bool) -> int | None:
    """Per-process RLIMIT_CPU in whole seconds.

    With a live cgroup: set RLIMIT **strictly above** the cgroup budget
    (``ceil`` of budget + max(1s, 10%), or ``floor(budget)+1`` when that is
    larger) as a fuse only — the host watcher must win the race and classify
    ``cpu_timeout``. For short walls (3–5s) the whole-second fuse may equal
    ``floor(wall)`` while still staying above budget; that is intentional so
    ``int()`` truncation cannot collapse the fuse onto/under the budget.
    Without cgroup: RLIMIT at ``floor(budget)`` (strictly below wall); exit
    SIGXCPU/152 is classified via host-measured child rusage (see ``_classify``).
    """
    wall = float(timeout_s)
    budget = _cpu_budget_seconds(wall)
    if cgroup_live:
        fuse = budget + max(1.0, budget * 0.10)
        if fuse >= wall:
            fuse = max(budget + 0.05, wall - 0.05)
        # Whole seconds must be strictly above budget (watcher wins below fuse).
        whole = max(math.ceil(fuse), math.floor(budget) + 1)
        if whole >= 1 and whole > budget and whole <= max(math.floor(wall), 1):
            return whole
        return None
    whole = math.floor(budget)
    if whole >= 1 and whole < wall:
        return whole
    return None


def _is_sigxcpu_exit(code: int) -> bool:
    """True for raw -SIGXCPU or shell-style 128+SIGXCPU (152)."""
    return code in (-int(signal.SIGXCPU), 128 + int(signal.SIGXCPU))


def _host_tree_cpu_s(root_pid: int) -> float | None:
    """Sum utime+stime (seconds) for ``root_pid`` and descendants via ``/proc``.

    Used when cgroup ``cpu.stat`` is unavailable: bwrap guests are not the
    direct ``wait4`` child, so ``RUSAGE_CHILDREN`` under-counts. Sampling the
    host-visible process tree is the accurate host-side CPU measurement.
    """
    try:
        clk = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        clk = 100.0
    if clk <= 0:
        clk = 100.0
    try:
        pids: set[int] = {int(root_pid)}
        queue = [int(root_pid)]
        while queue:
            pid = queue.pop()
            try:
                for name in os.listdir(f"/proc/{pid}/task"):
                    children = Path(f"/proc/{pid}/task/{name}/children")
                    if not children.is_file():
                        continue
                    for tok in children.read_text().split():
                        try:
                            cpid = int(tok)
                        except ValueError:
                            continue
                        if cpid not in pids:
                            pids.add(cpid)
                            queue.append(cpid)
            except OSError:
                continue
        ticks = 0
        for pid in pids:
            try:
                raw = Path(f"/proc/{pid}/stat").read_text()
            except OSError:
                continue
            # comm may contain spaces/parens — split after the last ") ".
            try:
                rest = raw[raw.rindex(")") + 2 :].split()
                # fields 14,15 (1-indexed) → indices 11,12 after state in rest
                # After ") ": state is rest[0], utime rest[11], stime rest[12].
                ticks += int(rest[11]) + int(rest[12])
            except (ValueError, IndexError):
                continue
        return ticks / clk
    except OSError:
        return None


def _wrapper_script(
    *, memory_mb: int, timeout_s: float, cgroup_live: bool = False
) -> str:
    """setrlimit, close infra FD, then exec.

    Host starts bwrap already inside the cgroup leaf (launcher). Rlimit failure
    writes magic to ``COSCIENTIST_INFRA_FD`` then aborts. Infra FD closed before
    exec so agent/test code never inherits it. With cgroup, RLIMIT_CPU is a
    fuse above the cgroup budget; without cgroup it matches the budget and
    classification uses host rusage.
    """
    rlimit = _rlimit_cpu_seconds(timeout_s, cgroup_live=cgroup_live)
    rlimit_block = (
        f"resource.setrlimit(resource.RLIMIT_CPU, ({rlimit}, {rlimit + 1}))"
        if rlimit is not None
        else "pass  # no integer RLIMIT_CPU slot below wall"
    )
    return f"""
import os, sys
infra_fd = os.environ.get("COSCIENTIST_INFRA_FD")
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
    {rlimit_block}
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
# Never pass the infra FD to guest/agent code.
if infra_fd is not None:
    try:
        os.close(int(infra_fd))
    except OSError:
        pass
    os.environ.pop("COSCIENTIST_INFRA_FD", None)
try:
    os.execvp(argv[0], argv)
except MemoryError:
    # Without delegated cgroup this stays scientific (host notes OOM not detectable).
    sys.exit(1)
except OSError as e:
    sys.stderr.write(str(e) + "\\n")
    sys.exit(127)
"""


def _cgroup_bwrap_launcher(leaf: Path) -> str:
    """Host-side: join ``leaf`` then exec bwrap (starts jail already in cgroup).

    Attach failure signals infra via the private FD (never a bare scientific exit).
    """
    return f"""
import os, sys
leaf = {str(leaf)!r}
infra_fd = os.environ.get("COSCIENTIST_INFRA_FD")

def _infra(kind: bytes) -> None:
    if infra_fd is None:
        return
    try:
        os.write(int(infra_fd), kind)
    except OSError:
        pass

try:
    with open(os.path.join(leaf, "cgroup.procs"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
except OSError as e:
    sys.stderr.write(f"cgroup_attach:{{e.__class__.__name__}}\\n")
    _infra({_INFRA_CGROUP_ATTACH!r})
    sys.exit(70)
if len(sys.argv) < 2:
    _infra(b"COS_SBX_INFRA_v1:bad_argv\\n")
    sys.exit(64)
os.execvp(sys.argv[1], sys.argv[1:])
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


def _cgroup_prepare_leaf(memory_mb: int, timeout_s: float = DEFAULT_TIMEOUT_S) -> Path | None:
    """Create a memory+cpu-limited leaf under a delegated cgroup v2 subtree."""
    if not _cgroup_delegation_available():
        return None
    cur = _current_cgroup_path()
    if cur is None:
        return None
    leaf = cur / f"cos_sbx_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        leaf.mkdir(exist_ok=False)
        controllers = (cur / "cgroup.controllers").read_text()
        enable: list[str] = []
        if "memory" in controllers:
            enable.append("+memory")
        if "cpu" in controllers:
            enable.append("+cpu")
        if enable:
            try:
                (cur / "cgroup.subtree_control").write_text(" ".join(enable))
            except OSError:
                pass
        (leaf / "memory.max").write_text(str(max(1, int(memory_mb)) * 1024 * 1024))
        # Bound the whole tree to ~1 CPU of bandwidth; tree-wide time budget is
        # enforced via cpu.stat in the host watcher (always < wall).
        if "cpu" in controllers and (leaf / "cpu.max").exists():
            try:
                (leaf / "cpu.max").write_text("100000 100000")
            except OSError:
                pass
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


def _cgroup_cpu_usage_usec(leaf: Path | None) -> int | None:
    """Total CPU time used by the leaf tree (microseconds), or None."""
    if leaf is None:
        return None
    try:
        for line in (leaf / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[-1])
    except (OSError, ValueError):
        return None
    return None


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
    cgroup_leaf: Path | None = None,
) -> tuple[bool, bool, bytes, bytes, str | None, float | None, float | None]:
    """Return (timed_out, overflow, stdout, stderr, kill_cause, cpu_s, rusage_cpu_s).

    ``kill_cause`` is ``wall`` or ``cpu`` when the host budget tripped.
    ``cpu_s`` is cgroup ``cpu.stat`` when available; ``rusage_cpu_s`` is the
    host-measured process-tree CPU (for no-cgroup SIGXCPU classification).
    """
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
    kill_cause: str | None = None
    started = time.monotonic()
    deadline = started + float(lim.timeout_s)
    cpu_budget_usec = int(_cpu_budget_seconds(lim.timeout_s) * 1_000_000)
    last_usage: int | None = None
    tree_cpu_s: float | None = None
    while True:
        # Sample /proc *before* poll() — poll reaps the child and /proc vanishes.
        if cgroup_leaf is None and proc.pid:
            sampled = _host_tree_cpu_s(proc.pid)
            if sampled is not None:
                tree_cpu_s = (
                    sampled
                    if tree_cpu_s is None
                    else max(float(tree_cpu_s), float(sampled))
                )
        if proc.poll() is not None:
            break
        if overflow[0]:
            _kill_group_term_then_kill(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            kill_cause = "wall"
            _kill_group_term_then_kill(proc)
            break
        usage = _cgroup_cpu_usage_usec(cgroup_leaf)
        if usage is not None:
            last_usage = usage
            if usage >= cpu_budget_usec:
                timed_out = True
                kill_cause = "cpu"
                _kill_group_term_then_kill(proc)
                break
        time.sleep(0.02)
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    if last_usage is None:
        last_usage = _cgroup_cpu_usage_usec(cgroup_leaf)
    cpu_s = (last_usage / 1_000_000.0) if last_usage is not None else None
    return (
        timed_out,
        overflow[0],
        out_sink[0] if out_sink else b"",
        err_sink[0] if err_sink else b"",
        kill_cause,
        cpu_s,
        tree_cpu_s,
    )


def classify_guest_timeout(
    *,
    kill_cause: str | None,
    duration_s: float,
    cpu_usage_s: float | None,
    idle_ratio: float = IDLE_CPU_WALL_RATIO,
    refute_role: bool = False,
) -> str:
    """Host-only classification: idle_timeout | cpu_timeout | wall_timeout.

    ``cpu_timeout`` when ``kill_cause==\"cpu\"`` (host cgroup watcher). Guest
    ``SIGXCPU`` / exit 152 is handled in ``_classify`` via host rusage (no
    cgroup) or cgroup measurement — never trusted from the exit code alone.

    Idle (cpu/wall below ``idle_ratio``) requires measured ``cpu.stat``. Without
    a measurement, refute-role timeouts are treated as ``idle_timeout``
    (untestable / sabotage-suspected); other roles stay ``wall_timeout``.
    """
    if kill_cause == "cpu":
        return "cpu_timeout"
    if cpu_usage_s is None:
        return "idle_timeout" if refute_role else "wall_timeout"
    wall = max(float(duration_s), 1e-6)
    cpu = float(cpu_usage_s)
    if (cpu / wall) < float(idle_ratio):
        return "idle_timeout"
    return "wall_timeout"


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
    kill_cause: str | None = None,
    cpu_usage_s: float | None = None,
    rusage_cpu_s: float | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SandboxResult:
    note = soft_isolation_note(net_i, fs_i)
    if not oom_detectable:
        note = f"{note}; OOM not detectable" if note else "OOM not detectable"

    def fin(**extra: object) -> SandboxResult:
        if "timeout_kill" not in extra:
            extra = {
                **extra,
                "timeout_kill": kill_cause if timed_out else None,
            }
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
            cpu_usage_s=cpu_usage_s,
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
    code = proc.returncode if proc.returncode is not None else -1
    budget = _cpu_budget_seconds(timeout_s)
    if timed_out:
        # Guest timeout is NOT countable evidence — classified host-side only.
        # Role-aware idle elevation (refute / no cpu.stat) happens in evaluate.
        kind = classify_guest_timeout(
            kill_cause=kill_cause,
            duration_s=duration,
            cpu_usage_s=cpu_usage_s,
            refute_role=False,
        )
        return fin(
            status=SandboxStatus.TIMEOUT,
            exit_code=-1 if kill_cause == "wall" else code,
            reason=kind,
            timeout_kill=kill_cause or ("cpu" if kind == "cpu_timeout" else "wall"),
        )
    if _cgroup_oom_killed(cgroup_leaf):
        return fin(
            status=SandboxStatus.OOM,
            exit_code=proc.returncode,
            reason="sandbox_oom",
        )
    # SIGXCPU / exit 152: forgeable by the guest. With live cgroup the watcher
    # should have won; without cgroup promote to cpu_timeout only when host
    # tree CPU shows the child actually burned the (integer) RLIMIT budget.
    if _is_sigxcpu_exit(code):
        if cgroup_leaf is not None:
            measured = cpu_usage_s
            threshold = budget
        else:
            measured = rusage_cpu_s
            # RLIMIT_CPU is whole seconds at floor(budget); compare to that
            # threshold so a real busy loop that hit RLIMIT promotes cleanly.
            rlim = _rlimit_cpu_seconds(timeout_s, cgroup_live=False)
            threshold = float(rlim) if rlim is not None else budget
        # Sampling lags the integer RLIMIT edge by up to one watch tick; accept
        # measured CPU within 1s of the threshold (still far above a forge≈0).
        reached = (
            measured is not None
            and float(measured) >= max(0.5, float(threshold) - 1.0 + 1e-6)
        )
        if reached:
            # Prefer cgroup measurement; else stamp host tree CPU into the result.
            if cpu_usage_s is None:
                cpu_usage_s = float(measured)  # type: ignore[arg-type]
            return fin(
                status=SandboxStatus.TIMEOUT,
                exit_code=code,
                reason="cpu_timeout",
                timeout_kill="cpu",
            )
        return fin(
            status=SandboxStatus.NONZERO,
            exit_code=code,
            reason=f"exit:{code}",
        )
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


def _verify_guest_in_cgroup(leaf: Path | None, bwrap_pid: int) -> tuple[bool, str | None]:
    """oom_detectable only if memory.max applied and a GUEST host pid is in leaf.

    ``bwrap_pid`` is the launcher/bwrap host pid — must not count as the guest.
    """
    if leaf is None:
        return False, "OOM not detectable"
    try:
        mem_raw = (leaf / "memory.max").read_text().strip()
        if not mem_raw or mem_raw == "max":
            return False, "OOM not detectable: memory.max not applied"
        # Poll briefly for a guest host pid (child of bwrap in the leaf).
        for _ in range(50):
            procs = {
                int(x)
                for x in (leaf / "cgroup.procs").read_text().split()
                if x.isdigit()
            }
            guests = procs - {int(bwrap_pid)}
            if guests:
                return True, None
            time.sleep(0.01)
        return False, "OOM not detectable: guest cgroup membership unverified"
    except OSError as e:
        return False, f"OOM not detectable: {e.__class__.__name__}"


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
    """Spawn bwrap already inside a cgroup leaf + wrapper private infra FD."""
    note = soft_isolation_note(net_i, fs_i)
    leaf = _cgroup_prepare_leaf(lim.memory_mb, lim.timeout_s)

    r_fd, w_fd = os.pipe()
    os.set_inheritable(w_fd, True)
    os.set_inheritable(r_fd, False)

    env_pairs: list[tuple[str, str]] = [("COSCIENTIST_INFRA_FD", str(w_fd))]
    if extra_env:
        env_pairs.extend((str(k), str(v)) for k, v in extra_env.items())

    if "--" not in cmd:
        for fd in (r_fd, w_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        return _unavailable(
            reason="sandbox_unavailable:bad_argv",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
        )

    idx = cmd.index("--")
    injected: list[str] = []
    for k, v in env_pairs:
        injected.extend(["--setenv", k, v])
    bwrap_cmd = [*cmd[:idx], *injected, *cmd[idx:]]

    # Start bwrap already inside the leaf via a tiny host launcher.
    launcher_path: Path | None = None
    popen_env: dict[str, str] | None = None
    if leaf is not None:
        td = Path(tempfile.mkdtemp(prefix="cos_cg_"))
        launcher_path = td / "cgroup_launch.py"
        launcher_path.write_text(_cgroup_bwrap_launcher(leaf), encoding="utf-8")
        popen_cmd = ["python3", str(launcher_path), *bwrap_cmd]
        # Launcher (host) needs the infra FD env to signal attach failure.
        popen_env = {**os.environ, "COSCIENTIST_INFRA_FD": str(w_fd)}
    else:
        popen_cmd = bwrap_cmd
        note = f"{note}; OOM not detectable" if note else "OOM not detectable"

    started = time.monotonic()
    try:
        try:
            proc = subprocess.Popen(
                popen_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=None,
                env=popen_env,
                start_new_session=True,
                pass_fds=(w_fd,),
            )
        except OSError as e:
            try:
                os.close(w_fd)
            except OSError:
                pass
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

        oom_det, oom_frag = _verify_guest_in_cgroup(leaf, proc.pid)
        if oom_frag:
            note = f"{note}; {oom_frag}" if note else oom_frag

        timed_out, overflow, out_b, err_b, kill_cause, cpu_s, rusage_s = (
            _watch_process(proc, lim=lim, cgroup_leaf=leaf if oom_det else None)
        )
        infra_msg = b""
        try:
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
            cgroup_leaf=leaf if oom_det else None,
            oom_detectable=oom_det,
            infra_msg=infra_msg,
            out_b=out_b,
            err_b=err_b,
            duration=time.monotonic() - started,
            net_i=net_i,
            fs_i=fs_i,
            work_dir=work_dir,
            result_files=files,
            kill_cause=kill_cause,
            cpu_usage_s=cpu_s,
            rusage_cpu_s=rusage_s,
            timeout_s=lim.timeout_s,
        )
    finally:
        _cgroup_remove_leaf(leaf)
        if launcher_path is not None:
            import shutil

            shutil.rmtree(launcher_path.parent, ignore_errors=True)


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
            _wrapper_script(
                memory_mb=lim.memory_mb,
                timeout_s=lim.timeout_s,
                cgroup_live=_cgroup_delegation_available(),
            ),
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
    input_allowlist: frozenset[str] | None = None,
) -> SandboxResult:
    """Catalog verifier in a fresh jail. Agent outputs/tests RO; only results RW.

    Reads ``collect`` via O_NOFOLLOW — never stdout. No agent code as entrypoint.
    Fail-closed without hard isolation. ``input_allowlist`` is allowlist-only
    (``None``/empty → mount nothing from agent inputs).
    """
    lim = limits or SandboxLimits()
    net_i, fs_i = resolve_isolation(allow_network=False)
    note = soft_isolation_note(net_i, fs_i)
    allow = frozenset(input_allowlist) if input_allowlist is not None else frozenset()

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
            base = Path(name).name
            if not agent_input_name_allowed(base, allow):
                log.warning("sandbox: rejecting agent input name %r", base)
                continue
            p = inputs_dir / base
            p.write_bytes(data)
            p.chmod(0o444)
        for name, data in (extra_ro_files or {}).items():
            base = Path(name).name
            if not agent_input_name_allowed(base, allow):
                log.warning("sandbox: rejecting extra input name %r", base)
                continue
            p = inputs_dir / base
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
            _wrapper_script(
                memory_mb=lim.memory_mb,
                timeout_s=lim.timeout_s,
                cgroup_live=_cgroup_delegation_available(),
            ),
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
