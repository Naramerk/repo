"""POST /studies with Idempotency-Key (DESIGN §9 + DESIGN_M1 §8 / K13)."""

from __future__ import annotations

import json
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


def _idem_path(data_root: Path, key: str) -> Path:
    digest = sha256_hex(key)
    return Path(data_root) / "idempotency" / "studies" / digest


def _request_digest(body: CreateStudyBody) -> str:
    return sha256_hex(canonical_json(body.model_dump(mode="json")))


def _read_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_mapping(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


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
    - first request stores ``key → study_id`` (+ request digest);
    - repeat with the same body returns the same ``study_id``;
    - repeat with a different body raises ``Conflict`` (HTTP 409).
    """
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(body, dict):
        body = CreateStudyBody.model_validate(body)
    req_digest = _request_digest(body)

    if idempotency_key:
        path = _idem_path(root, idempotency_key)
        existing = _read_mapping(path)
        if existing is not None:
            if existing.get("request_digest") != req_digest:
                raise Conflict(
                    f"Idempotency-Key reused with a different POST /studies body "
                    f"(key={idempotency_key!r})"
                )
            return {"study_id": str(existing["study_id"])}

    # Provisional sid for open_runtime path; create_study may keep it.
    from coscientist.core.ids import study_id as new_study_id

    sid = new_study_id()
    rt = await open_runtime(root, sid, catalog=catalog)
    try:
        created = await rt.create_study(
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

    if idempotency_key:
        _write_mapping(
            _idem_path(root, idempotency_key),
            {
                "study_id": created,
                "request_digest": req_digest,
                "idempotency_key": idempotency_key,
            },
        )
    return {"study_id": created}
