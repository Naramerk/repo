"""Shared explicit sandbox budgets for check tests (no production DEFAULT_*)."""

from __future__ import annotations

from coscientist.checks import catalog as cat
from coscientist.checks.sandbox import SandboxLimits

# Catalog / evaluate defaults injected by ``_catalog_with``.
TEST_TIMEOUT_S = 30.0
TEST_MEMORY_MB = 256
TEST_MAX_OUTPUT_BYTES = 1_000_000
TEST_PIDS_MAX = 256
TEST_RLIMIT_NPROC = 256
# Per-check host post-exit budgets carried by catalog executable checks.
TEST_CHECK_REAP_TIMEOUT_S = 5.0
TEST_CHECK_DRAIN_JOIN_S = 5.0

# Host-watch / unit-test budgets (not catalog scientific defaults).
TEST_WALL_TIMEOUT_S = 2.0
TEST_REAP_TIMEOUT_S = 0.25
TEST_DRAIN_JOIN_S = 0.25
TEST_LARGE_WALL_TIMEOUT_S = 30.0
TEST_READY_TIMEOUT_S = 2.0
TEST_UNIT_MEMORY_MB = 64
TEST_UNIT_MAX_OUTPUT_BYTES = 1024

# Explicit non-empty freeze row for TaskCreated journals (v14 refuses empty {}).
EXEC_OK_FREEZE_ROW: dict[str, object] = {
    "timeout_s": 5.0,
    "memory_mb": 64,
    "max_output_bytes": 1024,
    "pids_max": 16,
    "rlimit_nproc": 16,
    "reap_timeout_s": 1.0,
    "output_drain_join_s": 1.0,
    "allow_network": False,
    "allow_cgroup_degraded": False,
}


def exec_ok_freeze_row(**overrides: object) -> dict[str, object]:
    """Complete ``exec_ok`` freeze dict for tests (no soft ``{}`` pin)."""
    row = dict(EXEC_OK_FREEZE_ROW)
    row.update(overrides)
    return row


def exec_ok_freeze_map(**overrides: object) -> dict[str, dict[str, object]]:
    """``{\"exec_ok\": <complete freeze row>}`` for TaskSpec / upcast fixtures."""
    return {"exec_ok": exec_ok_freeze_row(**overrides)}


def sandbox_limits(
    *,
    timeout_s: float,
    memory_mb: int,
    max_output_bytes: int,
    pids_max: int = TEST_PIDS_MAX,
    rlimit_nproc: int = TEST_RLIMIT_NPROC,
    allow_network: bool = False,
    allow_cgroup_degraded: bool = True,
    **overrides: object,
) -> SandboxLimits:
    """``SandboxLimits`` with required pid budgets filled when omitted.

    Test helper defaults ``allow_cgroup_degraded=True`` so hosts/CI without
    cgroup delegation can still exercise bwrap paths. ``SandboxLimits`` /
    ``CodeCheck`` have no default for that flag (the catalog freezes it
    explicitly). Pass ``allow_cgroup_degraded=False`` explicitly in
    fail-closed cgroup tests.
    """
    fields: dict[str, object] = {
        "timeout_s": timeout_s,
        "memory_mb": memory_mb,
        "max_output_bytes": max_output_bytes,
        "pids_max": pids_max,
        "rlimit_nproc": rlimit_nproc,
        "allow_network": allow_network,
        "allow_cgroup_degraded": allow_cgroup_degraded,
        "reap_timeout_s": TEST_CHECK_REAP_TIMEOUT_S,
        "output_drain_join_s": TEST_CHECK_DRAIN_JOIN_S,
        "term_grace_s": cat.SANDBOX_TERM_GRACE_S,
        "watch_poll_interval_s": cat.SANDBOX_WATCH_POLL_INTERVAL_S,
        "cgroup_probe_interval_s": cat.SANDBOX_CGROUP_PROBE_INTERVAL_S,
        "cgroup_probe_max_attempts": cat.SANDBOX_CGROUP_PROBE_MAX_ATTEMPTS,
        "reap_poll_interval_s": cat.SANDBOX_REAP_POLL_INTERVAL_S,
        "cpu_budget_wall_ratio": cat.SANDBOX_CPU_BUDGET_WALL_RATIO,
        "cpu_budget_wall_margin_s": cat.SANDBOX_CPU_BUDGET_WALL_MARGIN_S,
        "cpu_budget_floor_s": cat.SANDBOX_CPU_BUDGET_FLOOR_S,
        "idle_cpu_wall_ratio": cat.SANDBOX_IDLE_CPU_WALL_RATIO,
        "rlimit_fuse_extra_ratio": cat.SANDBOX_RLIMIT_FUSE_EXTRA_RATIO,
        "rlimit_fuse_extra_floor_s": cat.SANDBOX_RLIMIT_FUSE_EXTRA_FLOOR_S,
        "rlimit_fuse_wall_margin_s": cat.SANDBOX_RLIMIT_FUSE_WALL_MARGIN_S,
        "cgroup_cpu_max": cat.SANDBOX_CGROUP_CPU_MAX,
    }
    fields.update(overrides)
    return SandboxLimits(**fields)  # type: ignore[arg-type]
