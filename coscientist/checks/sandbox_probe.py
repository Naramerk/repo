"""Cached bubblewrap probes and argv builders (M1 PR17).

Hard isolation requires a real bwrap probe (host paths invisible, net down).
No bwrap / probe fail → isolation unavailable; callers fail-closed (no guest exec).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping, Sequence

from coscientist.checks.catalog import (
    PROBE_EXIT_HOST_VISIBLE,
    PROBE_EXIT_NET_UP,
    PROBE_HOST_CANARY_PATHS,
    PROBE_NET_CANARY_HOST,
    PROBE_NET_CANARY_PORT,
    PROBE_NET_CONNECT_TIMEOUT_S,
    PROBE_SUBPROCESS_TIMEOUT_S,
    VERIFIER_SCORER_GUEST_PATH,
)
from coscientist.core.events.errors import SandboxInfraError

log = logging.getLogger(__name__)

Isolation = Literal["hard", "soft", "none"]

# Isolation claimed when the hard probe did not pass (keyed by ``allow_network``):
# network is "none" when the caller asked for it, else "soft"; fs is always soft.
_NET_ISOLATION_WITHOUT_HARD: Mapping[bool, Isolation] = {True: "none", False: "soft"}

# Explicit allowlist after --clearenv (never pass host secrets / full environ).
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONUNBUFFERED",
    "PYTHONPATH",
    "COSCIENTIST_INPUTS",
    "COSCIENTIST_RESULTS",
    "COSCIENTIST_HOLDOUT",
    "COSCIENTIST_PREDICTIONS",
    "COSCIENTIST_SCORER",
    "COSCIENTIST_TESTS",
    "COSCIENTIST_INFRA_FD",
    "COSCIENTIST_CGROUP_GATE",
)


def _common_jail_flags(*, allow_network: bool) -> list[str]:
    """Shared hardening after the bwrap binary name is prepended by callers."""
    site_binds, _site_path = _python_site_ro_binds()
    argv = [
        "--die-with-parent",
        "--unshare-user",
        "--uid",
        "0",
        "--gid",
        "0",
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        *_system_ro_binds(allow_network=allow_network),
        *site_binds,
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    if not allow_network:
        argv.append("--unshare-net")
    return argv


@lru_cache(maxsize=1)
def _bwrap_bin() -> str | None:
    return shutil.which("bwrap")


def bwrap_available() -> bool:
    return _bwrap_bin() is not None


def _system_ro_binds(*, allow_network: bool = False) -> list[str]:
    args: list[str] = []
    for p in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(p).exists():
            args.extend(["--ro-bind", p, p])
    # Host /etc stays hidden (hard-probe canaries), but the mount itself must
    # not be writable — tmpfs + needed RO files + remount-ro.
    # DNS/TLS binds for allow_network must land *before* remount-ro.
    args.extend(["--tmpfs", "/etc"])
    for p in ("/etc/ld.so.cache", "/etc/alternatives"):
        if Path(p).exists():
            args.extend(["--ro-bind", p, p])
    if allow_network:
        for p in ("/etc/resolv.conf", "/etc/ssl"):
            if Path(p).exists():
                args.extend(["--ro-bind", p, p])
    args.extend(["--remount-ro", "/etc"])
    return args


def _python_site_ro_binds() -> tuple[list[str], str]:
    """RO-bind host site-packages (pytest etc.) without mounting the workspace."""
    import site

    args: list[str] = []
    paths: list[str] = []
    candidates: list[str] = []
    # Stdlib site helpers are always present on CPython; AttributeError → infra.
    try:
        candidates.extend(site.getsitepackages())
        candidates.append(site.getusersitepackages())
    except AttributeError as e:
        raise RuntimeError(
            f"sandbox_probe:site_helpers_missing:{e}"
        ) from e
    workspace = Path.cwd().resolve()
    # Existing site dirs only (the user site dir is normally absent); each
    # distinct path is bound once. A site dir that *is* the workspace must
    # never be mounted — refuse instead of silently skipping it.
    existing = [p for p in dict.fromkeys(candidates) if p and Path(p).is_dir()]
    for p in existing:
        if Path(p).resolve() == workspace:
            raise SandboxInfraError(
                f"sandbox_unavailable:site_packages_is_workspace:{p!r}"
            )
        args.extend(["--ro-bind", p, p])
        paths.append(p)
    return args, ":".join(paths)


@lru_cache(maxsize=2)
def probe_bwrap_hard(*, allow_network: bool) -> bool:
    """True only when bwrap hides host canaries and (unless allowed) has no net.

    ``False`` only for the probe's own verdict (host canary visible / net up) or
    no bwrap. Probe execution failures (OSError, timeout, bwrap error exit, no
    host canary file to test) raise ``SandboxInfraError`` — never a quiet False.
    """
    bwrap = _bwrap_bin()
    if bwrap is None:
        return False
    canary = next((p for p in PROBE_HOST_CANARY_PATHS if Path(p).is_file()), None)
    if canary is None:
        raise SandboxInfraError("sandbox_unavailable:bwrap_probe_no_host_canary")
    try:
        with tempfile.TemporaryDirectory(prefix="cos_probe_") as td:
            work = Path(td) / "work"
            work.mkdir()
            script = work / "probe.py"
            script.write_text(
                "import os,sys\n"
                f"if os.path.exists({canary!r}):\n"
                f"    sys.exit({PROBE_EXIT_HOST_VISIBLE})\n"
                "if not os.environ.get('ALLOW_NET'):\n"
                "    import socket\n"
                "    try:\n"
                f"        s=socket.socket(); s.settimeout({PROBE_NET_CONNECT_TIMEOUT_S!r})\n"
                f"        s.connect(({PROBE_NET_CANARY_HOST!r},{PROBE_NET_CANARY_PORT}))\n"
                f"        s.close(); sys.exit({PROBE_EXIT_NET_UP})\n"
                "    except OSError:\n"
                "        pass\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            # --unshare-user is required on GHA/CI: unshare-net alone fails
            # with "loopback: Failed RTM_NEWADDR: Operation not permitted".
            # Ubuntu 24.04 also needs apparmor_restrict_unprivileged_userns=0.
            argv = [
                bwrap,
                *_common_jail_flags(allow_network=allow_network),
                "--bind",
                str(work),
                "/work",
                "--chdir",
                "/work",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "LANG",
                "C.UTF-8",
            ]
            if allow_network:
                argv.extend(["--setenv", "ALLOW_NET", "1"])
            argv.extend(["--", "python3", "/work/probe.py"])
            r = subprocess.run(
                argv,
                capture_output=True,
                timeout=PROBE_SUBPROCESS_TIMEOUT_S,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:bwrap_probe_failed:{type(e).__name__}"
        ) from e
    # Only the probe script's own verdict codes are a definitive "not hard";
    # any other non-zero (bwrap/userns/mount error) is infra, not a verdict.
    if r.returncode in (PROBE_EXIT_HOST_VISIBLE, PROBE_EXIT_NET_UP):
        log.warning(
            "sandbox bwrap probe rejected hard (code=%s)", r.returncode
        )
        return False
    if r.returncode != 0:
        raise SandboxInfraError(
            f"sandbox_unavailable:bwrap_probe_exit:{r.returncode}:"
            f"{r.stderr[:200]!r}"
        )
    return True


def resolve_isolation(*, allow_network: bool) -> tuple[Isolation, Isolation]:
    """Return ``(net_isolation, fs_isolation)``. Never claim hard without probe."""
    if not bwrap_available():
        log.warning(
            "sandbox isolation unavailable: bubblewrap not installed; "
            "checks fail-closed (no guest exec)"
        )
        return _NET_ISOLATION_WITHOUT_HARD[allow_network], "soft"

    if probe_bwrap_hard(allow_network=allow_network):
        if allow_network:
            return "none", "hard"
        return "hard", "hard"

    log.warning(
        "sandbox isolation unavailable: bwrap hard probe failed — "
        "checks fail-closed (no guest exec); evidence not trusted"
    )
    return _NET_ISOLATION_WITHOUT_HARD[allow_network], "soft"


def isolation_fully_hard(net: Isolation, fs: Isolation) -> bool:
    return net == "hard" and fs == "hard"


def build_agent_bwrap(
    *,
    work: Path,
    allow_network: bool,
    extra_env: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """Agent jail: system RO, only ``work`` RW, clearenv, hardened unshare."""
    bwrap = _bwrap_bin()
    if bwrap is None:
        raise OSError("bwrap_missing")
    _binds, site_path = _python_site_ro_binds()
    argv = [
        bwrap,
        *_common_jail_flags(allow_network=allow_network),
        "--bind",
        str(work),
        "/work",
        "--chdir",
        "/work",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/work",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "PYTHONUNBUFFERED",
        "1",
    ]
    if site_path:
        argv.extend(["--setenv", "PYTHONPATH", site_path])
    for k, v in extra_env:
        if k in _ENV_ALLOWLIST:
            argv.extend(["--setenv", k, v])
    return argv


def build_verifier_bwrap(
    *,
    results: Path,
    inputs: Path,
    scorer: Path | None,
    holdout: Path | None,
    allow_network: bool,
    tests: Path | None = None,
    extra_ro: Sequence[tuple[Path, str]] = (),
    extra_env: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """Verifier jail: agent outputs/scorer/holdout/tests RO; only results RW.

    No agent code executes as the entrypoint — catalog scorer / task tests do.
    Journal/CAS/meta are simply not mounted.
    """
    bwrap = _bwrap_bin()
    if bwrap is None:
        raise OSError("bwrap_missing")
    # Never put /inputs on PYTHONPATH (agent files are data / allowlisted modules).
    _binds, site_path = _python_site_ro_binds()
    argv = [
        bwrap,
        *_common_jail_flags(allow_network=allow_network),
        "--ro-bind",
        str(inputs),
        "/inputs",
        "--bind",
        str(results),
        "/results",
        "--chdir",
        "/results",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "PYTHONUNBUFFERED",
        "1",
        "--setenv",
        "COSCIENTIST_INPUTS",
        "/inputs",
        "--setenv",
        "COSCIENTIST_RESULTS",
        "/results",
    ]
    if site_path:
        argv.extend(["--setenv", "PYTHONPATH", site_path])
    if scorer is not None:
        argv.extend(["--ro-bind", str(scorer), VERIFIER_SCORER_GUEST_PATH])
        argv.extend(["--setenv", "COSCIENTIST_SCORER", VERIFIER_SCORER_GUEST_PATH])
    if holdout is not None:
        argv.extend(["--ro-bind", str(holdout), "/holdout"])
        argv.extend(["--setenv", "COSCIENTIST_HOLDOUT", "/holdout"])
    if tests is not None:
        argv.extend(["--ro-bind", str(tests), "/tests"])
        argv.extend(["--setenv", "COSCIENTIST_TESTS", "/tests"])
    for host_path, guest_path in extra_ro:
        argv.extend(["--ro-bind", str(host_path), guest_path])
    for k, v in extra_env:
        if k in _ENV_ALLOWLIST:
            argv.extend(["--setenv", k, v])
    return argv
