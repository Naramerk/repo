"""Shared durable idempotency reservations for intake and console launch."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path
from typing import Any

from coscientist.core.events.errors import Conflict

WAIT_COMPLETE_S = 2.0
_STALE_RESERVATION_S = 60.0
log = logging.getLogger(__name__)


def _owner_sidecar_path(path: Path) -> Path:
    """Sidecar written before the mapping body so owner is reachable mid-write."""
    return Path(str(path) + ".owner")


def read_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None  # empty file from crash between O_EXCL and write
        mapping = json.loads(raw)
        if isinstance(mapping, dict):
            return mapping
        log.debug("reservation metadata is not an object")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        log.debug("reservation metadata unreadable", exc_info=True)
    return None


def _read_owner_sidecar(path: Path) -> tuple[str, int] | None:
    """Return ``(hostname, pid)`` from the sidecar, if present and parseable."""
    side = _owner_sidecar_path(path)
    try:
        raw = side.read_text(encoding="utf-8").strip()
        data = json.loads(raw)
        host = data.get("hostname")
        pid = data.get("owner_pid")
        if isinstance(host, str) and host and type(pid) is int and pid > 0:
            return host, pid
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        log.debug("reservation owner unreadable", exc_info=True)
    return None


def write_owner_sidecar(path: Path, *, hostname: str, owner_pid: int) -> None:
    side = _owner_sidecar_path(path)
    payload = {"hostname": hostname, "owner_pid": owner_pid, "created_at": time.time()}
    tmp = side.parent / f".{side.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, side)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                log.debug("reservation temporary cleanup failed", exc_info=True)


def _unlink_reservation(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("reservation cleanup failed", exc_info=True)
    try:
        _owner_sidecar_path(path).unlink(missing_ok=True)
    except OSError:
        log.debug("reservation cleanup failed", exc_info=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        exists_but_forbidden = isinstance(exc, PermissionError)
    else:
        return True
    return exists_but_forbidden


def _reservation_reclaimable(path: Path) -> bool:
    """True only for empty crash stubs or provably stale/dead-owner reservations.

    Owner identity lives in a ``.owner`` sidecar so mid-write corrupt mapping
    bodies still expose hostname+pid. Cross-host: reclaim by age only (never pid).
    Never unlink a recent non-empty file — it may be a live neighbour mid-write.
    """
    try:
        st = path.stat()
    except OSError:
        log.debug("reservation stat unavailable", exc_info=True)
    else:
        return _reclaimable_stat(path, st)
    return False


def _reclaimable_stat(path: Path, st: os.stat_result) -> bool:
    if st.st_size == 0:
        return True
    age = time.time() - st.st_mtime
    if age >= _STALE_RESERVATION_S:
        return True
    owner = _read_owner_sidecar(path)
    if owner is None:
        # Fall back to mapping body if sidecar missing (legacy / complete rewrite).
        existing = read_mapping(path)
        if existing is None:
            return False
        host = existing.get("hostname")
        pid = existing.get("owner_pid")
        if not isinstance(host, str) or not host or type(pid) is not int:
            return False
        owner = (host, pid)
    host, pid = owner
    local = socket.gethostname()
    if host != local:
        return False  # different host — only age may reclaim (already checked)
    return not _pid_alive(pid)


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Unique tmp + fsync + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            log.debug("reservation directory fsync unavailable", exc_info=True)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                log.debug("reservation temporary cleanup failed", exc_info=True)


async def reserve_key(
    path: Path, *, study_id: str, request_digest: str, key: str, request_label: str,
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """O_EXCL create the mapping file.

    Returns ``(mapping, created)``. Empty crash stubs (O_EXCL then die) and
    *provably stale* unreadable files may be reclaimed. A recent non-empty
    unreadable file is treated as a live neighbour write → ``Conflict`` (retry).

    Owner ``hostname``+``pid`` are written to a ``.owner`` sidecar immediately
    after O_EXCL so reclaim can see them even when the mapping body is corrupt.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname()
    owner_pid = os.getpid()
    payload = {
        "study_id": study_id,
        "request_digest": request_digest,
        "idempotency_key": key,
        "status": "pending",
        "hostname": hostname,
        "owner_pid": owner_pid,
        "created_at": time.time(),
    }
    if context is not None:
        payload["context"] = context
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY

    for _attempt in range(3):
        try:
            fd = os.open(str(path), flags, 0o644)
        except FileExistsError:
            log.debug("reservation already exists")
        else:
            try:
                write_owner_sidecar(path, hostname=hostname, owner_pid=owner_pid)
                data = json.dumps(payload, sort_keys=True).encode("utf-8")
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            return payload, True
        existing = read_mapping(path)
        if existing is None:
            await asyncio.sleep(0.05)
            existing = read_mapping(path)
        if existing is None:
            if _reservation_reclaimable(path):
                _unlink_reservation(path)
                await asyncio.sleep(0)
                continue
            raise Conflict(
                f"Idempotency-Key reservation busy or unreadable (not yet stale); retry later (key={key!r})"
            )
        if existing.get("request_digest") != request_digest:
            raise Conflict(f"Idempotency-Key reused with a different {request_label} body (key={key!r})")
        return existing, False
    raise Conflict(f"Idempotency-Key reservation unreadable after retries: {key!r}")


async def wait_complete(path: Path, *, timeout_s: float = WAIT_COMPLETE_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        existing = read_mapping(path)
        if existing and existing.get("status") == "complete":
            return existing
        await asyncio.sleep(0.05)
    existing = read_mapping(path)
    if existing and existing.get("status") == "complete":
        return existing
    raise Conflict(f"Idempotency-Key reservation timed out waiting for complete: {path}")

