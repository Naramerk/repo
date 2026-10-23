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
from coscientist.core.errors import Conflict
from coscientist.core.runtime_factory import close_runtime, open_runtime


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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
        # Best-effort directory fsync for durability of the rename.
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


def _reserve_key(
    path: Path, *, study_id: str, request_digest: str, key: str
) -> tuple[dict[str, Any], bool]:
    """O_EXCL create the mapping file.

    Returns ``(mapping, created)``. ``created=True`` means this caller owns the
    reservation and must run ``create_study``. Raises Conflict on digest mismatch.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "study_id": study_id,
        "request_digest": request_digest,
        "idempotency_key": key,
        "status": "pending",
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError:
        existing = _read_mapping(path)
        if existing is None:
            time.sleep(0.05)
            existing = _read_mapping(path)
        if existing is None:
            raise Conflict(f"Idempotency-Key reservation unreadable: {key!r}") from None
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


async def _wait_complete(path: Path, *, timeout_s: float = 30.0) -> dict[str, Any]:
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
    **create_kwargs: Any,
) -> dict[str, str]:
    """Create a study (``POST /studies`` semantics).

    When ``Idempotency-Key`` is provided:
    - ``study_id`` is derived deterministically from the key;
    - reservation is created with ``O_EXCL`` before ``create_study``;
    - concurrent losers wait for ``status=complete`` (no double create);
    - repeat with the same body returns the same ``study_id``;
    - repeat with a different body raises ``Conflict`` (HTTP 409);
    - crash after reserve / before complete: retry is safe (StudyCreated idempotent).
    """
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(body, dict):
        body = CreateStudyBody.model_validate(body)
    req_digest = _request_digest(body)

    if idempotency_key:
        sid = study_id_from_idempotency_key(idempotency_key)
        path = _idem_path(root, idempotency_key)
        mapping, reserved = _reserve_key(
            path, study_id=sid, request_digest=req_digest, key=idempotency_key
        )
        if mapping.get("status") == "complete":
            return {"study_id": str(mapping["study_id"])}
        if not reserved:
            # Another worker holds the pending reservation — wait for complete.
            # If the winner crashed, a later retry (new process) may O_EXCL-miss
            # and see pending forever; take over after a short wait.
            try:
                done = await _wait_complete(path, timeout_s=2.0)
                return {"study_id": str(done["study_id"])}
            except Conflict:
                # Take over: pending stale — proceed to create (StudyCreated idempotent).
                sid = str(mapping["study_id"])
        else:
            sid = str(mapping["study_id"])
    else:
        from coscientist.core.ids import study_id as new_study_id

        sid = new_study_id()
        path = None

    rt = await open_runtime(root, sid, catalog=catalog)
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
    finally:
        await close_runtime(rt)

    if idempotency_key and path is not None:
        _atomic_write(
            path,
            {
                "study_id": created_id,
                "request_digest": req_digest,
                "idempotency_key": idempotency_key,
                "status": "complete",
            },
        )
    return {"study_id": created_id}
