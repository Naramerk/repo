"""POST /studies with Idempotency-Key (DESIGN §9 + DESIGN_M1 §8 / K13).

Idempotency-Key → deterministic ``study_id`` plus an O_EXCL reservation file so
concurrent POSTs and crash-restart cannot create two studies for one key.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from coscientist.api.idempotency import (
    WAIT_COMPLETE_S as _WAIT_COMPLETE_S,
)
from coscientist.api.idempotency import (
    atomic_write as _atomic_write,
)
from coscientist.api.idempotency import (
    reserve_key as _reserve_key,
)
from coscientist.api.idempotency import (
    wait_complete as _wait_complete,
)
from coscientist.api.idempotency import (
    write_owner_sidecar as _write_owner_sidecar,
)
from coscientist.core.claims.reasons import RefusalKind
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import Conflict, LeaseLost
from coscientist.core.events.ids import study_id_from_idempotency_key
from coscientist.core.execution.domain import Budget, Catalog, HitlPolicy
from coscientist.core.execution.input_limits import (
    InputLimits,
    default_input_limits,
    enforce_request_body_limit,
    merge_input_limits,
)
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from coscientist.core.execution.thresholds import (
    CONFIRMATIONS_NEEDED_GE,
    MAX_ATTEMPTS_GE,
    MAX_EXPANSIONS_GE,
    REFUTATIONS_NEEDED_GE,
    SUPPORTED_NEEDED_GE,
)

if TYPE_CHECKING:
    from coscientist.core.execution._ingest_gate import UserPutCapability


class CreateStudyBody(BaseModel):
    """Request body for ``POST /studies``."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "research"
    request: str
    catalog_version: StrictInt
    # G4/D3: required — caller must pass (no API default).
    require_hypotheses: bool
    # RC3t: Study thresholds required (no API / construct defaults).
    # StrictInt/StrictBool — refuse True/"2" coercion (same as catalog_version).
    # ge= from thresholds.py (single source with Study / upcasters / verdict).
    confirmations_needed: StrictInt = Field(ge=CONFIRMATIONS_NEEDED_GE)
    refutations_needed: StrictInt = Field(ge=REFUTATIONS_NEEDED_GE)
    refutation_priority: StrictBool
    supported_needed: StrictInt = Field(ge=SUPPORTED_NEEDED_GE)
    max_expansions: StrictInt = Field(ge=MAX_EXPANSIONS_GE)
    # RC17a: required (profile.max_attempts → Study; no API default).
    max_attempts: StrictInt = Field(ge=MAX_ATTEMPTS_GE)
    hitl_policy: HitlPolicy | None = None
    deadline: datetime | None = None
    budget: Budget | None = None
    finalize_reserve_s: int = 30
    # PR19 / DESIGN_M1 §9: user blobs → CAS before SPEC; as_of for time-sensitive Qs.
    # Profile owns InputLimits; client may only LOWER via this optional dict.
    inputs: list[dict[str, object]] = []
    as_of: datetime | None = None
    input_limits: dict[str, Any] | None = None


def parse_create_study_body(
    raw: bytes | str | dict[str, object] | CreateStudyBody,
    *,
    profile_limits: InputLimits | None = None,
) -> tuple[CreateStudyBody, InputLimits] | tuple[None, tuple[RefusalKind, str]]:
    """Decode body with wire-size gate **before** expanding into memory.

    ValidationError / client raising limits → ``INPUT_INVALID`` (never 500).
    Oversized wire body → ``INPUT_TOO_LARGE``.
    """
    profile = profile_limits or default_input_limits()
    if isinstance(raw, CreateStudyBody):
        body = raw
    elif isinstance(raw, dict):
        # Size gate before pydantic expand (DoS).
        bad = enforce_request_body_limit(raw, max_wire_bytes=profile.max_wire_bytes)
        if bad is not None:
            return None, bad
        try:
            body = CreateStudyBody.model_validate(raw)
        except ValidationError as e:
            return None, (RefusalKind.INPUT_INVALID, f"body:{type(e).__name__}")
    else:
        bad = enforce_request_body_limit(raw, max_wire_bytes=profile.max_wire_bytes)
        if bad is not None:
            return None, bad
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
            return None, (RefusalKind.INPUT_INVALID, f"json:{type(e).__name__}")
        try:
            body = CreateStudyBody.model_validate(data)
        except ValidationError as e:
            return None, (RefusalKind.INPUT_INVALID, f"body:{type(e).__name__}")
    merged = merge_input_limits(profile, body.input_limits)
    if isinstance(merged, tuple):
        return None, merged
    return body, merged


def _idem_path(data_root: Path, key: str) -> Path:
    digest = sha256_hex(key)
    return Path(data_root) / "idempotency" / "studies" / digest


def _request_digest(body: CreateStudyBody) -> str:
    return sha256_hex(canonical_json(body.model_dump(mode="json")))


def _raw_body_digest(raw: bytes | str | dict[str, object] | CreateStudyBody) -> str:
    """Idempotency digest over the full client body (not a reconstructed minimal body)."""
    if isinstance(raw, CreateStudyBody):
        return _request_digest(raw)
    if isinstance(raw, dict):
        return sha256_hex(canonical_json(raw))
    if isinstance(raw, bytes):
        return sha256_hex(raw)
    return sha256_hex(raw.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _CatalogVersionOk:
    value: int


@dataclass(frozen=True, slots=True)
class _CatalogVersionRefuse:
    """Typed refuse for missing / non-StrictInt ``catalog_version`` (never soft ``None``)."""

    reason: str


def _raw_body_dict(
    raw: bytes | str | dict[str, object] | CreateStudyBody,
) -> dict[str, object] | None:
    """Single JSON decode for refuse-path field extraction (no invented defaults)."""
    if isinstance(raw, CreateStudyBody):
        return raw.model_dump(mode="json")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _catalog_version_from_raw(
    raw: bytes | str | dict[str, object] | CreateStudyBody,
) -> _CatalogVersionOk | _CatalogVersionRefuse:
    """StrictInt ``catalog_version`` from client input, or a typed refuse.

    Refuse-path only — never invent ``CATALOG_VERSION`` / a current generation.
    Wrong types (``\"3\"``, float, bool) are refused, not collapsed to soft ``None``.
    """
    data = _raw_body_dict(raw)
    if data is None:
        return _CatalogVersionRefuse("catalog_version_required")
    if "catalog_version" not in data:
        return _CatalogVersionRefuse("catalog_version_required")
    cv = data["catalog_version"]
    if isinstance(cv, bool) or not isinstance(cv, int):
        return _CatalogVersionRefuse(f"catalog_version_must_be_strict_int_got_{type(cv).__name__}:{cv!r}")
    return _CatalogVersionOk(cv)


def _request_from_raw_dict(data: dict[str, object] | None) -> str | None:
    """Client ``request`` string when present; absent/non-str → ``None`` (no literal)."""
    request = data.get("request") if data is not None else None
    if isinstance(request, str):
        return request.strip()
    return None


def _refuse_body_from_client(
    data: dict[str, object] | None, *, request: str, catalog_version: int
) -> CreateStudyBody | None:
    """Build a validated CreateStudyBody from client fields only (no model_construct)."""
    candidate: dict[str, object] = {
        "request": request,
        "catalog_version": catalog_version,
    }
    if data is not None:
        for key in (
            "profile",
            "require_hypotheses",
            "confirmations_needed",
            "refutations_needed",
            "refutation_priority",
            "supported_needed",
            "max_expansions",
            "max_attempts",
            "hitl_policy",
            "deadline",
            "budget",
            "finalize_reserve_s",
            "inputs",
            "as_of",
            "input_limits",
        ):
            if key in data:
                candidate[key] = data[key]
    try:
        return CreateStudyBody.model_validate(candidate)
    except ValidationError:
        return None


def _study_created_matches_body(payload: dict[str, object], body: CreateStudyBody) -> bool:
    """True when an existing StudyCreated is the same POST /studies request."""
    return (
        payload.get("request") == body.request
        and payload.get("catalog_version") == body.catalog_version
        and payload.get("profile") == body.profile
        and payload.get("finalize_reserve_s") == body.finalize_reserve_s
        and payload.get("require_hypotheses") == body.require_hypotheses
        and payload.get("confirmations_needed") == body.confirmations_needed
        and payload.get("refutations_needed") == body.refutations_needed
        and payload.get("refutation_priority") == body.refutation_priority
        and payload.get("supported_needed") == body.supported_needed
        and payload.get("max_expansions") == body.max_expansions
        and payload.get("max_attempts") == body.max_attempts
    )


def mint_study_user_put_capability() -> UserPutCapability:
    """Mint a ``UserPutCapability`` for a non-HTTP study-intake caller.

    The CLI ``run`` path reads ``--task-input`` files and hands them to
    ``create_study`` — the same ingest gate (``register_user_input_artifacts``)
    as ``POST /studies`` — and only this module and ``core.plan.lifecycle``
    may import the gate (import-linter), so the mint stays inside the intake
    module instead of widening the gate's importer set.
    """
    from coscientist.core.execution._ingest_gate import mint_user_put_capability

    return mint_user_put_capability()


def release_study_user_put_capability(capability: UserPutCapability | None) -> None:
    """Release a capability minted via ``mint_study_user_put_capability``."""
    from coscientist.core.execution._ingest_gate import release_user_put_capability

    release_user_put_capability(capability)


async def post_studies(
    data_root: Path | str,
    body: CreateStudyBody | dict[str, object] | bytes | str,
    *,
    idempotency_key: str | None = None,
    catalog: Catalog,
    wait_complete_s: float = _WAIT_COMPLETE_S,
    profile_input_limits: InputLimits | None = None,
    **create_kwargs: Any,
) -> dict[str, str]:
    """Create a study (``POST /studies`` semantics).

    ``wait_complete_s`` is how long a losing concurrent caller waits for the
    winner before taking over a stale pending reservation (testable).

    Raw ``bytes``/``str`` bodies are size-gated **before** JSON decode.
    Invalid body / client raising limits → StudyFinalized(refused), never 500.
    """
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    profile_limits = profile_input_limits or default_input_limits()
    raw_body = body
    parsed = parse_create_study_body(body, profile_limits=profile_limits)
    refuse_kind: RefusalKind | None = None
    refuse_reason: str | None = None
    # Missing / non-StrictInt client cv → StudyRefused.catalog_version=null path
    # (no StudyCreated; never invent a generation). Do not overwrite parse reasons.
    refuse_without_catalog = False
    validated_body: CreateStudyBody | None = None
    if parsed[0] is None:
        refuse_kind, refuse_reason = parsed[1]
        data = _raw_body_dict(raw_body)
        cv_result = _catalog_version_from_raw(raw_body if data is None else data)
        req = _request_from_raw_dict(data)
        if isinstance(cv_result, _CatalogVersionRefuse) or req is None:
            refuse_without_catalog = True
        else:
            built = _refuse_body_from_client(data, request=req, catalog_version=cv_result.value)
            if built is None:
                refuse_without_catalog = True
            else:
                validated_body = built
    else:
        validated_body, _effective = parsed
        _ = _effective  # validated; create_study re-merges client vs profile
    # Full client body — two different invalid payloads must not share a digest.
    req_digest = _raw_body_digest(raw_body)

    if idempotency_key:
        sid = study_id_from_idempotency_key(idempotency_key)
        path = _idem_path(root, idempotency_key)
        mapping, reserved = await _reserve_key(
            path, study_id=sid, request_digest=req_digest, key=idempotency_key, request_label="POST /studies",
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
        from coscientist.core.events.ids import study_id as new_study_id

        sid = new_study_id()
        path = None

    async def _wait_for_peer() -> dict[str, str]:
        if path is None:
            raise Conflict(f"lease lost creating study {sid} without idempotency path")
        done = await _wait_complete(path, timeout_s=max(wait_complete_s * 10, 30.0))
        return {"study_id": str(done["study_id"])}

    try:
        from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime

        rt = await open_runtime(root, sid, catalog=catalog, runtime_cls=ApiStudyRuntime)
        assert isinstance(rt, ApiStudyRuntime)
    except LeaseLost:
        if idempotency_key:
            return await _wait_for_peer()
        raise

    created_id: str | None = None
    try:
        try:
            from coscientist.core.plan.intake import IntakeRefuse

            if refuse_without_catalog:
                if refuse_kind is None or refuse_reason is None:
                    raise Conflict("refuse_without_catalog missing typed refuse reason")
                created_id = await rt.refuse_study_without_catalog(
                    kind=refuse_kind,
                    reason=str(refuse_reason)[:200],
                    sid=sid,
                )
            else:
                assert validated_body is not None
                body = validated_body
                force_refuse = (
                    IntakeRefuse(refuse_kind, str(refuse_reason)[:200])
                    if refuse_kind is not None and refuse_reason is not None
                    else None
                )
                if force_refuse is not None:
                    # Intake-refuse path: do not forward client inputs.
                    use_inputs: list[dict[str, object]] = []
                    client_input_limits = None
                else:
                    use_inputs = list(body.inputs)
                    client_input_limits = body.input_limits
                user_cap = mint_study_user_put_capability() if use_inputs else None
                try:
                    created_id = await rt.create_study(
                        profile=body.profile,
                        request=body.request.strip(),
                        catalog_version=body.catalog_version,
                        hitl_policy=body.hitl_policy,
                        deadline=body.deadline,
                        budget=body.budget,
                        finalize_reserve_s=body.finalize_reserve_s,
                        sid=sid,
                        require_hypotheses=body.require_hypotheses,
                        confirmations_needed=body.confirmations_needed,
                        refutations_needed=body.refutations_needed,
                        refutation_priority=body.refutation_priority,
                        supported_needed=body.supported_needed,
                        max_expansions=body.max_expansions,
                        max_attempts=body.max_attempts,
                        inputs=use_inputs,
                        as_of=body.as_of,
                        input_limits=profile_limits,
                        client_input_limits=client_input_limits,
                        intake_refuse=force_refuse,
                        user_put_capability=user_cap,
                        **create_kwargs,
                    )
                finally:
                    release_study_user_put_capability(user_cap)
        except Conflict:
            # Peer wrote first — success only when it is the same request.
            events = await rt.store.read_all()
            created_ev = next((e for e in events if e.type == "StudyCreated"), None)
            if (
                created_ev is not None
                and validated_body is not None
                and _study_created_matches_body(created_ev.payload, validated_body)
            ):
                created_id = sid
            elif refuse_without_catalog and any(e.type == "StudyRefused" for e in events):
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
        host = socket.gethostname()
        pid = os.getpid()
        _write_owner_sidecar(path, hostname=host, owner_pid=pid)
        _atomic_write(
            path,
            {
                "study_id": created_id,
                "request_digest": req_digest,
                "idempotency_key": idempotency_key,
                "status": "complete",
                "hostname": host,
                "owner_pid": pid,
                "created_at": time.time(),
            },
        )
    return {"study_id": created_id}
