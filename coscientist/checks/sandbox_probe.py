"""Cached probes for sandbox net/fs isolation (M1 PR17)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from functools import lru_cache
from typing import Literal

log = logging.getLogger(__name__)

Isolation = Literal["hard", "soft", "none"]


@lru_cache(maxsize=1)
def _unshare_bin() -> str | None:
    return shutil.which("unshare")


@lru_cache(maxsize=8)
def probe_unshare(*flags: str) -> bool:
    """True when ``unshare <flags> -- true`` succeeds (cached; MJ2)."""
    unshare = _unshare_bin()
    if unshare is None:
        return False
    try:
        r = subprocess.run(
            [unshare, *flags, "--", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def resolve_isolation(*, allow_network: bool) -> tuple[Isolation, Isolation, list[str]]:
    """Return ``(net_isolation, fs_isolation, argv_prefix)``.

    Hard = user+mount(+net) unshare. Soft = no unshare (network may be open;
    blocklist is bypassable). ``allow_network=True`` ⇒ net=none (not trusted).
    """
    if allow_network:
        if probe_unshare("--user", "--map-root-user", "--mount"):
            unshare = _unshare_bin()
            assert unshare is not None
            return (
                "none",
                "hard",
                [unshare, "--user", "--map-root-user", "--mount", "--"],
            )
        log.warning(
            "sandbox soft fs_isolation: hard mount unshare unavailable; "
            "evidence not trusted"
        )
        return "none", "soft", []

    if probe_unshare("--user", "--map-root-user", "--mount", "--net"):
        unshare = _unshare_bin()
        assert unshare is not None
        return (
            "hard",
            "hard",
            [unshare, "--user", "--map-root-user", "--mount", "--net", "--"],
        )

    log.warning(
        "sandbox soft isolation: hard unshare unavailable — network may be open "
        "(fetcher blocklist is bypassable); evidence not trusted"
    )
    return "soft", "soft", []


def isolation_fully_hard(net: Isolation, fs: Isolation) -> bool:
    """trusted requires both net and fs hard (MJ3: == hard, not != soft)."""
    return net == "hard" and fs == "hard"
