"""Fail-closed reads of AttemptStarted forever-fields (NeedUnrecorded-style).

``AttemptUnrecorded`` is an explicit legacy gap — never coerce to a writer
value. Consumers ``match`` + ``assert_never`` and raise
``AttemptUnrecordedRefuse`` at use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, assert_never

from pydantic import BaseModel

from coscientist.core.events.payload_schemas import AttemptUnrecorded

AttemptUnrecordedRefuseReason = Literal["attempt_field_unrecorded"]


class AttemptUnrecordedRefuse(ValueError):
    """Typed refuse: forever-field was never recorded in the journal."""

    def __init__(self, field: str, *, source_schema_version: int) -> None:
        self.field = field
        self.source_schema_version = source_schema_version
        self.reason = "attempt_field_unrecorded"
        super().__init__(f"attempt_field_unrecorded:{field}:source_schema_version={source_schema_version}")


def _parse_unrecorded(raw: object) -> AttemptUnrecorded | None:
    if isinstance(raw, AttemptUnrecorded):
        return raw
    if isinstance(raw, dict) and raw.get("kind") == "unrecorded":
        return AttemptUnrecorded.model_validate(raw)
    return None


def parse_attempt_bool(raw: object) -> bool | AttemptUnrecorded:
    unrec = _parse_unrecorded(raw)
    if unrec is not None:
        return unrec
    if type(raw) is bool:
        return raw
    raise TypeError(f"attempt forever-field: expected bool|AttemptUnrecorded, got {raw!r}")


def parse_attempt_str(raw: object) -> str | AttemptUnrecorded:
    unrec = _parse_unrecorded(raw)
    if unrec is not None:
        return unrec
    if type(raw) is str:
        return raw
    raise TypeError(f"attempt forever-field: expected str|AttemptUnrecorded, got {raw!r}")


def parse_attempt_int(raw: object) -> int | AttemptUnrecorded:
    unrec = _parse_unrecorded(raw)
    if unrec is not None:
        return unrec
    if type(raw) is int:
        return raw
    raise TypeError(f"attempt forever-field: expected int|AttemptUnrecorded, got {raw!r}")


def require_attempt_bool(raw: object, *, field: str) -> bool:
    value = parse_attempt_bool(raw)
    match value:
        case AttemptUnrecorded(source_schema_version=v):
            raise AttemptUnrecordedRefuse(field, source_schema_version=v)
        case bool() as b:
            return b
        case _ as unreachable:
            assert_never(unreachable)


def require_attempt_str(raw: object, *, field: str) -> str:
    value = parse_attempt_str(raw)
    match value:
        case AttemptUnrecorded(source_schema_version=v):
            raise AttemptUnrecordedRefuse(field, source_schema_version=v)
        case str() as s:
            return s
        case _ as unreachable:
            assert_never(unreachable)
def attempt_mapping(attempt: Mapping[str, object] | BaseModel) -> Mapping[str, object]:
    """Normalize Attempt model or projection dict to a mapping (no getattr)."""
    if isinstance(attempt, BaseModel):
        return attempt.model_dump(mode="python")
    if isinstance(attempt, Mapping):
        return attempt
    raise TypeError(
        f"attempt must be Mapping|BaseModel, got {type(attempt).__name__}"
    )


def attempt_get(attempt: Mapping[str, object] | BaseModel, name: str) -> object:
    """Read a forever-field from an Attempt model or projection mapping."""
    row = attempt_mapping(attempt)
    if name not in row:
        raise KeyError(name)
    return row[name]


def attempt_closed(attempt: Mapping[str, object] | BaseModel) -> bool:
    """``closed`` forever-field — refuse AttemptUnrecorded (never truthy-coerce)."""
    return require_attempt_bool(attempt_get(attempt, "closed"), field="closed")


def attempt_row_is_open(attempts: Mapping[str, object], aid: str) -> bool:
    """True when attempt row is present and ``closed`` is recorded False.

    Missing row → open (same as legacy ``not {}.get("closed")``). Unrecorded
    ``closed`` → ``AttemptUnrecordedRefuse``.
    """
    if not isinstance(attempts, Mapping) or aid not in attempts:
        return True
    row = attempts[aid]
    if not isinstance(row, Mapping):
        raise TypeError(
            f"attempt row must be a mapping, got {type(row).__name__}"
        )
    return not attempt_closed(row)


# Forever-fields on AttemptStartedPayload that may be AttemptUnrecorded.
ATTEMPT_FOREVER_FIELDS: frozenset[str] = frozenset(
    {"n", "dispatch_id", "route_returned", "closed", "provenance", "study_id"}
)


def project_attempt_started_payload(payload: dict[str, object]) -> dict[str, object]:
    """Copy AttemptStarted payload for projection without reading unrecorded as values.

    Each forever-field is matched: recorded scalars pass through; unrecorded stays
    the explicit marker dict. Never coerce unrecorded → bool/str/int defaults.
    """
    out = dict(payload)
    for field in ATTEMPT_FOREVER_FIELDS:
        if field not in out:
            continue
        raw = out[field]
        unrec = _parse_unrecorded(raw)
        if unrec is not None:
            out[field] = unrec.model_dump(mode="json")
            continue
        if field in ("closed", "route_returned"):
            parse_attempt_bool(raw)
        elif field == "n":
            parse_attempt_int(raw)
        else:
            # dispatch_id | provenance | study_id
            parse_attempt_str(raw)
    return out
