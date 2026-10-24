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
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from coscientist.core.claims.reasons import RefusalKind
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import Conflict, LeaseLost
from coscientist.core.execution.domain import Budget, HitlPolicy, StudyConfig
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
# Floor when waiting for a peer that holds the create lease (platform, not Study domain).
_PEER_WAIT_FLOOR_S = 30.0

# Minimal StudyConfig for INPUT_INVALID refuse path only — numbers are platform
# refuse stubs so create_study can finalize(refused); not wire/API invents.
_REFUSE_EVALUE_ALPHA = 0.05
_REFUSE_REQUEST = "invalid"


class CreateStudyBody(BaseModel):
    """Request body for ``POST /studies``.

    ``config`` (StudyConfig) and ``hitl_policy`` are required — no silent
    domain-number or HITL-timeout defaults in core ``create_study``.
    """

    model_config = ConfigDict(extra="forbid")

    profile: str
    request: str
    catalog_version: str
    config: StudyConfig
    hitl_policy: HitlPolicy
    deadline: datetime
    budget: Budget | None = None
    # PR19 / DESIGN_M1 §9: user blobs → CAS before SPEC; as_of for time-sensitive Qs.
    # Profile owns InputLimits; client may only LOWER via this optional dict.
    # Required from request body (empty list is explicit client choice — no Field default).
    inputs: list[dict[str, Any]]
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
    profile = profile_limits or default_input_limits()  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
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
            return None, (
                RefusalKind.INPUT_INVALID,
                f"json:{type(e).__name__}",
            )  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class
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


def _require_study_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise Conflict(f"idempotency mapping study_id must be non-empty str, got {raw!r}")
    return raw


def _idem_path(data_root: Path, key: str) -> Path:
    digest = sha256_hex(key)
    return Path(data_root) / "idempotency" / "studies" / digest


def _owner_sidecar_path(path: Path) -> Path:
    """Sidecar written before the mapping body so owner is reachable mid-write."""
    return Path(str(path) + ".owner")


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


def _read_owner_sidecar(path: Path) -> tuple[str, int] | None:
    """Return ``(hostname, pid)`` from the sidecar, if present and parseable."""
    side = _owner_sidecar_path(path)
    try:
        raw = side.read_text(encoding="utf-8").strip()
        data = json.loads(raw)
        host_raw = data.get("hostname")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        if host_raw is None:
            host = ""
        elif not isinstance(host_raw, str):
            return None
        else:
            host = host_raw
        pid = data.get("owner_pid")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        if host and isinstance(pid, int) and pid > 0:
            return host, pid
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class
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
                pass  # audit-ok[except_pass]: best-effort secondary close after primary error path


def _unlink_reservation(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # audit-ok[except_pass]: best-effort secondary close after primary error path
    try:
        _owner_sidecar_path(path).unlink(missing_ok=True)
    except OSError:
        pass  # audit-ok[except_pass]: best-effort secondary close after primary error path


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
        host_raw = existing.get("hostname")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        if host_raw is None:
            host = ""
        elif not isinstance(host_raw, str):
            return False
        else:
            host = host_raw
        pid = existing.get("owner_pid")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
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
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # audit-ok[except_pass]: best-effort secondary close after primary error path
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass  # audit-ok[except_pass]: best-effort secondary close after primary error path


async def _reserve_key(path: Path, *, study_id: str, request_digest: str, key: str) -> tuple[dict[str, Any], bool]:
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
                    f"Idempotency-Key reservation busy or unreadable (not yet stale); retry later (key={key!r})"
                ) from None
            if existing.get("request_digest") != request_digest:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
                raise Conflict(f"Idempotency-Key reused with a different POST /studies body (key={key!r})") from None
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


async def _wait_complete(path: Path, *, timeout_s: float = _WAIT_COMPLETE_S) -> dict[str, Any]:  # audit-ok[named_default]: named constant / enum member default; not silent invent  # noqa: E501
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        existing = _read_mapping(path)
        if existing and existing.get("status") == "complete":  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
            return existing
        await asyncio.sleep(0.05)
    existing = _read_mapping(path)
    if existing and existing.get("status") == "complete":  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
        return existing
    raise Conflict(f"Idempotency-Key reservation timed out waiting for complete: {path}")



def _platform_input_invalid_stub_body() -> CreateStudyBody:
    """Platform-only stub so INPUT_INVALID still lands StudyFinalized(refused).

    Not a CreateStudyBody Field default: wire bodies must supply profile /
    catalog_version / config / hitl_policy / deadline / inputs explicitly.
    Numbers here are refuse-path platform constants only (see DESIGN intake).
    """
    from datetime import UTC, timedelta

    from coscientist.core.execution.domain import GateConfig

    now = datetime.now(UTC)
    return CreateStudyBody(
        profile="research",
        request=_REFUSE_REQUEST,
        catalog_version="test",
        inputs=[],
        config=StudyConfig(
            finalize_reserve_s=30,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            aggregate="count",
            evalue_alpha=_REFUSE_EVALUE_ALPHA,
            max_expansions=2,
            max_human_expansions=10,
            replan_reserve=0.1,
            max_tasks=64,
            max_depth=8,
            plan_timeout_s=600,
            max_foreach_children=32,
            max_loop_iter=8,
            max_task_budget_share=1.0,
            max_proposals=3,
        ),
        hitl_policy=HitlPolicy(
            gates={
                "spec_approval": GateConfig(
                    timeout_s=120, on_timeout="delegate", enabled=True
                ),
            },
            mode="headless",
            timeout_s={},
            on_timeout=None,
            defaults={
                "plan_approval": "human_timeout",
                "result_acceptance": "accept_computed",
                "skip_task": "deny",
                "side_effect": "deny",
                "proposed_check": "reject",
            },
        ),
        deadline=now + timedelta(hours=2),
    )


async def post_studies(
    data_root: Path | str,
    body: CreateStudyBody | dict[str, Any] | bytes | str,
    *,
    idempotency_key: str | None = None,
    catalog: Any,
    wait_complete_s: float = _WAIT_COMPLETE_S,  # audit-ok[named_default]: named constant / enum member default; not silent invent  # noqa: E501
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
    profile_limits = profile_input_limits or default_input_limits()  # audit-ok[or_call]: coalesce existing call result; no invented literal  # noqa: E501
    parsed = parse_create_study_body(body, profile_limits=profile_limits)
    refuse_kind: RefusalKind | None = None
    refuse_reason: str | None = None
    if parsed[0] is None:
        refuse_kind, refuse_reason = parsed[1]
        # Minimal body so idempotency digest + create_study still run.
        # Domain numbers / HITL gates still explicit (no core silent defaults).
        body = _platform_input_invalid_stub_body()
    else:
        body, _effective = parsed
        _ = _effective  # validated; create_study re-merges client vs profile
    req_digest = _request_digest(body)

    if idempotency_key:
        sid = study_id_from_idempotency_key(idempotency_key)
        path = _idem_path(root, idempotency_key)
        mapping, reserved = await _reserve_key(path, study_id=sid, request_digest=req_digest, key=idempotency_key)
        if mapping.get("status") == "complete":  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
            return {"study_id": _require_study_id(mapping["study_id"])}
        if not reserved:
            try:
                done = await _wait_complete(path, timeout_s=wait_complete_s)
                return {"study_id": _require_study_id(done["study_id"])}
            except Conflict:
                sid = _require_study_id(mapping["study_id"])
        else:
            sid = _require_study_id(mapping["study_id"])
    else:
        from coscientist.core.events.ids import study_id as new_study_id

        sid = new_study_id()
        path = None

    async def _wait_for_peer() -> dict[str, str]:
        if path is None:
            raise Conflict(f"lease lost creating study {sid} without idempotency path")
        peer_timeout = wait_complete_s * 10
        if peer_timeout < _PEER_WAIT_FLOOR_S:
            peer_timeout = _PEER_WAIT_FLOOR_S
        done = await _wait_complete(path, timeout_s=peer_timeout)
        return {"study_id": _require_study_id(done["study_id"])}

    try:
        from coscientist.api.runtime import StudyRuntime as ApiStudyRuntime
        from coscientist.core.claims.graph import default_execution_registry
        from coscientist.core.events.clock import SystemClock
        from coscientist.core.execution.machine import new_owner

        rt = await open_runtime(
            root,
            sid,
            catalog=catalog,
            runtime_cls=ApiStudyRuntime,
            clock=SystemClock(),
            owner=new_owner(),
            registry=default_execution_registry(),
        )
        if not isinstance(rt, ApiStudyRuntime):
            raise TypeError(f"open_runtime must return ApiStudyRuntime, got {type(rt).__name__}")
    except LeaseLost:
        if idempotency_key:
            return await _wait_for_peer()
        raise

    created_id: str | None = None
    try:
        try:
            from coscientist.core.plan.intake import IntakeRefuse

            if refuse_kind is not None:
                if refuse_reason is None:
                    reason_s = _REFUSE_REQUEST
                else:
                    reason_s = refuse_reason
                force_refuse = IntakeRefuse(refuse_kind, reason_s[:200])
            else:
                force_refuse = None
            from coscientist.core.execution._ingest_gate import (
                mint_user_put_capability,
                release_user_put_capability,
            )

            if force_refuse is not None:
                use_inputs: list[dict[str, Any]] = []  # audit-ok[field_default]: structural empty/collector; not invented domain value  # noqa: E501
            else:
                use_inputs = list(body.inputs)
            user_cap = mint_user_put_capability() if use_inputs else None  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
            req = body.request.strip()
            if not req:
                req = _REFUSE_REQUEST
            try:
                created_id = await rt.create_study(
                    profile=body.profile,
                    request=req,
                    catalog_version=body.catalog_version,
                    config=body.config,
                    hitl_policy=body.hitl_policy,
                    deadline=body.deadline,
                    budget=body.budget,
                    sid=sid,
                    inputs=use_inputs,
                    as_of=body.as_of,
                    input_limits=profile_limits,
                    client_input_limits=(None if force_refuse is not None else body.input_limits),  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
                    intake_refuse=force_refuse,
                    user_put_capability=user_cap,
                    min_plan_cost=0,
                    **create_kwargs,
                )
            finally:
                release_user_put_capability(user_cap)
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
