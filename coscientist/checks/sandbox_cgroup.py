"""cgroup v2 leaf prepare/teardown and delegation probes (sandbox jail).

Extracted from ``sandbox`` without behavior change. Host run/watch/classify
paths stay in ``sandbox``; they call into this module by attribute so tests
can monkeypatch here.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from coscientist.core.events.errors import SandboxInfraError

if TYPE_CHECKING:
    from coscientist.checks.sandbox import SandboxLimits

log = logging.getLogger(__name__)

# Wrapper private-channel magic for cgroup attach failure (FD, not exit code).
_INFRA_CGROUP_ATTACH = b"COS_SBX_INFRA_v1:cgroup_attach\n"

# ``missing_limits`` tag when the cgroup leaf itself could not be prepared.
MISSING_LIMIT_CGROUP = "cgroup"


@dataclass(frozen=True)
class _CgroupPrep:
    """Result of preparing a cgroup v2 leaf (or deciding none is available)."""

    leaf: Path | None
    applied: frozenset[str]
    missing: tuple[str, ...]
    # Non-None → must not run unless ``allow_cgroup_degraded``.
    fatal: str | None = None


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


def require_cgroup_delegation() -> None:
    """Public fail-closed probe: ``CGROUP_JOB`` must host a memory-limited child.

    Raises ``SandboxInfraError`` on misconfiguration / probe failure.
    No degraded bypass — callers that need soft failure must catch explicitly.
    """
    _cgroup_delegation_available()


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
