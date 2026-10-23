"""Private user-input ingest gate (DESIGN_M1 §9 / PR19).

``UserPutCapability`` is minted only on the ``POST /studies`` intake path
(``api.studies``). ``_store(producer=\"user\")`` requires a live capability.
import-linter: only ``api.studies`` and ``core.lifecycle`` may import this
module (ignore those edges; every other ``coscientist.*`` module is a source).

Registry state lives in a closure cell (not a module-global ``_issued`` dict)
so casual ``sys.modules`` / ``setattr`` on this module cannot inject
``nonce→token``. Residual: any in-process caller that can reach
``mint_user_put_capability.__closure__`` (or replace ``verify_user_put_capability``)
can still forge entries — **not** a security boundary against same-process
code. Same trust as the rest of ``coscientist.core``; sandboxed agent/tool
code is not given that import.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from coscientist.core.claims.reasons import RefusalKind
from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.events.schema_versions import ARTIFACT_REGISTERED_SCHEMA_VERSION
from coscientist.core.execution._attempt_frame import attempt_active
from coscientist.core.execution.domain import Artifact


def _make_issued_registry() -> tuple[
    Any, Any, Any
]:
    """Closure-cell registry — not a module attribute named ``_issued``."""
    data: dict[str, str] = {}

    def put(nonce: str, token: str) -> None:
        data[nonce] = token

    def pop(nonce: str) -> None:
        data.pop(nonce, None)

    def get(nonce: str) -> str | None:
        return data.get(nonce)

    return put, pop, get


_issued_put, _issued_pop, _issued_get = _make_issued_registry()


@dataclass(frozen=True, slots=True)
class UserPutCapability:
    """Opaque capability; construct only via ``mint_user_put_capability``.

    Verification is module-level (``verify_user_put_capability``) — never trust
    methods on a caller-supplied object.
    """

    _nonce: str
    _token: str


def mint_user_put_capability() -> UserPutCapability:
    """Mint a capability for one study create / input registration."""
    nonce = secrets.token_hex(16)
    token = secrets.token_hex(32)
    _issued_put(nonce, token)
    return UserPutCapability(_nonce=nonce, _token=token)


def release_user_put_capability(cap: UserPutCapability | None) -> None:
    if cap is None:
        return
    if type(cap) is UserPutCapability:
        _issued_pop(cap._nonce)


def verify_user_put_capability(cap: object | None) -> bool:
    """Exact-type + hmac against closure registry — no ``isinstance`` / methods."""
    if type(cap) is not UserPutCapability:
        return False
    expected = _issued_get(cap._nonce)
    if expected is None:
        return False
    return hmac.compare_digest(expected, cap._token)


def put_user(
    store: Any,
    content: bytes,
    produced_by: Any,
    *,
    capability: UserPutCapability,
    name: str,
    media_type: str,
    schema_ref: str | None = None,
    role: str = "data",
    attempt_closed: bool = False,
) -> Artifact:
    """Store user study **task data** (``producer=user``, forced ``role=data``).

    Requires a live ``UserPutCapability`` and no attempt frame (no taint
    laundering). Quote sources use ``put_fetched`` / ``SourceFetched`` only.
    """
    if not verify_user_put_capability(capability):
        raise ArtifactRejected("user_put_capability_required")
    if attempt_active():
        raise ArtifactRejected("put_user_requires_ingest_scope")
    _ = role  # forced data — client must not pick paper/figure
    return store._store(
        content,
        produced_by,
        name=name,
        media_type=media_type,
        schema_ref=schema_ref,
        role="data",
        attempt_closed=attempt_closed,
        producer="user",
        tainted=False,
        user_put=capability,
    )


class UserInputRegistrationError(Exception):
    """Fail-closed: user inputs present but CAS registration cannot complete."""

    def __init__(
        self, reason: str, *, kind: RefusalKind = RefusalKind.INPUT_INVALID
    ) -> None:
        self.reason = reason
        self.kind = kind
        super().__init__(reason)


def register_user_input_artifacts(
    inputs: list[Any],
    *,
    study_id: str,
    now: datetime,
    artifacts: Any,
    capability: UserPutCapability | None,
) -> tuple[list[str], list[Any]]:
    """Put user bytes into CAS + ``ArtifactRegistered`` (CAS uri + client source_uri).

    Requires ``capability`` from ``mint_user_put_capability``. Empty inputs →
    ``([], [])``. Missing store / decode/put failure → refuse INPUT_INVALID.
    """
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.input_limits import coerce_user_input, decode_user_content

    if not inputs:
        return [], []
    if not verify_user_put_capability(capability):
        raise UserInputRegistrationError(
            "user_put_capability_required",
            kind=RefusalKind.INPUT_INVALID,
        )
    assert isinstance(capability, UserPutCapability)
    if artifacts is None or not hasattr(artifacts, "_store"):
        raise UserInputRegistrationError(
            "artifact_store_unavailable",
            kind=RefusalKind.INPUT_INVALID,
        )
    ids: list[str] = []
    events: list[Any] = []
    for i, raw in enumerate(inputs):
        try:
            inp = coerce_user_input(raw)
            data = decode_user_content(inp)
        except Exception as e:  # noqa: BLE001 — decode/validate → refuse
            name = getattr(raw, "name", None) or (
                raw.get("name") if isinstance(raw, dict) else f"idx{i}"
            )
            raise UserInputRegistrationError(
                f"decode_error:{type(e).__name__}:{name}",
                kind=RefusalKind.INPUT_INVALID,
            ) from e
        ref = make_tool_ref(
            call_id=f"user-input:{study_id}:{i}:{inp.name}",
            tool="user_input",
            args=inp.name,
        )
        try:
            art = put_user(
                artifacts,
                data,
                ref,
                capability=capability,
                name=inp.name,
                media_type=inp.media_type or "application/octet-stream",
                role="data",
            )
        except Exception as e:  # noqa: BLE001 — store deny/IO → refuse
            raise UserInputRegistrationError(
                f"put_user_failed:{type(e).__name__}:{inp.name}",
                kind=RefusalKind.INPUT_INVALID,
            ) from e
        payload = art.model_dump(mode="json")
        payload["source_uri"] = inp.source_uri
        payload["uri"] = art.uri
        events.append(
            EventEnvelope(
                study_id=study_id,
                type="ArtifactRegistered",
                entity_key=art.artifact_id,
                idem_key=make_idem("ArtifactRegistered", art.artifact_id),
                payload=payload,
                ts=now,
                schema_version=ARTIFACT_REGISTERED_SCHEMA_VERSION,
            )
        )
        ids.append(art.artifact_id)
    return ids, events
