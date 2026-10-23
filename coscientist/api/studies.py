"""POST /studies with Idempotency-Key (DESIGN §9 + DESIGN_M1 §8 / K13).

Idempotency-Key → deterministic ``study_id`` plus an O_EXCL reservation file so
concurrent POSTs and crash-restart cannot create two studies for one key.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import Budget, HitlPolicy
from coscientist.core.errors import Conflict, LeaseLost
from coscientist.core.runtime_factory import close_runtime, open_runtime

# Default wait for another worker to finish create_study before takeover.
_WAIT_COMPLETE_S = 2.0
# Only reclaim a non-empty unreadable reservation if older than this (or owner dead).
_STALE_RESERVATION_S = 60.0


class CreateStudyBody(BaseModel):
    """Request body for ``POST /studies``."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "research"
    request: str
    catalog_version: str = "test"
    hitl_policy: HitlPolicy | None = None
    deadline: datetime | None = None
    budget: Budget | None = None
    finalize_reserve_s: int = 30


def study_id_from_idempotency_key(key: str) -> str:
    """Deterministic study_id derived from the client Idempotency-Key."""
    digest = sha256_hex(f"Idempotency-Key:{key}")
    return f"st_idem_{digest[:24]}"


def _idem_path(data_root: Path, key: str) -> Path:
    digest = sha256_hex(key)
    return Path(data_root) / "idempotency" / "studies" / digest


def _request_digest(body: CreateStudyBody) -> str:
    return sha256_hex(canonical_json(body.model_dump(mode="json")))


def _read_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None  # empty file from crash between O_EXCL and write
        return json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not owned by us
    except OSError:
        return False
    return True


def _reservation_reclaimable(path: Path) -> bool:
    """True only for empty crash stubs or provably stale/dead-owner reservations.

    Never unlink a recent non-empty file — it may be a live neighbour mid-write.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return True
    age = time.time() - st.st_mtime
    if age >= _STALE_RESERVATION_S:
        return True
    existing = _read_mapping(path)
    if existing is not None:
        pid = existing.get("owner_pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            return True
        return False  # readable live mapping — not a reclaim candidate here
    return False


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
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
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


async def _reserve_key(
    path: Path, *, study_id: str, request_digest: str, key: str
) -> tuple[dict[str, Any], bool]:
    """O_EXCL create the mapping file.

    Returns ``(mapping, created)``. Empty crash stubs (O_EXCL then die) and
    *provably stale* unreadable files may be reclaimed. A recent non-empty
    unreadable file is treated as a live neighbour write → ``Conflict`` (retry).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "study_id": study_id,
        "request_digest": request_digest,
        "idempotency_key": key,
        "status": "pending",
        "owner_pid": os.getpid(),
        "created_at": time.time(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY

    for _attempt in range(3):
        try:
            fd = os.open(str(path), flags, 0o644)
        except FileExistsError:
            existing = _read_mapping(path)
            if existing is None:
                # Brief wait — neighbour may still be writing the first bytes.
                await asyncio.sleep(0.05)
                existing = _read_mapping(path)
            if existing is None:
                if _reservation_reclaimable(path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    await asyncio.sleep(0)
                    continue
                raise Conflict(
                    f"Idempotency-Key reservation busy or unreadable "
                    f"(not yet stale); retry later (key={key!r})"
                ) from None
            if existing.get("request_digest") != request_digest:
                raise Conflict(
                    f"Idempotency-Key reused with a different POST /studies body "
                    f"(key={key!r})"
                ) from None
            return existing, False
        try:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return payload, True
    raise Conflict(f"Idempotency-Key reservation unreadable after retries: {key!r}")


async def _wait_complete(path: Path, *, timeout_s: float = _WAIT_COMPLETE_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        existing = _read_mapping(path)
        if existing and existing.get("status") == "complete":
            return existing
        await asyncio.sleep(0.05)
    existing = _read_mapping(path)
    if existing and existing.get("status") == "complete":
        return existing
    raise Conflict(f"Idempotency-Key reservation timed out waiting for complete: {path}")


async def post_studies(
    data_root: Path | str,
    body: CreateStudyBody | dict[str, Any],
    *,
    idempotency_key: str | None = None,
    catalog: Any,
    wait_complete_s: float = _WAIT_COMPLETE_S,
    **create_kwargs: Any,
) -> dict[str, str]:
    """Create a study (``POST /studies`` semantics).

    ``wait_complete_s`` is how long a losing concurrent caller waits for the
    winner before taking over a stale pending reservation (testable).
    """
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(body, dict):
        body = CreateStudyBody.model_validate(body)
    req_digest = _request_digest(body)

    if idempotency_key:
        sid = study_id_from_idempotency_key(idempotency_key)
        path = _idem_path(root, idempotency_key)
        mapping, reserved = await _reserve_key(
            path, study_id=sid, request_digest=req_digest, key=idempotency_key
        )
        if mapping.get("status") == "complete":
            return {"study_id": str(mapping["study_id"])}
        if not reserved:
            try:
                done = await _wait_complete(path, timeout_s=wait_complete_s)
                return {"study_id": str(done["study_id"])}
            except Conflict:
                sid = str(mapping["study_id"])
        else:
            sid = str(mapping["study_id"])
    else:
        from coscientist.core.ids import study_id as new_study_id

        sid = new_study_id()
        path = None

    async def _wait_for_peer() -> dict[str, str]:
        if path is None:
            raise Conflict(f"lease lost creating study {sid} without idempotency path")
        done = await _wait_complete(path, timeout_s=max(wait_complete_s * 10, 30.0))
        return {"study_id": str(done["study_id"])}

    try:
        rt = await open_runtime(root, sid, catalog=catalog)
    except LeaseLost:
        if idempotency_key:
            return await _wait_for_peer()
        raise

    created_id: str | None = None
    try:
        try:
            created_id = await rt.create_study(
                profile=body.profile,
                request=body.request,
                catalog_version=body.catalog_version,
                hitl_policy=body.hitl_policy,
                deadline=body.deadline,
                budget=body.budget,
                finalize_reserve_s=body.finalize_reserve_s,
                sid=sid,
                **create_kwargs,
            )
        except Conflict:
            # Peer wrote StudyCreated (payload clock differs) — treat as success.
            events = await rt.store.read_all()
            if any(e.type == "StudyCreated" for e in events):
                created_id = sid
            else:
                raise
    finally:
        await close_runtime(rt)

    if created_id is None:
        if idempotency_key:
            return await _wait_for_peer()
        raise Conflict(f"create_study produced no study_id for {sid}")

    if idempotency_key and path is not None:
        _atomic_write(
            path,
            {
                "study_id": created_id,
                "request_digest": req_digest,
                "idempotency_key": idempotency_key,
                "status": "complete",
                "owner_pid": os.getpid(),
                "created_at": time.time(),
            },
        )
    return {"study_id": created_id}
