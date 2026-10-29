"""cgroup v2 launcher, leaf teardown, and capacity probes (sandbox jail).

Extracted from ``sandbox`` without behavior change. Host run/watch/classify
paths stay in ``sandbox``; they call into this module by attribute so tests
can monkeypatch here.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Wrapper private-channel magic for cgroup attach failure (FD, not exit code).
_INFRA_CGROUP_ATTACH = b"COS_SBX_INFRA_v1:cgroup_attach\n"


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



def _cgroup_remove_leaf(leaf: Path | None) -> str | None:
    """Remove a leaf (kernel may still hold procs); returns a failure note."""
    if leaf is None:
        return None
    return _cgroup_remove_dir(leaf)

