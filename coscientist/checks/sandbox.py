"""Core-owned sandbox + separate catalog verifier (M1 PR17).

Agent jail (``run_sandbox``): bwrap --clearenv, system RO, only work RW,
--unshare-net/--unshare-pid/--unshare-ipc/uts/cgroup-try, --disable-userns,
--cap-drop ALL, --new-session, --die-with-parent.

Verifier jail (``run_verifier``): fresh bwrap; agent *data*/scorer/holdout/tests
RO; only results RW; read reward/result files — never trust stdout.
``metric_on_holdout`` runs only the catalog scorer (predictions are data; no
agent code). ``tests_pass`` imports agent modules → ``trusted=False``,
``countable=False`` (observation; tamper-proof tests = M2).

What isolation enforces:
- **bwrap**: mount namespace (host paths not writable; only configured binds),
  network namespace (``--unshare-net`` when ``allow_network=False``), PID/IPC/UTS,
  dropped caps, no nested userns, die-with-parent.
- **cgroup v2 leaf** (when delegated): ``memory.max``, ``cpu.max``, ``pids.max``.
- **wrapper rlimits**: ``RLIMIT_AS``, ``RLIMIT_CPU`` (fuse / budget), and
  ``RLIMIT_NPROC`` (per **host UID**, not per sandbox — a process-wide ceiling
  shared with every other task of the same uid; cgroup ``pids.max`` is the
  per-jail bound when the pids controller is applied).

Fail-closed: without verified hard FS isolation (real bwrap probe), do **not**
execute guest code — return ``sandbox_unavailable``. Cgroup limit application
(``memory.max`` / ``cpu.max`` / ``pids.max`` / ``subtree_control``) is also
fail-closed unless ``SandboxLimits.allow_cgroup_degraded`` is set — then the
run may continue with ``limits_fully_held=False`` and an explicit note per
missing limit. ``trusted`` / ``limits_fully_held`` require hard net+fs **and**
cgroup limits held. Infra **only** via the private FD channel (wrapper rlimit
failure, cgroup attach failure, bwrap/launcher host errors) plus unavailable /
verified cgroup OOM — does **not** charge the retry budget.
Guest wall or CPU budget exhaustion is host-classified
(``idle_timeout`` / ``cpu_timeout`` / ``wall_timeout``): never countable toward
``k_ref``/``k_sup``; refute-role idle/timeout → ``refute_unresolved`` (hard-blocks
``supported``); idle → ``untestable_by_agent``. CPU budget always strictly below
wall. Seccomp → M2.
"""

from __future__ import annotations

import errno
import logging
import math
import os
import select
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
from typing import Callable, Mapping, Sequence

from coscientist.checks.catalog import (
    GUEST_PYTHON_ARGV0,
    SANDBOX_CGROUP_CPU_MAX,
    SANDBOX_CGROUP_PROBE_INTERVAL_S,
    SANDBOX_CGROUP_PROBE_MAX_ATTEMPTS,
    SANDBOX_CPU_BUDGET_FLOOR_S,
    SANDBOX_CPU_BUDGET_WALL_MARGIN_S,
    SANDBOX_CPU_BUDGET_WALL_RATIO,
    SANDBOX_IDLE_CPU_WALL_RATIO,
    SANDBOX_REAP_POLL_INTERVAL_S,
    SANDBOX_RLIMIT_FUSE_EXTRA_FLOOR_S,
    SANDBOX_RLIMIT_FUSE_EXTRA_RATIO,
    SANDBOX_RLIMIT_FUSE_WALL_MARGIN_S,
    SANDBOX_TERM_GRACE_S,
    SANDBOX_WATCH_POLL_INTERVAL_S,
    VERIFIER_SCORER_GUEST_PATH,
)
from coscientist.checks.sandbox_probe import (
    Isolation,
    build_agent_bwrap,
    build_verifier_bwrap,
    bwrap_available,
    isolation_fully_hard,
    resolve_isolation,
)
from coscientist.core.events.errors import SandboxInfraError

log = logging.getLogger(__name__)

NetIsolation = Isolation
FsIsolation = Isolation

# Wrapper private-channel magic (FD, not exit code). Guest exit 75 is scientific.
_INFRA_MAGIC = b"COS_SBX_INFRA_v1:"
_INFRA_RLIMIT = b"COS_SBX_INFRA_v1:rlimit\n"
_INFRA_CGROUP_ATTACH = b"COS_SBX_INFRA_v1:cgroup_attach\n"
_INFRA_EXEC_MEMORY = b"COS_SBX_INFRA_v1:exec_memory\n"
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
# CRASH / MALFORMED are never produced by this module; if one ever appears it
# must not collapse into scientific FAILED (fail-closed → infra).
# Guest wall/CPU kill → TIMEOUT with host-classified reason (not countable).
INFRA_STATUSES = frozenset(
    {
        SandboxStatus.OOM,
        SandboxStatus.UNAVAILABLE,
        SandboxStatus.BLOCKED,
        SandboxStatus.CRASH,
        SandboxStatus.MALFORMED,
    }
)

# SandboxLimits has no field defaults: scientific budgets come from the frozen
# per-check row, host tuning from ``catalog.SANDBOX_*`` (see ``limits_from_check``).
TIMEOUT_KINDS = frozenset({"idle_timeout", "cpu_timeout", "wall_timeout"})
# ``missing_limits`` tag when the cgroup leaf itself could not be prepared.
MISSING_LIMIT_CGROUP = "cgroup"
# bwrap launcher + >=1 forked guest process ever charged to the leaf.
MIN_PIDS_PEAK_FOR_GUEST = 2


def _clk_tck() -> float:
    """Host jiffy rate from ``SC_CLK_TCK``; fail-closed if unavailable."""
    try:
        clk = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError) as e:
        raise RuntimeError("SC_CLK_TCK unavailable") from e
    if not math.isfinite(clk) or clk <= 0.0:
        raise RuntimeError(f"SC_CLK_TCK must be finite and > 0, got {clk!r}")
    return clk


def _sigxcpu_from_poll(watch_poll_interval_s: float) -> tuple[float, float]:
    """``(slack_s, min_measured_s)`` from jiffy + watcher poll lag.

    Not computed at import: callers pass ``SandboxLimits.watch_poll_interval_s``.

    ``RLIMIT_CPU`` is whole seconds; the kernel sends ``SIGXCPU`` when CPU
    reaches the soft limit. Host ``/proc`` accounting is in jiffies
    (``1/SC_CLK_TCK``). ``_watch_process`` samples tree CPU once per
    ``watch_poll_interval_s`` before ``poll()``. At an integer RLIMIT edge
    the last sample can therefore lag by up to one full poll interval, and
    the sample itself is quantized to one jiffy. Formula:

    - ``min_measured`` = ``1 / SC_CLK_TCK`` (smallest positive jiffy sample)
    - ``slack`` = ``watch_poll_interval_s + 1/SC_CLK_TCK``
      (one watcher sample period + one jiffy of accounting quantization)
    """
    if isinstance(watch_poll_interval_s, bool) or not isinstance(
        watch_poll_interval_s, (int, float)
    ):
        raise TypeError(
            "watch_poll_interval_s must be a real number, "
            f"got {type(watch_poll_interval_s).__name__}"
        )
    poll = float(watch_poll_interval_s)
    if not math.isfinite(poll) or poll <= 0.0:
        raise ValueError(
            f"watch_poll_interval_s must be finite and > 0, got {watch_poll_interval_s!r}"
        )
    tick_s = 1.0 / _clk_tck()
    return poll + tick_s, tick_s


class ReapTimeoutError(Exception):
    """Process did not disappear within the configured reap budget.

    Not an ``OSError``/``TimeoutError`` subclass — must not be swallowed by
    ``except OSError`` host-io handlers. Carries ``kill_cause`` / ``timed_out``
    and any host CPU samples already taken when the reap followed a budget kill.
    """

    def __init__(
        self,
        message: str,
        *,
        kill_cause: str | None = None,
        timed_out: bool = False,
        overflow: bool = False,
        cpu_usage_s: float | None = None,
        rusage_cpu_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kill_cause = kill_cause
        self.timed_out = timed_out
        self.overflow = overflow
        self.cpu_usage_s = cpu_usage_s
        self.rusage_cpu_s = rusage_cpu_s


def is_plain_filename(name: str) -> bool:
    """True for a single path component: no separators / ``.`` / ``..`` / NUL."""
    return (
        bool(name)
        and name not in (".", "..")
        and "/" not in name
        and "\x00" not in name
    )


def agent_input_name_allowed(name: str, allowlist: frozenset[str]) -> bool:
    """Allowlist-only: exact token match (no path parts / denylist heuristics)."""
    return is_plain_filename(name) and name in allowlist


def require_isolation(value: object) -> Isolation:
    """Validate an isolation token at a boundary (no ``cast`` laundering)."""
    if value == "hard":
        return "hard"
    if value == "soft":
        return "soft"
    if value == "none":
        return "none"
    raise SandboxInfraError(f"sandbox_unavailable:bad_isolation_token:{value!r}")


def soft_isolation_note(net: Isolation, fs: Isolation) -> str | None:
    if isolation_fully_hard(net, fs):
        return None
    parts: list[str] = []
    if net != "hard":
        parts.append("network namespace not hard (bwrap --unshare-net unavailable)")
    if fs != "hard":
        parts.append("FS not mount-isolated (bwrap hard probe failed)")
    if not bwrap_available():
        parts.append("isolation unavailable (no bubblewrap)")
    return "isolation soft/unavailable: " + "; ".join(parts) + " — evidence not trusted"


_soft_note = soft_isolation_note("soft", "soft")
if _soft_note is None:
    raise RuntimeError("soft_isolation_note(soft, soft) must return a note")
SOFT_ISOLATION_NOTE = _soft_note


@dataclass(frozen=True)
class SandboxLimits:
    """Explicit sandbox budgets. Scientific + pid + security toggles required."""

    timeout_s: float
    memory_mb: int
    max_output_bytes: int
    # cgroup ``pids.max`` and wrapper ``RLIMIT_NPROC`` — required, no module default.
    # ``rlimit_nproc`` is per host UID (shared), not per jail; see module docstring.
    pids_max: int
    rlimit_nproc: int
    # Security toggles — required bools (no default / no bool() coerce at builders).
    allow_network: bool
    # When False: missing/failed cgroup limit application → unavailable.
    # When True: continue with ``cgroup_limits_held=False`` + note per missing limit.
    allow_cgroup_degraded: bool
    # Host post-exit SIGKILL reap budget (not derived from ``timeout_s``).
    reap_timeout_s: float
    # Shared join budget for both stdout/stderr drain threads (not derived
    # from ``timeout_s``).
    output_drain_join_s: float
    # SIGTERM→SIGKILL grace inside force-reap of the process group.
    term_grace_s: float
    # Host watcher / cgroup-membership poll cadence (not derived from wall).
    watch_poll_interval_s: float
    cgroup_probe_interval_s: float
    cgroup_probe_max_attempts: int
    reap_poll_interval_s: float
    # CPU budget relative to wall (cgroup watcher + RLIMIT fuse math).
    cpu_budget_wall_ratio: float
    cpu_budget_wall_margin_s: float
    cpu_budget_floor_s: float
    idle_cpu_wall_ratio: float
    rlimit_fuse_extra_ratio: float
    rlimit_fuse_extra_floor_s: float
    rlimit_fuse_wall_margin_s: float
    # cgroup v2 ``cpu.max`` ("quota period"); None → do not write cpu.max.
    cgroup_cpu_max: str | None
    # None → derived exactly in ``__post_init__`` from ``watch_poll_interval_s`` + tick.
    sigxcpu_slack_s: float | None = None
    sigxcpu_min_measured_s: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timeout_s", _require_positive_finite_seconds("timeout_s", self.timeout_s)
        )
        object.__setattr__(
            self, "memory_mb", _require_positive_int("memory_mb", self.memory_mb)
        )
        object.__setattr__(
            self,
            "max_output_bytes",
            _require_positive_int("max_output_bytes", self.max_output_bytes),
        )
        object.__setattr__(
            self, "pids_max", _require_positive_int("pids_max", self.pids_max)
        )
        object.__setattr__(
            self, "rlimit_nproc", _require_positive_int("rlimit_nproc", self.rlimit_nproc)
        )
        if not isinstance(self.allow_network, bool):
            raise TypeError(
                f"allow_network must be bool, got {type(self.allow_network).__name__}"
            )
        if not isinstance(self.allow_cgroup_degraded, bool):
            raise TypeError(
                "allow_cgroup_degraded must be bool, got "
                f"{type(self.allow_cgroup_degraded).__name__}"
            )
        if self.cgroup_cpu_max is not None and not isinstance(
            self.cgroup_cpu_max, str
        ):
            raise TypeError(
                "cgroup_cpu_max must be str | None, got "
                f"{type(self.cgroup_cpu_max).__name__}"
            )
        for name in (
            "reap_timeout_s",
            "output_drain_join_s",
            "term_grace_s",
            "watch_poll_interval_s",
            "cgroup_probe_interval_s",
            "reap_poll_interval_s",
            "cpu_budget_wall_margin_s",
            "cpu_budget_floor_s",
            "rlimit_fuse_extra_ratio",
            "rlimit_fuse_extra_floor_s",
            "rlimit_fuse_wall_margin_s",
        ):
            object.__setattr__(
                self, name, _require_positive_finite_seconds(name, getattr(self, name))
            )
        object.__setattr__(
            self,
            "cpu_budget_wall_ratio",
            _require_ratio_exclusive("cpu_budget_wall_ratio", self.cpu_budget_wall_ratio),
        )
        object.__setattr__(
            self,
            "idle_cpu_wall_ratio",
            _require_ratio_exclusive("idle_cpu_wall_ratio", self.idle_cpu_wall_ratio),
        )
        object.__setattr__(
            self,
            "cgroup_probe_max_attempts",
            _require_positive_int(
                "cgroup_probe_max_attempts", self.cgroup_probe_max_attempts
            ),
        )
        # SIGXCPU defaults from this instance's poll (never import-time constants).
        slack_floor, tick = _sigxcpu_from_poll(float(self.watch_poll_interval_s))
        if self.sigxcpu_min_measured_s is None:
            object.__setattr__(self, "sigxcpu_min_measured_s", tick)
        else:
            object.__setattr__(
                self,
                "sigxcpu_min_measured_s",
                _require_positive_finite_seconds(
                    "sigxcpu_min_measured_s", self.sigxcpu_min_measured_s
                ),
            )
        if self.sigxcpu_slack_s is None:
            object.__setattr__(self, "sigxcpu_slack_s", slack_floor)
        else:
            slack = _require_positive_finite_seconds(
                "sigxcpu_slack_s", self.sigxcpu_slack_s
            )
            if slack < slack_floor:
                raise ValueError(
                    f"sigxcpu_slack_s={slack!r} < watch_poll_interval_s+tick="
                    f"{slack_floor!r}"
                )
            object.__setattr__(self, "sigxcpu_slack_s", slack)
        wall = float(self.timeout_s)
        if self.cpu_budget_floor_s >= wall:
            raise ValueError(
                "cpu_budget_floor_s must be < timeout_s, got "
                f"floor={self.cpu_budget_floor_s!r} timeout_s={self.timeout_s!r}"
            )
        if self.cpu_budget_wall_margin_s >= wall:
            raise ValueError(
                "cpu_budget_wall_margin_s must be < timeout_s, got "
                f"margin={self.cpu_budget_wall_margin_s!r} timeout_s={self.timeout_s!r}"
            )
        # Derived CPU budget must already be >= floor — no silent raise-to-floor.
        derived = min(
            wall * float(self.cpu_budget_wall_ratio),
            wall - float(self.cpu_budget_wall_margin_s),
        )
        if derived < float(self.cpu_budget_floor_s):
            raise ValueError(
                "cpu budget "
                f"{derived!r} < cpu_budget_floor_s={self.cpu_budget_floor_s!r}; "
                "raise timeout_s or lower floor/margin/ratio"
            )
        # gate = threshold - slack must be >= min_measured for threshold >= floor.
        # __post_init__ always sets sigxcpu_* from poll+tick before this check.
        slack_v = self.sigxcpu_slack_s
        min_v = self.sigxcpu_min_measured_s
        if slack_v is None or min_v is None:
            raise ValueError(
                "sigxcpu_slack_s and sigxcpu_min_measured_s must be set "
                f"(got slack={slack_v!r} min={min_v!r})"
            )
        if float(self.cpu_budget_floor_s) - float(slack_v) < float(min_v):
            raise ValueError(
                "sigxcpu_min_measured_s + sigxcpu_slack_s must be "
                f"<= cpu_budget_floor_s (got min+slack="
                f"{float(min_v) + float(slack_v)!r}, "
                f"floor={self.cpu_budget_floor_s!r})"
            )


@dataclass(frozen=True)
class SandboxResult:
    status: SandboxStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    reason: str
    duration_s: float
    # Required: False when cgroup memory/cpu/pids were not fully applied.
    cgroup_limits_held: bool
    # Required — every builder states the isolation it actually resolved.
    net_isolation: Isolation
    fs_isolation: Isolation
    oom_detectable: bool
    # Absent-value fields: ``None`` / empty mean "the jail produced none".
    work_dir: str | None = None
    result_files: dict[str, bytes] = field(default_factory=dict)
    note: str | None = None
    # Host-measured CPU seconds for the cgroup tree (None if unavailable).
    cpu_usage_s: float | None = None
    # wall | cpu — which host budget tripped (None if not a timeout).
    timeout_kill: str | None = None
    # True when a stdout/stderr drain thread was still alive after the join budget
    # (e.g. grandchild still holding the write end) — do not trust empty sinks.
    output_drain_failed: bool = False
    # Idle ratio used for host classification (stamped from SandboxLimits).
    idle_cpu_wall_ratio: float | None = None
    # Explicit missing limit tags (e.g. ``pids``, ``cpu.max``, ``cgroup``).
    missing_limits: tuple[str, ...] = ()

    @property
    def limits_fully_held(self) -> bool:
        return (
            isolation_fully_hard(self.net_isolation, self.fs_isolation)
            and self.cgroup_limits_held
        )

    @property
    def is_guest_timeout(self) -> bool:
        return self.status is SandboxStatus.TIMEOUT and self.reason in TIMEOUT_KINDS


def limits_fully_held(
    net_isolation: Isolation,
    fs_isolation: Isolation = "none",
    *,
    cgroup_limits_held: bool,
) -> bool:
    """True only when net+fs are hard and cgroup limits were fully applied."""
    return isolation_fully_hard(net_isolation, fs_isolation) and cgroup_limits_held


@dataclass(frozen=True)
class _CgroupPrep:
    """Result of preparing a cgroup v2 leaf (or deciding none is available)."""

    leaf: Path | None
    applied: frozenset[str]
    missing: tuple[str, ...]
    # Non-None → must not run unless ``allow_cgroup_degraded``.
    fatal: str | None = None


def _require_positive_finite_seconds(name: str, value: object) -> float:
    """Reject bool/str coercion; require a finite float ``> 0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{name} must be a real number, got {type(value).__name__}"
        )
    v = float(value)
    if not math.isfinite(v) or v <= 0.0:
        raise ValueError(f"{name} must be finite and > 0, got {value!r}")
    return v


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return value


def _require_ratio_exclusive(name: str, value: object) -> float:
    """Finite ratio in ``(0, 1)``."""
    v = _require_positive_finite_seconds(name, value)
    if v >= 1.0:
        raise ValueError(f"{name} must be < 1, got {value!r}")
    return v


def force_reap_pid(
    pid: int,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> None:
    """SIGKILL then wait until the pid is reaped or ``/proc`` vanishes.

    Raises ``ReapTimeoutError`` if the pid is still present at the deadline
    (never clamps the poll interval or exits silently).
    """
    timeout_s = _require_positive_finite_seconds("timeout_s", timeout_s)
    poll_interval_s = _require_positive_finite_seconds(
        "poll_interval_s", poll_interval_s
    )
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + float(timeout_s)
    proc_path = f"/proc/{pid}"
    while True:
        try:
            wpid, _st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except ChildProcessError:
            if not os.path.isdir(proc_path):
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise ReapTimeoutError(
                f"pid {pid} still present after reap_timeout_s={timeout_s!r}",
            )
        # Sleep at most poll_interval_s, and never past the deadline.
        select_timeout = (
            poll_interval_s if poll_interval_s <= remaining else remaining
        )
        select.select([], [], [], select_timeout)


def _close_fd(fd: int, what: str) -> None:
    """Close ``fd``; a failure is logged (never swallowed silently)."""
    try:
        os.close(fd)
    except OSError as e:
        log.warning("sandbox close %s fd=%s: %s", what, fd, e)


def _killpg_sigkill(pid: int) -> str | None:
    """SIGKILL the process group; returns a note when the kill itself failed.

    ``ProcessLookupError`` means the group is already gone — the desired end
    state, not a failure.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except OSError as e:
        log.warning("sandbox killpg(%s): %s", pid, e)
        return f"killpg_oserror:{e.__class__.__name__}"
    return None


def _kill_group_term_then_kill(
    proc: subprocess.Popen[bytes],
    *,
    term_grace_s: float,
    reap_timeout_s: float,
) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=float(term_grace_s))
    except subprocess.TimeoutExpired:
        log.debug("sandbox pid=%s outlived SIGTERM grace; escalating", proc.pid)
    else:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=float(reap_timeout_s))
    except subprocess.TimeoutExpired as e:
        raise ReapTimeoutError(
            f"process group pid={proc.pid} still present after "
            f"reap_timeout_s={reap_timeout_s!r}",
        ) from e


def _read_capped(
    pipe: object | None,
    limit: int,
    sink: list[bytes],
    overflow: list[bool],
    drain_notes: list[str],
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
    except (OSError, ValueError) as e:
        # Closed/broken pipe while draining — keep bytes already read; note it.
        drain_notes.append(f"drain_{e.__class__.__name__}")
    sink.append(b"".join(chunks))


def _read_nofollow(path: Path) -> bytes:
    """Read a regular file without following symlinks (lstat + O_NOFOLLOW).

    Fail-closed: OSError / non-regular → ``SandboxInfraError`` (never soft None).
    """
    try:
        st = os.lstat(path)
        if not statmod.S_ISREG(st.st_mode):
            raise SandboxInfraError(
                f"sandbox_unavailable:not_regular_file:{path}"
            )
        # Linux always exposes O_NOFOLLOW; do not soft-getattr to 0.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:read_nofollow:{type(e).__name__}:{path}"
        ) from e


def _collect_results(results_dir: Path, names: Sequence[str] | None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not names:
        return out
    for name in names:
        if not is_plain_filename(name):
            raise SandboxInfraError(
                f"sandbox_unavailable:collect_name_rejected:{name!r}"
            )
        out[name] = _read_nofollow(results_dir / name)
    return out


def _cpu_budget_seconds(lim: SandboxLimits) -> float:
    """CPU budget for the whole cgroup tree — always strictly below wall.

    Ratios/floors/margins come from ``lim``. ``SandboxLimits.__post_init__``
    already rejects configs where the derived budget would be below the floor
    (no silent raise-to-floor here).
    """
    wall = float(lim.timeout_s)
    return min(
        wall * float(lim.cpu_budget_wall_ratio),
        wall - float(lim.cpu_budget_wall_margin_s),
    )


def _rlimit_cpu_seconds(lim: SandboxLimits, *, cgroup_live: bool) -> int | None:
    """Per-process RLIMIT_CPU in whole seconds.

    With a live cgroup: set RLIMIT **strictly above** the cgroup budget
    (``ceil`` of budget + configured fuse extra, or ``floor(budget)+1`` when
    that is larger) as a fuse only — the host watcher must win the race and
    classify ``cpu_timeout``. Without cgroup: RLIMIT at ``floor(budget)``
    (strictly below wall); exit SIGXCPU/152 is classified via host-measured
    child rusage (see ``_classify``).
    """
    wall = float(lim.timeout_s)
    budget = _cpu_budget_seconds(lim)
    if cgroup_live:
        extra = max(
            float(lim.rlimit_fuse_extra_floor_s),
            budget * float(lim.rlimit_fuse_extra_ratio),
        )
        fuse = budget + extra
        if fuse >= wall:
            fuse = max(
                budget + float(lim.rlimit_fuse_wall_margin_s),
                wall - float(lim.rlimit_fuse_wall_margin_s),
            )
        # Whole seconds must be strictly above budget (watcher wins below fuse).
        whole = max(math.ceil(fuse), math.floor(budget) + 1)
        wall_floor = math.floor(wall)
        if wall_floor < 1:
            return None
        if whole >= 1 and whole > budget and whole <= wall_floor:
            return whole
        return None
    whole = math.floor(budget)
    if whole >= 1 and whole < wall:
        return whole
    return None


def _is_sigkill_exit(code: int) -> bool:
    """True for raw -SIGKILL or shell-style 128+SIGKILL (137)."""
    return code in (-int(signal.SIGKILL), 128 + int(signal.SIGKILL))


def _is_sigxcpu_exit(code: int) -> bool:
    """True for raw -SIGXCPU or shell-style 128+SIGXCPU (152)."""
    return code in (-int(signal.SIGXCPU), 128 + int(signal.SIGXCPU))


_PROC_VANISHED_ERRNOS: frozenset[int] = frozenset({errno.ENOENT, errno.ESRCH})


def _proc_read_text(path: str) -> str | None:
    """Text of a ``/proc`` file; ``None`` only when the PID/task vanished.

    ENOENT/ESRCH mean the process was reaped mid-walk. Any other ``OSError``
    (EACCES, EIO, ...) is a host measurement failure → ``SandboxInfraError``.
    """
    try:
        return Path(path).read_text()
    except OSError as e:
        if e.errno in _PROC_VANISHED_ERRNOS:
            return None
        raise SandboxInfraError(
            f"sandbox_unavailable:host_tree_cpu:{type(e).__name__}"
        ) from e


def _proc_task_names(pid: int) -> list[str]:
    """Thread ids of ``pid``; empty only when the PID vanished."""
    try:
        return os.listdir(f"/proc/{pid}/task")
    except OSError as e:
        if e.errno in _PROC_VANISHED_ERRNOS:
            return []
        raise SandboxInfraError(
            f"sandbox_unavailable:host_tree_cpu:{type(e).__name__}"
        ) from e


def _host_tree_cpu_s(root_pid: int) -> float:
    """Sum utime+stime (seconds) for ``root_pid`` and descendants via ``/proc``.

    Used when cgroup ``cpu.stat`` is unavailable: bwrap guests are not the
    direct ``wait4`` child, so ``RUSAGE_CHILDREN`` under-counts. Sampling the
    host-visible process tree is the accurate host-side CPU measurement.

    ``SC_CLK_TCK`` failure raises (via ``_clk_tck``) — never silent ``None``.
    Host ``/proc`` errors other than a vanished PID (ENOENT/ESRCH) →
    ``SandboxInfraError`` (fail-closed). A ``task/*/children`` file that is
    absent while its task still exists (kernel without
    ``CONFIG_PROC_CHILDREN``) is infra too — descendants cannot be counted, so
    an under-counted CPU sample is never returned.

    On a readable but empty/idle ``/proc`` walk this returns ``0.0``.
    """
    clk = _clk_tck()
    pids: set[int] = {int(root_pid)}
    queue = [int(root_pid)]
    while queue:
        pid = queue.pop()
        for name in _proc_task_names(pid):
            text = _proc_read_text(f"/proc/{pid}/task/{name}/children")
            if text is None and Path(f"/proc/{pid}/task/{name}").exists():
                raise SandboxInfraError(
                    "sandbox_unavailable:host_tree_cpu:proc_children_unavailable"
                )
            if text is not None:
                for tok in text.split():
                    try:
                        cpid = int(tok)
                    except ValueError as e:
                        raise SandboxInfraError(
                            "sandbox_unavailable:host_tree_cpu:bad_children_token"
                        ) from e
                    if cpid not in pids:
                        pids.add(cpid)
                        queue.append(cpid)
    ticks = 0
    for pid in pids:
        raw = _proc_read_text(f"/proc/{pid}/stat")
        if raw is not None:
            # comm may contain spaces/parens — split after the last ") ".
            try:
                rest = raw[raw.rindex(")") + 2 :].split()
                # After ") ": state is rest[0], utime rest[11], stime rest[12].
                ticks += int(rest[11]) + int(rest[12])
            except (ValueError, IndexError) as e:
                raise SandboxInfraError(
                    f"sandbox_unavailable:host_tree_cpu:bad_stat:{pid}"
                ) from e
    return ticks / clk


def _wrapper_script(*, lim: SandboxLimits, cgroup_live: bool) -> str:
    """setrlimit, close infra FD, then exec.

    Host starts bwrap already inside the cgroup leaf (launcher). Rlimit failure
    writes magic to ``COSCIENTIST_INFRA_FD`` then aborts. Infra FD closed before
    exec so agent/test code never inherits it. With cgroup, RLIMIT_CPU is a
    fuse above the cgroup budget; without cgroup it matches the budget and
    classification uses host rusage.
    """
    rlimit = _rlimit_cpu_seconds(lim, cgroup_live=cgroup_live)
    memory_mb = int(lim.memory_mb)
    nproc = int(lim.rlimit_nproc)
    # No soft ``pass`` when RLIMIT_CPU cannot be set — abort like setrlimit fail.
    rlimit_cpu = (
        f"resource.setrlimit(resource.RLIMIT_CPU, ({rlimit}, {rlimit + 1}))"
        if rlimit is not None
        else "raise OSError('rlimit_cpu_unset')"
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
            pass  # guest wrapper: peer may already have closed; abort below
    sys.exit({_WRAPPER_ABORT_EXIT})
as_bytes = {int(memory_mb)} * 1024 * 1024
nproc = {nproc}
try:
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
    {rlimit_cpu}
except (ValueError, OSError):
    if infra_fd is not None:
        try:
            os.write(int(infra_fd), {_INFRA_RLIMIT!r})
        except OSError:
            pass  # guest wrapper: best-effort infra signal before abort
    sys.exit({_WRAPPER_ABORT_EXIT})
argv = sys.argv[1:]
if not argv:
    sys.exit(64)
# Never pass the infra FD to guest/agent code: close-on-exec keeps it writable
# for an exec-failure infra signal below, yet gone from any successful exec.
if infra_fd is not None:
    try:
        os.set_inheritable(int(infra_fd), False)
    except OSError:
        sys.exit({_WRAPPER_ABORT_EXIT})  # cannot guarantee FD isolation: abort
    os.environ.pop("COSCIENTIST_INFRA_FD", None)
try:
    os.execvp(argv[0], argv)
except MemoryError:
    # exec could not start under RLIMIT_AS: the jail failed, the guest never ran.
    if infra_fd is not None:
        try:
            os.write(int(infra_fd), {_INFRA_EXEC_MEMORY!r})
        except OSError:
            pass  # guest wrapper: peer closed; abort exit below still refuses
    sys.exit({_WRAPPER_ABORT_EXIT})
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
        pass  # host launcher: peer closed; exit code still signals attach fail

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


def _current_cgroup_path() -> Path:
    """Return this process's cgroup v2 path.

    Fail-closed: OSError reading ``/proc/self/cgroup`` and a missing ``0::``
    entry raise ``SandboxInfraError`` (never soft-``None`` / host root guess).
    """
    from coscientist.core.events.errors import SandboxInfraError

    base = Path("/sys/fs/cgroup")
    if not (base / "cgroup.controllers").is_file():
        raise SandboxInfraError("cgroup_v2_controllers_missing")
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError as e:
        raise SandboxInfraError(
            f"cgroup_read_proc:{type(e).__name__}"
        ) from e
    for line in text.splitlines():
        # v2: 0::/path
        if line.startswith("0::"):
            rel = line.split("::", 1)[1].lstrip("/")
            return base / rel if rel else base
    raise SandboxInfraError("cgroup_missing_v2_self_entry")


def _cleanup_note(what: str, path: Path, exc: OSError) -> str:
    """Record a best-effort cleanup failure (logged; never a silent pass)."""
    note = f"cleanup_failed:{what}:{path.name}:{type(exc).__name__}"
    log.warning("sandbox %s", note)
    return note


def _cgroup_remove_dir(path: Path) -> str | None:
    """Best-effort cgroup dir removal; returns a note when anything failed.

    cgroupfs control files cannot be unlinked (only ``rmdir`` works); the
    unlink pass exists for plain-dir fixtures and is reported at debug level.
    """
    notes: list[str] = []
    try:
        if path.exists():
            for child in path.iterdir():
                try:
                    child.unlink()
                except OSError as e:
                    log.debug("sandbox cgroup unlink %s: %s", child, e)
            path.rmdir()
    except OSError as e:
        notes.append(_cleanup_note("cgroup_rmdir", path, e))
    return ";".join(notes) if notes else None


def _cgroup_remove_probe(probe: Path) -> None:
    _cgroup_remove_dir(probe)


def _cgroup_can_host_leaf(parent: Path) -> bool:
    """True when ``parent`` can create a child with writable ``memory.max``.

    Probe I/O failures are infra errors (never soft-False).
    """
    from coscientist.core.events.errors import SandboxInfraError

    probe = parent / f"cos_sbx_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        probe.mkdir(exist_ok=False)
        (probe / "memory.max").write_text("1048576")
        return True
    except OSError as e:
        raise SandboxInfraError(
            f"cgroup_probe_host_leaf:{type(e).__name__}:{parent}"
        ) from e
    finally:
        _cgroup_remove_probe(probe)


def _cgroup_delegation_ceiling() -> Path:
    """Upper bound for nest walk.

    ``CGROUP_JOB`` is required (fail-closed). Unset / empty / non-dir → raise.
    """
    from coscientist.core.events.errors import SandboxInfraError

    raw = os.environ.get("CGROUP_JOB")
    if raw is None:
        raise SandboxInfraError("cgroup_job_required")
    env = raw
    if not env:
        raise SandboxInfraError("cgroup_job_empty")
    if env != env.strip():
        raise SandboxInfraError(f"cgroup_job_whitespace_padded:{env!r}")
    p = Path(env)
    if not p.is_dir():
        raise SandboxInfraError(f"cgroup_job_not_directory:{env!r}")
    return p.resolve()


def _cgroup_nest_root() -> Path:
    """Return the ``CGROUP_JOB`` directory when it can host a memory-limited child.

    CI places the process in ``…/ci-job/worker`` with ``CGROUP_JOB=…/ci-job`` so
    sandbox leaves nest as siblings under the job. ``CGROUP_JOB`` is required;
    outside the process tree / probe I/O failure → ``SandboxInfraError``
    (never soft-None / soft-False walk).
    """
    from coscientist.core.events.errors import SandboxInfraError

    cur = _current_cgroup_path()
    if not cur.is_dir():
        raise SandboxInfraError(f"cgroup_current_not_dir:{cur}")
    ceiling = _cgroup_delegation_ceiling()
    try:
        cur_r = cur.resolve()
        ceil_r = ceiling.resolve()
    except OSError as e:
        raise SandboxInfraError(
            f"cgroup_resolve:{type(e).__name__}"
        ) from e
    # Current must be at or under the ceiling.
    if cur_r != ceil_r and ceil_r not in cur_r.parents:
        raise SandboxInfraError(
            f"cgroup_job_outside_process_tree:job={ceil_r}:current={cur_r}"
        )
    # Probe the job ceiling only — OSError inside probe raises (no soft-False).
    _cgroup_can_host_leaf(ceil_r)
    return ceil_r


def _cgroup_delegation_available() -> bool:
    """True when ``CGROUP_JOB`` can host a memory.max child.

    Misconfiguration / probe failure raises ``SandboxInfraError`` — never
    soft-False. Callers that opt into degraded mode must catch.
    """
    _cgroup_nest_root()
    return True


def _cgroup_live_for_wrapper(*, allow_cgroup_degraded: bool) -> bool:
    """Wrapper ``cgroup_live`` flag: typed refuse unless degraded opt-in.

    Nest / ``CGROUP_JOB`` outside the process tree raises
    ``SandboxInfraError``. Degraded mode treats that as ``cgroup_live=False``
    (prepare_leaf applies the same fatal path); otherwise re-raise.
    """
    try:
        return _cgroup_delegation_available()
    except SandboxInfraError:
        if allow_cgroup_degraded:
            return False
        raise


def _cgroup_controller_set(raw: str) -> frozenset[str]:
    """Exact controller tokens from ``cgroup.controllers`` (space-separated).

    Substring checks are wrong: ``'cpu' in 'cpuset cpu'`` is True even when
    the ``cpu`` controller is absent.
    """
    return frozenset(tok for tok in raw.split() if tok)


def _cgroup_prepare_leaf(lim: SandboxLimits) -> _CgroupPrep:
    """Create a memory+cpu+pids-limited leaf under a delegated cgroup v2 subtree.

    Fail-closed: missing controllers or failed writes are recorded in
    ``missing`` / ``fatal`` — never skipped silently. Callers must refuse the
    run unless ``lim.allow_cgroup_degraded`` is set.

    When only non-memory limits are missing (e.g. ``pids``), the leaf is kept
    with the ``memory`` limit applied so degraded mode can still detect OOM.
    """
    from coscientist.core.events.errors import SandboxInfraError

    try:
        cur = _cgroup_nest_root()
    except RuntimeError as e:
        return _CgroupPrep(
            leaf=None,
            applied=frozenset(),
            missing=(MISSING_LIMIT_CGROUP,),
            fatal=f"cgroup_job:{e}",
        )
    except SandboxInfraError as e:
        return _CgroupPrep(
            leaf=None,
            applied=frozenset(),
            missing=(MISSING_LIMIT_CGROUP,),
            fatal=f"cgroup_job:{e}",
        )
    leaf = cur / f"cos_sbx_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    applied: set[str] = set()
    missing: list[str] = []
    try:
        leaf.mkdir(exist_ok=False)
        controllers = _cgroup_controller_set((cur / "cgroup.controllers").read_text())
        need_cpu = lim.cgroup_cpu_max is not None
        required = ["memory", "pids"] + (["cpu"] if need_cpu else [])
        enable: list[str] = []
        for name in required:
            if name in controllers:
                enable.append(f"+{name}")
            else:
                missing.append(name)
        if enable:
            try:
                (cur / "cgroup.subtree_control").write_text(" ".join(enable))
            except OSError:
                missing.append("subtree_control")
                _cgroup_remove_leaf(leaf)
                return _CgroupPrep(
                    leaf=None,
                    applied=frozenset(),
                    missing=tuple(dict.fromkeys(missing)),
                    fatal="subtree_control",
                )
        # memory_mb / pids_max already validated > 0 on SandboxLimits.
        try:
            (leaf / "memory.max").write_text(str(int(lim.memory_mb) * 1024 * 1024))
            applied.add("memory")
        except OSError:
            missing.append("memory")
            _cgroup_remove_leaf(leaf)
            return _CgroupPrep(
                leaf=None,
                applied=frozenset(),
                missing=tuple(dict.fromkeys(missing)),
                fatal="memory",
            )
        if "pids" in controllers:
            pids_file = leaf / "pids.max"
            if not pids_file.exists():
                missing.append("pids")
            else:
                try:
                    pids_file.write_text(str(int(lim.pids_max)))
                    applied.add("pids")
                except OSError:
                    missing.append("pids.max")
        # A missing ``pids`` controller is already recorded in ``missing`` above.
        # A missing ``cpu`` controller is already recorded in ``missing`` above.
        if need_cpu and "cpu" in controllers:
            if not (leaf / "cpu.max").exists():
                missing.append("cpu.max")
            else:
                try:
                    (leaf / "cpu.max").write_text(str(lim.cgroup_cpu_max))
                    applied.add("cpu")
                except OSError:
                    missing.append("cpu.max")
        missing_t = tuple(dict.fromkeys(missing))
        if not missing_t:
            return _CgroupPrep(
                leaf=leaf,
                applied=frozenset(applied),
                missing=(),
                fatal=None,
            )
        # Memory stayed applied — keep the leaf for degraded OOM; tear down only
        # when the single ``memory`` tag is among the missing (should not happen here).
        fatal = "+".join(missing_t)
        if "memory" in missing_t:
            _cgroup_remove_leaf(leaf)
            return _CgroupPrep(
                leaf=None,
                applied=frozenset(),
                missing=missing_t,
                fatal=fatal,
            )
        return _CgroupPrep(
            leaf=leaf,
            applied=frozenset(applied),
            missing=missing_t,
            fatal=fatal,
        )
    except OSError as e:
        cleanup = _cgroup_remove_leaf(leaf)
        fatal = f"cgroup_io:{e.__class__.__name__}"
        return _CgroupPrep(
            leaf=None,
            applied=frozenset(),
            missing=(MISSING_LIMIT_CGROUP,),
            fatal=f"{fatal};{cleanup}" if cleanup else fatal,
        )


def _cgroup_oom_killed(leaf: Path | None) -> bool:
    from coscientist.core.events.errors import SandboxInfraError

    if leaf is None:
        raise SandboxInfraError("cgroup_oom_events_missing_leaf")
    try:
        events = (leaf / "memory.events").read_text()
    except OSError as e:
        raise SandboxInfraError(
            f"cgroup_oom_events_read:{type(e).__name__}"
        ) from e
    for line in events.splitlines():
        if line.startswith("oom_kill"):
            try:
                return int(line.split()[-1]) > 0
            except ValueError as e:
                raise SandboxInfraError(
                    "cgroup_oom_events_parse"
                ) from e
    raise SandboxInfraError("cgroup_oom_events_missing_oom_kill")


def _cgroup_cpu_usage_usec(leaf: Path | None) -> int | None:
    """Total CPU time used by the leaf tree (microseconds), or None if no leaf."""
    from coscientist.core.events.errors import SandboxInfraError

    if leaf is None:
        return None
    try:
        text = (leaf / "cpu.stat").read_text()
    except OSError as e:
        raise SandboxInfraError(
            f"cgroup_cpu_stat_read:{type(e).__name__}"
        ) from e
    for line in text.splitlines():
        if line.startswith("usage_usec"):
            try:
                return int(line.split()[-1])
            except ValueError as e:
                raise SandboxInfraError("cgroup_cpu_stat_parse") from e
    raise SandboxInfraError("cgroup_cpu_stat_missing_usage_usec")


def _cgroup_remove_leaf(leaf: Path | None) -> str | None:
    """Remove a leaf (kernel may still hold procs); returns a failure note."""
    if leaf is None:
        return None
    return _cgroup_remove_dir(leaf)


def _result(**kw: object) -> SandboxResult:
    return SandboxResult(**kw)  # type: ignore[arg-type]


def _watch_process(
    proc: subprocess.Popen[bytes],
    *,
    lim: SandboxLimits,
    cgroup_leaf: Path | None = None,
) -> tuple[
    bool, bool, bytes, bytes, str | None, float | None, float | None, bool, str | None
]:
    """Return (timed_out, overflow, stdout, stderr, kill_cause, cpu_s, rusage_cpu_s,
    drain_failed, drain_note).

    ``kill_cause`` is ``wall`` or ``cpu`` when the host budget tripped.
    ``cpu_s`` is cgroup ``cpu.stat`` when available; ``rusage_cpu_s`` is the
    host-measured process-tree CPU (for no-cgroup SIGXCPU classification).
    ``drain_failed`` is True if a drain thread is still alive after the shared
    join budget (pipe held open, e.g. by a grandchild) — sinks are incomplete.
    ``drain_note`` records OSError/ValueError during pipe drain (never silent).
    """
    out_sink: list[bytes] = []
    err_sink: list[bytes] = []
    overflow = [False]
    drain_notes: list[str] = []
    # Reader threads append to sinks only after EOF/error — must join before
    # sampling sinks or a fast-exit guest races to empty stdout/stderr.
    out_t = threading.Thread(
        target=_read_capped,
        args=(proc.stdout, lim.max_output_bytes, out_sink, overflow, drain_notes),
        daemon=True,
    )
    err_t = threading.Thread(
        target=_read_capped,
        args=(proc.stderr, lim.max_output_bytes, err_sink, overflow, drain_notes),
        daemon=True,
    )
    out_t.start()
    err_t.start()
    timed_out = False
    kill_cause: str | None = None
    started = time.monotonic()
    deadline = started + float(lim.timeout_s)
    cpu_budget_usec = int(_cpu_budget_seconds(lim) * 1_000_000)
    last_usage: int | None = None
    tree_cpu_s: float | None = None

    def _cpu_snapshot() -> tuple[float | None, float | None]:
        cg = (last_usage / 1_000_000.0) if last_usage is not None else None
        return cg, tree_cpu_s

    def _kill_preserving_cause() -> None:
        try:
            _kill_group_term_then_kill(
                proc,
                term_grace_s=float(lim.term_grace_s),
                reap_timeout_s=float(lim.reap_timeout_s),
            )
        except ReapTimeoutError as e:
            # Keep timed_out/kill_cause/CPU samples; surface with context after wait.
            cg_s, tree_s = _cpu_snapshot()
            raise ReapTimeoutError(
                str(e),
                kill_cause=kill_cause,
                timed_out=timed_out,
                overflow=overflow[0],
                cpu_usage_s=cg_s,
                rusage_cpu_s=tree_s,
            ) from e

    while True:
        # Sample /proc *before* poll() — poll reaps the child and /proc vanishes.
        # Keep the max while alive so a late 0.0 (see _host_tree_cpu_s) cannot
        # erase an earlier high watermark used for SIGXCPU promotion.
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
            _kill_preserving_cause()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            kill_cause = "wall"
            _kill_preserving_cause()
            break
        usage = _cgroup_cpu_usage_usec(cgroup_leaf)
        if usage is not None:
            last_usage = usage
            if usage >= cpu_budget_usec:
                timed_out = True
                kill_cause = "cpu"
                _kill_preserving_cause()
                break
        time.sleep(float(lim.watch_poll_interval_s))
    try:
        proc.wait(timeout=float(lim.reap_timeout_s))
    except subprocess.TimeoutExpired as e:
        cg_s, tree_s = _cpu_snapshot()
        raise ReapTimeoutError(
            f"pid={proc.pid} still present after reap_timeout_s="
            f"{lim.reap_timeout_s!r}",
            kill_cause=kill_cause,
            timed_out=timed_out,
            overflow=overflow[0],
            cpu_usage_s=cg_s,
            rusage_cpu_s=tree_s,
        ) from e
    # Process exit alone does not mean readers have appended — join first.
    # One shared deadline for both threads (explicit limit, not sequential doubles).
    join_deadline = time.monotonic() + float(lim.output_drain_join_s)
    for t in (out_t, err_t):
        remaining = join_deadline - time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)
    drain_failed = out_t.is_alive() or err_t.is_alive()
    if last_usage is None:
        last_usage = _cgroup_cpu_usage_usec(cgroup_leaf)
    cpu_s = (last_usage / 1_000_000.0) if last_usage is not None else None
    drain_note = ";".join(drain_notes) if drain_notes else None
    return (
        timed_out,
        overflow[0],
        out_sink[0] if out_sink else b"",
        err_sink[0] if err_sink else b"",
        kill_cause,
        cpu_s,
        tree_cpu_s,
        drain_failed,
        drain_note,
    )


def classify_guest_timeout(
    *,
    kill_cause: str | None,
    duration_s: float,
    cpu_usage_s: float | None,
    idle_ratio: float,
) -> str:
    """Host-only classification: idle_timeout | cpu_timeout | wall_timeout.

    ``cpu_timeout`` when ``kill_cause==\"cpu\"`` (host cgroup watcher). Guest
    ``SIGXCPU`` / exit 152 is handled in ``_classify`` via host rusage (no
    cgroup) or cgroup measurement — never trusted from the exit code alone.

    Idle (cpu/wall below ``idle_ratio``) requires measured CPU. Missing CPU
    data or non-positive wall → ``SandboxInfraError`` (no role-based invent).
    ``idle_ratio`` is required (from ``SandboxLimits.idle_cpu_wall_ratio``).

    **CI gap:** the cgroup ``cpu.stat`` idle path is not exercised on CI when
    cgroup delegation / ``cpu.stat`` is unavailable (job often skips); unit
    coverage is ``test_classify_guest_timeout_idle_vs_busy`` only. Do not
    loosen the write p99 SLO to paper over runner noise.
    """
    idle_ratio = _require_positive_finite_seconds("idle_ratio", idle_ratio)
    if idle_ratio >= 1.0:
        raise ValueError(f"idle_ratio must be < 1, got {idle_ratio!r}")
    if kill_cause == "cpu":
        return "cpu_timeout"
    if cpu_usage_s is None:
        raise SandboxInfraError("sandbox_unavailable:missing_cpu_usage")
    wall = float(duration_s)
    if wall <= 0.0:
        raise SandboxInfraError("sandbox_unavailable:non_positive_wall")
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
    lim: SandboxLimits,
    cgroup_limits_held: bool,
    kill_cause: str | None = None,
    cpu_usage_s: float | None = None,
    rusage_cpu_s: float | None = None,
    output_drain_failed: bool = False,
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
        if "output_drain_failed" not in extra:
            extra = {**extra, "output_drain_failed": output_drain_failed}
        if "idle_cpu_wall_ratio" not in extra:
            extra = {**extra, "idle_cpu_wall_ratio": float(lim.idle_cpu_wall_ratio)}
        if "cgroup_limits_held" not in extra:
            extra = {**extra, "cgroup_limits_held": cgroup_limits_held}
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
        try:
            kind = infra_msg.split(b":", 1)[-1].strip().decode("ascii")
        except UnicodeDecodeError as e:
            raise SandboxInfraError(
                "sandbox_unavailable:infra_kind_not_ascii"
            ) from e
        if not kind:
            raise SandboxInfraError("sandbox_unavailable:empty_infra_kind")
        return fin(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=proc.returncode,
            reason=f"sandbox_unavailable:{kind}",
        )
    if overflow:
        # Guest-caused output flood — scientific, not infra (even if drain stuck).
        return fin(
            status=SandboxStatus.OUTPUT_EXCEEDED,
            exit_code=proc.returncode,
            reason="output_exceeded",
        )
    if proc.returncode is None:
        # Never invent exit_code=-1 — unreaped after kill is infra.
        raise SandboxInfraError("sandbox_unavailable:missing_returncode")
    code = proc.returncode
    budget = _cpu_budget_seconds(lim)
    if timed_out:
        # Guest timeout is NOT countable evidence — classified host-side only.
        # Keep TIMEOUT (attempt consumed) even when a drain thread is still alive.
        if kill_cause is None:
            raise SandboxInfraError("sandbox_unavailable:missing_kill_cause")
        # Measurement source is fixed by mode: cgroup ``cpu.stat`` when the leaf
        # is live, else the host-tree sample. Rebind so ``fin`` stamps it.
        if cgroup_leaf is None:
            cpu_usage_s = None if rusage_cpu_s is None else float(rusage_cpu_s)
        kind = classify_guest_timeout(
            kill_cause=kill_cause,
            duration_s=duration,
            cpu_usage_s=cpu_usage_s,
            idle_ratio=float(lim.idle_cpu_wall_ratio),
        )
        return fin(
            status=SandboxStatus.TIMEOUT,
            exit_code=code,
            reason=kind,
            timeout_kill=kill_cause,
        )
    # Drain still blocked with no guest timeout/overflow — incomplete sinks.
    if output_drain_failed:
        return fin(
            status=SandboxStatus.UNAVAILABLE,
            exit_code=proc.returncode,
            reason="sandbox_unavailable:output_drain",
            output_drain_failed=True,
        )
    if cgroup_leaf is not None and _cgroup_oom_killed(cgroup_leaf):
        return fin(
            status=SandboxStatus.OOM,
            exit_code=proc.returncode,
            reason="sandbox_oom",
        )
    # Without a verified memory cgroup an OOM kill is indistinguishable from a
    # guest SIGKILL: refuse (infra) rather than report a scientific failure.
    if not oom_detectable and _is_sigkill_exit(code):
        raise SandboxInfraError("sandbox_unavailable:oom_undetectable_sigkill")
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
            rlim = _rlimit_cpu_seconds(lim, cgroup_live=False)
            if rlim is None:
                raise SandboxInfraError("sandbox_unavailable:rlimit_cpu_unset")
            threshold = float(rlim)
        # Sampling lags the integer RLIMIT edge; accept measured CPU within the
        # configured slack of the threshold (still far above a forge≈0).
        # ``SandboxLimits.__post_init__`` guarantees floor - slack >= min_measured;
        # if a runtime threshold still undershoots, fail closed (no silent raise).
        min_raw = lim.sigxcpu_min_measured_s
        slack_raw = lim.sigxcpu_slack_s
        if min_raw is None or slack_raw is None:
            raise ValueError("sigxcpu slack/min not initialized on SandboxLimits")
        min_measured = float(min_raw)
        slack = float(slack_raw)
        gate = float(threshold) - slack
        if gate < min_measured:
            raise ValueError(
                f"sigxcpu gate {gate!r} < sigxcpu_min_measured_s={min_measured!r} "
                f"(threshold={threshold!r}, slack={slack!r})"
            )
        reached = measured is not None and float(measured) >= gate
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
    cgroup_limits_held: bool,
    stderr: bytes = b"",
    missing_limits: tuple[str, ...] = (),
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
        oom_detectable=False,
        note=note,
        cgroup_limits_held=cgroup_limits_held,
        missing_limits=missing_limits,
    )


def _missing_limits_note(missing: tuple[str, ...]) -> str:
    return "cgroup limits missing: " + ",".join(missing)


def _require_guest_ran_in_leaf(leaf: Path) -> None:
    """Pass only when ``pids.peak`` proves a descendant of bwrap lived in ``leaf``.

    ``pids.peak`` counts the launcher/bwrap process plus every forked guest
    process ever charged to the leaf, so ``>= 2`` means the jailed command
    started inside the cgroup. Unreadable / unparsable / too small → infra.
    """
    try:
        raw = (leaf / "pids.peak").read_text().strip()
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:guest_cgroup_membership_unverified:"
            f"pids_peak_{type(e).__name__}"
        ) from e
    try:
        peak = int(raw)
    except ValueError as e:
        raise SandboxInfraError(
            "sandbox_unavailable:guest_cgroup_membership_unverified:pids_peak_parse"
        ) from e
    if peak < MIN_PIDS_PEAK_FOR_GUEST:
        raise SandboxInfraError(
            "sandbox_unavailable:guest_cgroup_membership_unverified:"
            f"pids_peak={peak}"
        )


def _verify_guest_in_cgroup(
    leaf: Path | None,
    bwrap_pid: int,
    *,
    lim: SandboxLimits,
    bwrap_exited: Callable[[], bool],
) -> tuple[bool, str | None]:
    """oom_detectable only if memory.max applied and the guest is in the leaf.

    ``bwrap_pid`` is the launcher/bwrap host pid — must not count as the guest.
    Poll cadence/attempts come from ``lim`` (no magic sleep-loop literals).
    Fail-closed: unexpected memory.max / OSError / a live bwrap whose guest
    never shows up in the leaf → ``SandboxInfraError`` (never a soft
    ``oom_detectable=False`` that would let an OOM kill read as FAILED).
    ``leaf is None`` is only reachable under explicit ``allow_cgroup_degraded``.
    If bwrap already exited before the guest was observed, membership is proven
    via ``pids.peak`` (``_require_guest_ran_in_leaf``) or the run is infra.
    """
    if leaf is None:
        return False, "OOM not detectable"
    try:
        mem_raw = (leaf / "memory.max").read_text().strip()
        if not mem_raw or mem_raw == "max":
            raise SandboxInfraError(
                "sandbox_unavailable:oom_memory_max_not_applied"
            )
        for _ in range(int(lim.cgroup_probe_max_attempts)):
            try:
                procs = {
                    int(x) for x in (leaf / "cgroup.procs").read_text().split()
                }
            except ValueError as e:
                raise SandboxInfraError(
                    "sandbox_unavailable:cgroup_procs_bad_token"
                ) from e
            guests = procs - {int(bwrap_pid)}
            if guests:
                return True, None
            if bwrap_exited():
                # Fast guest: exited before it was ever observed in the leaf.
                # Membership must be proven from the kernel's pid high-water
                # mark (launcher + at least one forked descendant) — never
                # assumed. Missing/short ``pids.peak`` → infra.
                _require_guest_ran_in_leaf(leaf)
                return True, None
            time.sleep(float(lim.cgroup_probe_interval_s))
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:oom_detect:{type(e).__name__}"
        ) from e
    raise SandboxInfraError(
        "sandbox_unavailable:guest_cgroup_membership_unverified"
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
    """Spawn bwrap already inside a cgroup leaf + wrapper private infra FD."""
    note = soft_isolation_note(net_i, fs_i)
    prep = _cgroup_prepare_leaf(lim)
    leaf = prep.leaf
    missing_limits = prep.missing
    cgroup_held = not prep.missing and prep.fatal is None and leaf is not None
    if prep.fatal is not None or prep.missing:
        # Structured missing tags only — never invent from fatal string alone.
        if not prep.missing:
            raise SandboxInfraError(
                "sandbox_unavailable:cgroup_limits:fatal_without_missing:"
                f"{prep.fatal}"
            )
        missing_tags: tuple[str, ...] = prep.missing
        miss_note = _missing_limits_note(missing_tags)
        if not lim.allow_cgroup_degraded:
            if leaf is not None:
                _cgroup_remove_leaf(leaf)
            if prep.fatal is None:
                raise SandboxInfraError(
                    "sandbox_unavailable:cgroup_limits:missing_without_fatal:"
                    f"{missing_tags}"
                )
            return _unavailable(
                reason=f"sandbox_unavailable:cgroup_limits:{prep.fatal}",
                net_i=net_i,
                fs_i=fs_i,
                note=f"{note}; {miss_note}" if note else miss_note,
                cgroup_limits_held=False,
                missing_limits=missing_tags,
            )
        log.warning(
            "sandbox cgroup degraded mode: %s (applied=%s leaf=%s)",
            miss_note,
            sorted(prep.applied),
            leaf,
        )
        note = f"{note}; {miss_note}" if note else miss_note
        cgroup_held = False
        missing_limits = missing_tags
        # Keep prep.leaf when memory.max was applied (pids/cpu-only gaps).

    r_fd, w_fd = os.pipe()
    os.set_inheritable(w_fd, True)
    os.set_inheritable(r_fd, False)

    env_pairs: list[tuple[str, str]] = [("COSCIENTIST_INFRA_FD", str(w_fd))]
    if extra_env:
        env_pairs.extend((str(k), str(v)) for k, v in extra_env.items())

    if "--" not in cmd:
        _close_fd(r_fd, "bad_argv_read")
        _close_fd(w_fd, "bad_argv_write")
        return _unavailable(
            reason="sandbox_unavailable:bad_argv",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=cgroup_held,
            missing_limits=missing_limits,
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
        oom_note = "OOM not detectable"
        note = f"{note}; {oom_note}" if note else oom_note

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
            # ``finally`` below closes ``w_fd`` exactly once.
            return _unavailable(
                reason=f"sandbox_unavailable:{e.__class__.__name__}",
                net_i=net_i,
                fs_i=fs_i,
                note=note,
                stderr=str(e).encode(),
                cgroup_limits_held=cgroup_held,
                missing_limits=missing_limits,
            )
        finally:
            _close_fd(w_fd, "write_end_after_handoff")

        try:
            oom_det, oom_frag = _verify_guest_in_cgroup(
                leaf,
                proc.pid,
                lim=lim,
                bwrap_exited=lambda: proc.poll() is not None,
            )
        except SandboxInfraError:
            _killpg_sigkill(proc.pid)
            try:
                proc.wait(timeout=float(lim.reap_timeout_s))
            except (subprocess.TimeoutExpired, OSError):
                log.warning("sandbox reap after unverified guest failed")
            _close_fd(r_fd, "unverified_guest_read")
            raise
        if oom_frag:
            note = f"{note}; {oom_frag}" if note else oom_frag

        try:
            (
                timed_out,
                overflow,
                out_b,
                err_b,
                kill_cause,
                cpu_s,
                rusage_s,
                drain_failed,
                drain_note,
            ) = _watch_process(proc, lim=lim, cgroup_leaf=leaf if oom_det else None)
            if drain_note:
                note = f"{note}; {drain_note}" if note else drain_note
        except ReapTimeoutError as e:
            # Preserve host kill_cause / timed_out — never map to host_io OSError.
            # Follow-up killpg/wait failures are recorded (never bare ``pass``).
            follow_note = _killpg_sigkill(proc.pid)
            if follow_note is None:
                try:
                    proc.wait(timeout=float(lim.reap_timeout_s))
                except subprocess.TimeoutExpired:
                    follow_note = "killpg_wait_timeout"
                    log.warning(
                        "sandbox wait after killpg timed out (reap_timeout_s=%r)",
                        lim.reap_timeout_s,
                    )
                except OSError as wait_err:
                    follow_note = f"killpg_wait_oserror:{wait_err.__class__.__name__}"
                    log.warning("sandbox wait after killpg: %s", wait_err)
            if follow_note:
                note = f"{note}; {follow_note}" if note else follow_note
            _close_fd(r_fd, "reap_timeout_read")
            if not e.timed_out and not e.overflow:
                return _unavailable(
                    reason="sandbox_unavailable:reap",
                    net_i=net_i,
                    fs_i=fs_i,
                    note=(f"{note}; reap_timeout" if note else "reap_timeout"),
                    cgroup_limits_held=cgroup_held,
                    missing_limits=missing_limits,
                )
            if not isinstance(e.timed_out, bool) or not isinstance(e.overflow, bool):
                raise SandboxInfraError(
                    "sandbox_unavailable:reap_flags_not_bool:"
                    f"timed_out={type(e.timed_out).__name__},"
                    f"overflow={type(e.overflow).__name__}"
                ) from e
            timed_out = e.timed_out
            overflow = e.overflow
            kill_cause = e.kill_cause
            out_b, err_b = b"", b""
            # Preserve host CPU samples taken before the stuck reap (no invent).
            cpu_s, rusage_s = e.cpu_usage_s, e.rusage_cpu_s
            drain_failed = True
            note = f"{note}; reap_timeout" if note else "reap_timeout"
            r_fd = -1
        infra_msg = b""
        if r_fd >= 0:
            try:
                if select.select([r_fd], [], [], 0)[0]:
                    infra_msg = os.read(r_fd, 256)
            except OSError as e:
                raise SandboxInfraError(
                    f"sandbox_unavailable:infra_fd_read:{type(e).__name__}"
                ) from e
            finally:
                _close_fd(r_fd, "infra_channel_read")
        # Empty kind after magic is infra (handled in _classify); bare magic OK.

        files: dict[str, bytes] = {}
        if result_files_dir is not None and collect:
            files = _collect_results(result_files_dir, collect)
        result = _classify(
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
            lim=lim,
            cgroup_limits_held=cgroup_held,
            kill_cause=kill_cause,
            cpu_usage_s=cpu_s,
            rusage_cpu_s=rusage_s,
            output_drain_failed=drain_failed,
        )
        # Preserve degraded-cgroup notes collected before classify.
        # Handle None without inventing an empty string for membership.
        merged_note = result.note
        if note:
            if merged_note is None:
                merged_note = note
            elif note not in merged_note:
                merged_note = f"{note}; {merged_note}"
        return SandboxResult(
            status=result.status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            reason=result.reason,
            duration_s=result.duration_s,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            work_dir=result.work_dir,
            result_files=result.result_files,
            oom_detectable=result.oom_detectable,
            note=merged_note,
            cpu_usage_s=result.cpu_usage_s,
            timeout_kill=result.timeout_kill,
            output_drain_failed=result.output_drain_failed,
            idle_cpu_wall_ratio=result.idle_cpu_wall_ratio,
            cgroup_limits_held=cgroup_held,
            missing_limits=missing_limits,
        )
    finally:
        _cgroup_remove_leaf(leaf)
        if launcher_path is not None:
            _rmtree_best_effort(launcher_path.parent, why="launcher_workdir")


def run_sandbox(
    argv: Sequence[str],
    *,
    limits: SandboxLimits,
    inputs: Mapping[str, bytes],
    env: Mapping[str, str],
    cwd: Path | str | None = None,
    core_files: Mapping[str, bytes] | None = None,
    collect_core: Sequence[str] | None = None,
    protect_paths: Sequence[str] | None = None,
) -> SandboxResult:
    """Agent jail. Journal/CAS are not mounted.

    Fail-closed without hard FS isolation — no guest exec.
    ``limits``, ``inputs``, and ``env`` are required (pass ``{}`` if empty).
    Unsupported kwargs (``core_files`` / ``collect_core`` / ``protect_paths``)
    must be omitted — present values raise ``TypeError``.
    """
    if core_files is not None or collect_core is not None or protect_paths is not None:
        raise TypeError(
            "run_sandbox: core_files/collect_core/protect_paths are not supported"
        )
    if not isinstance(inputs, Mapping):
        raise TypeError(
            f"run_sandbox: inputs must be a Mapping, got {type(inputs).__name__}"
        )
    if not isinstance(env, Mapping):
        raise TypeError(
            f"run_sandbox: env must be a Mapping, got {type(env).__name__}"
        )
    lim = limits
    net_i, fs_i = resolve_isolation(allow_network=lim.allow_network)
    note = soft_isolation_note(net_i, fs_i)

    if not argv:
        return _unavailable(
            reason="sandbox_unavailable:empty_argv",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=False,
        )

    # Fail-closed: soft / no bwrap → do not execute agent code.
    if fs_i != "hard":
        return _unavailable(
            reason="sandbox_unavailable:isolation",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=False,
        )

    # ``cwd=None`` → private temp dir owned (and removed) by this call.
    if cwd is None:
        own = True
        work = Path(tempfile.mkdtemp(prefix="cos_agent_"))
    else:
        own = False
        work = Path(cwd)
    try:
        work.mkdir(parents=True, exist_ok=True)
        for name, data in inputs.items():
            if not is_plain_filename(name):
                raise SandboxInfraError(
                    f"sandbox_unavailable:input_name_rejected:{name!r}"
                )
            p = work / name
            p.write_bytes(data)
            p.chmod(0o444)
        wrapper = work / "_cos_wrapper.py"
        wrapper.write_text(
            _wrapper_script(
                lim=lim,
                cgroup_live=_cgroup_live_for_wrapper(
                    allow_cgroup_degraded=lim.allow_cgroup_degraded
                ),
            ),
            encoding="utf-8",
        )
        extra_env = [(str(k), str(v)) for k, v in env.items()]
        prefix = build_agent_bwrap(
            work=work, allow_network=lim.allow_network, extra_env=extra_env
        )
        cmd = [
            *prefix,
            "--",
            GUEST_PYTHON_ARGV0,
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
    except ReapTimeoutError:
        # Must not fall through to OSError (TimeoutError⊂OSError historically).
        raise
    except OSError as e:
        return _unavailable(
            reason=f"sandbox_unavailable:host_io:{e.__class__.__name__}",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=False,
            stderr=str(e).encode(),
        )
    finally:
        if own:
            _rmtree_best_effort(Path(work), why="run_sandbox_work")


def run_verifier(
    scorer_argv: Sequence[str],
    *,
    inputs: Mapping[str, bytes],
    limits: SandboxLimits,
    collect: Sequence[str],
    holdout: bytes | None = None,
    scorer_bytes: bytes | None = None,
    tests: Mapping[str, bytes] | None = None,
    extra_ro_files: Mapping[str, bytes],
    input_allowlist: frozenset[str],
) -> SandboxResult:
    """Catalog verifier in a fresh jail. Agent outputs/tests RO; only results RW.

    Reads ``collect`` via O_NOFOLLOW — never stdout. No agent code as entrypoint.
    Fail-closed without hard isolation. ``input_allowlist`` is required and
    allowlist-only (empty → every agent input name is refused). ``extra_ro_files``
    is required (pass ``{}`` for none). ``limits`` and ``collect`` are required.
    Network isolation follows ``limits.allow_network``.
    """
    lim = limits
    net_i, fs_i = resolve_isolation(allow_network=lim.allow_network)
    note = soft_isolation_note(net_i, fs_i)
    allow = input_allowlist

    if not scorer_argv:
        return _unavailable(
            reason="sandbox_unavailable:missing_scorer",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=False,
        )

    if fs_i != "hard":
        return _unavailable(
            reason="sandbox_unavailable:isolation",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=False,
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
            if not agent_input_name_allowed(name, allow):
                raise SandboxInfraError(
                    f"sandbox_unavailable:input_name_rejected:{name!r}"
                )
            p = inputs_dir / name
            p.write_bytes(data)
            p.chmod(0o444)
        for name, data in extra_ro_files.items():
            if not agent_input_name_allowed(name, allow):
                raise SandboxInfraError(
                    f"sandbox_unavailable:extra_input_name_rejected:{name!r}"
                )
            p = inputs_dir / name
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
                    raise SandboxInfraError(
                        f"sandbox_unavailable:bad_tests_path:{name!r}"
                    )
                dest = tests_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest.chmod(0o444)
            tests_path = tests_dir

        wrapper = results_dir / "_cos_wrapper.py"
        wrapper.write_text(
            _wrapper_script(
                lim=lim,
                cgroup_live=_cgroup_live_for_wrapper(
                    allow_cgroup_degraded=lim.allow_cgroup_degraded
                ),
            ),
            encoding="utf-8",
        )

        # Exact remap of the host scorer path only — argv already carries the
        # guest path token (``catalog.VERIFIER_SCORER_GUEST_PATH``), no alias.
        guest_argv = [str(a) for a in scorer_argv]
        if scorer_path is not None:
            host_scorer = str(scorer_path)
            guest_argv = [
                VERIFIER_SCORER_GUEST_PATH if a == host_scorer else a
                for a in guest_argv
            ]
        prefix = build_verifier_bwrap(
            results=results_dir,
            inputs=inputs_dir,
            scorer=scorer_path,
            holdout=holdout_path,
            tests=tests_path,
            allow_network=lim.allow_network,
        )
        cmd = [
            *prefix,
            "--",
            GUEST_PYTHON_ARGV0,
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
    except ReapTimeoutError:
        raise
    except OSError as e:
        return _unavailable(
            reason=f"sandbox_unavailable:host_io:{e.__class__.__name__}",
            net_i=net_i,
            fs_i=fs_i,
            note=note,
            cgroup_limits_held=False,
            stderr=str(e).encode(),
        )
    finally:
        _rmtree_best_effort(root, why="run_verifier_root")


def _rmtree_best_effort(path: Path, *, why: str) -> None:
    """Best-effort tree delete; log OSError (tmpdir may already be gone)."""
    import shutil

    try:
        shutil.rmtree(path)
    except OSError as e:
        logging.getLogger(__name__).warning(
            "sandbox_rmtree_failed:%s:%s:%s", why, path, e.__class__.__name__
        )


def limits_from_check(
    *,
    timeout_s: float,
    memory_mb: int,
    max_output_bytes: int,
    pids_max: int,
    rlimit_nproc: int,
    allow_network: bool,
    allow_cgroup_degraded: bool,
    reap_timeout_s: float,
    output_drain_join_s: float,
) -> SandboxLimits:
    """Build limits from a frozen CodeCheck. Every argument is required (non-Optional).

    Wrong types → ``CatalogError`` (fail-closed). Host tuning comes from
    ``catalog.SANDBOX_*``; ``SandboxLimits`` itself has no policy defaults.
    """
    from coscientist.core.events.errors import CatalogError

    if not isinstance(allow_network, bool):
        raise CatalogError(
            f"sandbox_limits_bad_toggle:allow_network:{type(allow_network).__name__}"
        )
    if not isinstance(allow_cgroup_degraded, bool):
        raise CatalogError(
            "sandbox_limits_bad_toggle:allow_cgroup_degraded:"
            f"{type(allow_cgroup_degraded).__name__}"
        )
    for name, v, typ in (
        ("timeout_s", timeout_s, float),
        ("memory_mb", memory_mb, int),
        ("max_output_bytes", max_output_bytes, int),
        ("pids_max", pids_max, int),
        ("rlimit_nproc", rlimit_nproc, int),
        ("reap_timeout_s", reap_timeout_s, float),
        ("output_drain_join_s", output_drain_join_s, float),
    ):
        # Strict: float fields require float (not int); int fields require int
        # (not bool). No coercion.
        if isinstance(v, bool) or not isinstance(v, typ):
            raise CatalogError(
                f"sandbox_limits_bad_type:{name}:{type(v).__name__}"
            )
    return SandboxLimits(
        timeout_s=timeout_s,
        memory_mb=memory_mb,
        max_output_bytes=max_output_bytes,
        pids_max=pids_max,
        rlimit_nproc=rlimit_nproc,
        allow_network=allow_network,
        allow_cgroup_degraded=allow_cgroup_degraded,
        reap_timeout_s=reap_timeout_s,
        output_drain_join_s=output_drain_join_s,
        term_grace_s=SANDBOX_TERM_GRACE_S,
        watch_poll_interval_s=SANDBOX_WATCH_POLL_INTERVAL_S,
        cgroup_probe_interval_s=SANDBOX_CGROUP_PROBE_INTERVAL_S,
        cgroup_probe_max_attempts=SANDBOX_CGROUP_PROBE_MAX_ATTEMPTS,
        reap_poll_interval_s=SANDBOX_REAP_POLL_INTERVAL_S,
        cpu_budget_wall_ratio=SANDBOX_CPU_BUDGET_WALL_RATIO,
        cpu_budget_wall_margin_s=SANDBOX_CPU_BUDGET_WALL_MARGIN_S,
        cpu_budget_floor_s=SANDBOX_CPU_BUDGET_FLOOR_S,
        idle_cpu_wall_ratio=SANDBOX_IDLE_CPU_WALL_RATIO,
        rlimit_fuse_extra_ratio=SANDBOX_RLIMIT_FUSE_EXTRA_RATIO,
        rlimit_fuse_extra_floor_s=SANDBOX_RLIMIT_FUSE_EXTRA_FLOOR_S,
        rlimit_fuse_wall_margin_s=SANDBOX_RLIMIT_FUSE_WALL_MARGIN_S,
        cgroup_cpu_max=SANDBOX_CGROUP_CPU_MAX,
    )
