"""POST /studies with Idempotency-Key (DESIGN §9 + DESIGN_M1 §8 / K13).

Idempotency-Key → deterministic ``study_id`` plus an O_EXCL reservation file so
concurrent POSTs and crash-restart cannot create two studies for one key.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from coscientist.core.claims.reasons import RefusalKind
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import Conflict, LeaseLost
from coscientist.core.execution.domain import Budget, HitlPolicy
from coscientist.core.execution.input_limits import (
    InputLimits,
    default_input_limits,
    enforce_request_body_limit,
    merge_input_limits,
)
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime

# Default wait for another worker to finish create_study before takeover.
_WAIT_COMPLETE_S = 2.0
# Only reclaim a non-empty unreadable reservation if older than this (or owner dead).
_STALE_RESERVATION_S = 60.0


class CreateStudyBody(BaseModel):
    """Request body for ``POST /studies``."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "research"
    request: str
    catalog_version: StrictInt
    hitl_policy: HitlPolicy | None = None
    deadline: datetime | None = None
    budget: Budget | None = None
    finalize_reserve_s: int = 30
    # PR19 / DESIGN_M1 §9: user blobs → CAS before SPEC; as_of for time-sensitive Qs.
    # Profile owns InputLimits; client may only LOWER via this optional dict.
    inputs: list[dict[str, Any]] = []
    as_of: datetime | None = None
    input_limits: dict[str, Any] | None = None


def parse_create_study_body(
    raw: bytes | str | dict[str, Any] | CreateStudyBody,
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
        bad = enforce_request_body_limit(
            raw, max_wire_bytes=profile.max_wire_bytes
        )
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


def study_id_from_idempotency_key(key: str) -> str:
    """Deterministic study_id derived from the client Idempotency-Key."""
    digest = sha256_hex(f"Idempotency-Key:{key}")
    return f"st_idem_{digest[:24]}"


def _idem_path(data_root: Path, key: str) -> Path:
    digest = sha256_hex(key)
    return Path(data_root) / "idempotency" / "studies" / digest


def _owner_sidecar_path(path: Path) -> Path:
    """Sidecar written before the mapping body so owner is reachable mid-write."""
    return Path(str(path) + ".owner")


def _request_digest(body: CreateStudyBody) -> str:
    return sha256_hex(canonical_json(body.model_dump(mode="json")))


def _raw_body_digest(raw: bytes | str | dict[str, Any] | CreateStudyBody) -> str:
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
    raw: bytes | str | dict[str, Any] | CreateStudyBody,
) -> dict[str, Any] | None:
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
    raw: bytes | str | dict[str, Any] | CreateStudyBody,
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
        return _CatalogVersionRefuse(
            "catalog_version_must_be_strict_int_got_"
            f"{type(cv).__name__}:{cv!r}"
        )
    return _CatalogVersionOk(cv)


def _request_from_raw_dict(data: dict[str, Any] | None) -> str | None:
    """Client ``request`` string when present; absent/non-str → ``None`` (no literal)."""
    if data is not None and isinstance(data.get("request"), str):
        return data["request"].strip()
    return None


def _refuse_body_from_client(
    data: dict[str, Any] | None, *, request: str, catalog_version: int
) -> CreateStudyBody | None:
    """Build a validated CreateStudyBody from client fields only (no model_construct)."""
    candidate: dict[str, Any] = {
        "request": request,
        "catalog_version": catalog_version,
    }
    if data is not None:
        for key in (
            "profile",
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


def _study_created_matches_body(
    payload: dict[str, Any], body: CreateStudyBody
) -> bool:
    """True when an existing StudyCreated is the same POST /studies request."""
    return (
        payload.get("request") == body.request
        and payload.get("catalog_version") == body.catalog_version
        and payload.get("profile") == body.profile
        and payload.get("finalize_reserve_s") == body.finalize_reserve_s
    )


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


def _read_owner_sidecar(path: Path) -> tuple[str, int] | None:
    """Return ``(hostname, pid)`` from the sidecar, if present and parseable."""
    side = _owner_sidecar_path(path)
    try:
        raw = side.read_text(encoding="utf-8").strip()
        data = json.loads(raw)
        host = str(data.get("hostname") or "")
        pid = data.get("owner_pid")
        if host and isinstance(pid, int) and pid > 0:
            return host, pid
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return None


def _write_owner_sidecar(path: Path, *, hostname: str, owner_pid: int) -> None:
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
                pass


def _unlink_reservation(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _owner_sidecar_path(path).unlink(missing_ok=True)
    except OSError:
        pass


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

    Owner identity lives in a ``.owner`` sidecar so mid-write corrupt mapping
    bodies still expose hostname+pid. Cross-host: reclaim by age only (never pid).
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
    owner = _read_owner_sidecar(path)
    if owner is None:
        # Fall back to mapping body if sidecar missing (legacy / complete rewrite).
        existing = _read_mapping(path)
        if existing is None:
            return False
        host = str(existing.get("hostname") or "")
        pid = existing.get("owner_pid")
        if not host or not isinstance(pid, int):
            return False
        owner = (host, pid)
    host, pid = owner
    local = socket.gethostname()
    if host != local:
        return False  # different host — only age may reclaim (already checked)
    return not _pid_alive(pid)


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
                    _unlink_reservation(path)
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
            # Sidecar first so a crash mid-body still leaves reclaimable owner info.
            _write_owner_sidecar(path, hostname=hostname, owner_pid=owner_pid)
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
    body: CreateStudyBody | dict[str, Any] | bytes | str,
    *,
    idempotency_key: str | None = None,
    catalog: Any,
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
            built = _refuse_body_from_client(
                data, request=req, catalog_version=cv_result.value
            )
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
                from coscientist.core.execution._ingest_gate import (
                    mint_user_put_capability,
                    release_user_put_capability,
                )

                use_inputs = [] if force_refuse is not None else list(body.inputs or [])
                user_cap = mint_user_put_capability() if use_inputs else None
                try:
                    # D3: create_study requires require_hypotheses (no default).
                    # Profile→flag wiring later; API passes False unless caller overrides.
                    _ck = dict(create_kwargs)
                    if "require_hypotheses" in _ck:
                        rh = bool(_ck.pop("require_hypotheses"))
                    else:
                        rh = False
                    created_id = await rt.create_study(
                        profile=body.profile,
                        request=body.request.strip(),
                        catalog_version=body.catalog_version,
                        hitl_policy=body.hitl_policy,
                        deadline=body.deadline,
                        budget=body.budget,
                        finalize_reserve_s=body.finalize_reserve_s,
                        sid=sid,
                        require_hypotheses=rh,
                        inputs=use_inputs,
                        as_of=body.as_of,
                        input_limits=profile_limits,
                        client_input_limits=(
                            None if force_refuse is not None else body.input_limits
                        ),
                        intake_refuse=force_refuse,
                        user_put_capability=user_cap,
                        **_ck,
                    )
                finally:
                    release_user_put_capability(user_cap)
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
            elif refuse_without_catalog and any(
                e.type == "StudyRefused" for e in events
            ):
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
