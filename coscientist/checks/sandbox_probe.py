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
from typing import Literal, Sequence

log = logging.getLogger(__name__)

Isolation = Literal["hard", "soft", "none"]

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
        *_system_ro_binds(),
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


def _system_ro_binds() -> list[str]:
    args: list[str] = []
    for p in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(p).exists():
            args.extend(["--ro-bind", p, p])
    for p in ("/etc/ld.so.cache", "/etc/alternatives"):
        if Path(p).exists():
            args.extend(["--ro-bind", p, p])
    return args


def _python_site_ro_binds() -> tuple[list[str], str]:
    """RO-bind host site-packages (pytest etc.) without mounting the workspace."""
    import site

    args: list[str] = []
    paths: list[str] = []
    seen: set[str] = set()
    candidates: list[str] = []
    try:
        candidates.extend(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass
    try:
        candidates.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    for p in candidates:
        if not p or p in seen:
            continue
        path = Path(p)
        if not path.is_dir():
            continue
        # Never mount the workspace / project tree into the jail.
        if path.resolve() == Path.cwd().resolve():
            continue
        seen.add(p)
        args.extend(["--ro-bind", p, p])
        paths.append(p)
    return args, ":".join(paths)


@lru_cache(maxsize=2)
def probe_bwrap_hard(*, allow_network: bool) -> bool:
    """True only when bwrap hides host canaries and (unless allowed) has no net.

    Any probe failure → False (never ignore mount/exec errors).
    """
    bwrap = _bwrap_bin()
    if bwrap is None:
        return False
    canary = "/etc/hostname" if Path("/etc/hostname").is_file() else "/etc/passwd"
    try:
        with tempfile.TemporaryDirectory(prefix="cos_probe_") as td:
            work = Path(td) / "work"
            work.mkdir()
            script = work / "probe.py"
            script.write_text(
                "import os,sys\n"
                f"if os.path.exists({canary!r}):\n"
                "    sys.exit(11)\n"
                "if not os.environ.get('ALLOW_NET'):\n"
                "    import socket\n"
                "    try:\n"
                "        s=socket.socket(); s.settimeout(0.2)\n"
                "        s.connect(('1.1.1.1',80)); s.close(); sys.exit(12)\n"
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
            r = subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("sandbox bwrap probe failed: %s", e)
        return False
    if r.returncode != 0:
        log.warning(
            "sandbox bwrap probe rejected hard (code=%s stderr=%r)",
            r.returncode,
            (r.stderr or b"")[:200],
        )
        return False
    return True


def resolve_isolation(*, allow_network: bool) -> tuple[Isolation, Isolation]:
    """Return ``(net_isolation, fs_isolation)``. Never claim hard without probe."""
    if not bwrap_available():
        log.warning(
            "sandbox isolation unavailable: bubblewrap not installed; "
            "checks fail-closed (no guest exec)"
        )
        return ("none" if allow_network else "soft"), "soft"

    if probe_bwrap_hard(allow_network=allow_network):
        if allow_network:
            return "none", "hard"
        return "hard", "hard"

    log.warning(
        "sandbox isolation unavailable: bwrap hard probe failed — "
        "checks fail-closed (no guest exec); evidence not trusted"
    )
    return ("none" if allow_network else "soft"), "soft"


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
        if k in _ENV_ALLOWLIST or k.startswith("COSCIENTIST_"):
            argv.extend(["--setenv", k, v])
    return argv


def build_verifier_bwrap(
    *,
    results: Path,
    inputs: Path,
    scorer: Path | None,
    holdout: Path | None,
    tests: Path | None = None,
    allow_network: bool = False,
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
        argv.extend(["--ro-bind", str(scorer), "/scorer"])
        argv.extend(["--setenv", "COSCIENTIST_SCORER", "/scorer"])
    if holdout is not None:
        argv.extend(["--ro-bind", str(holdout), "/holdout"])
        argv.extend(["--setenv", "COSCIENTIST_HOLDOUT", "/holdout"])
    if tests is not None:
        argv.extend(["--ro-bind", str(tests), "/tests"])
        argv.extend(["--setenv", "COSCIENTIST_TESTS", "/tests"])
    for host_path, guest_path in extra_ro:
        argv.extend(["--ro-bind", str(host_path), guest_path])
    for k, v in extra_env:
        if k in _ENV_ALLOWLIST or k.startswith("COSCIENTIST_"):
            argv.extend(["--setenv", k, v])
    return argv
