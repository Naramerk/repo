"""InputLimits, user-input registration, P2 input digests (DESIGN_M1 §9 / PR19).

User bytes land in local CAS. Client ``source_uri`` is recorded separately from
the CAS ``uri`` on ArtifactRegistered. Limits are **profile-owned**; the client
may only LOWER them.

**Scope (M1):** user inputs are **task data only** (``producer=user``,
``role=data``) — not ``quote_in_source`` sources (PR14 ``SourceFetched`` /
``put_fetched`` / ``producer=tool``). HITL upload-as-source deferred.
"""

from __future__ import annotations

import base64
import math
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from coscientist.core.claims.reasons import RefusalKind
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.schema_versions import INPUTS_DIGEST_VERSION
from coscientist.core.execution.archive_limits import inspect_archive
from coscientist.core.execution.domain import (
    Frozen,
    InputBinding,
    Limitation,
    Provenance,
    worst_provenance,
)
from coscientist.core.execution.taint import GetMeta

InputLimitViolation = tuple[RefusalKind, str]

DEFAULT_MAX_BYTES = 50_000_000
DEFAULT_MAX_UNPACK_RATIO = 100.0
DEFAULT_MAX_PAGES = 10_000
DEFAULT_MAX_ROWS = 1_000_000
DEFAULT_MAX_ZIP_MEMBERS = 10_000
# Wire-size budget before decode (base64 expands ~4/3).
DEFAULT_MAX_WIRE_BYTES = DEFAULT_MAX_BYTES

_CLIENT_LIMIT_KEYS = frozenset(
    {
        "max_bytes",
        "max_pages",
        "max_rows",
        "max_zip_members",
        "max_wire_bytes",
        "max_unpack_ratio",
    }
)

QUOTE_SOURCE_PRODUCERS: frozenset[str] = frozenset({"tool"})


def is_quote_source_producer(producer: str | None) -> bool:
    return str(producer or "") in QUOTE_SOURCE_PRODUCERS


class InputLimits(Frozen):
    """Profile caps for user-provided study inputs (DESIGN_M1 §9).

    ``None`` = unlimited (**profile only**). Client may never send None/0/bool;
    client ints must be ``>= 1`` and may only LOWER a numeric profile cap.
    """

    max_bytes: int | None = Field(default=DEFAULT_MAX_BYTES, ge=1)
    max_unpack_ratio: float | None = Field(default=DEFAULT_MAX_UNPACK_RATIO, gt=0)
    max_pages: int | None = Field(default=DEFAULT_MAX_PAGES, ge=1)
    max_rows: int | None = Field(default=DEFAULT_MAX_ROWS, ge=1)
    max_zip_members: int | None = Field(default=DEFAULT_MAX_ZIP_MEMBERS, ge=1)
    max_wire_bytes: int | None = Field(default=DEFAULT_MAX_WIRE_BYTES, ge=1)

    @field_validator(
        "max_bytes",
        "max_pages",
        "max_rows",
        "max_zip_members",
        "max_wire_bytes",
        mode="before",
    )
    @classmethod
    def _no_bool_int(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("bool not allowed for numeric limits")
        return v

    @field_validator("max_unpack_ratio", mode="before")
    @classmethod
    def _finite_ratio(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("bool not allowed for numeric limits")
        if v is None:
            return v
        try:
            f = float(v)
        except (TypeError, ValueError) as e:
            raise ValueError("max_unpack_ratio must be a finite number") from e
        if not math.isfinite(f):
            raise ValueError("max_unpack_ratio must be finite (not NaN/inf)")
        return f


class UserInput(Frozen):
    """One user-provided blob for ``create_study(..., inputs=[...])``."""

    name: str
    content: bytes | str = b""
    media_type: str = "application/octet-stream"
    source_uri: str | None = None
    encoding: Literal["utf-8", "base64", "raw"] = "raw"
    # Client claims ignored for enforcement — pages/rows computed from content.


def default_input_limits() -> InputLimits:
    return InputLimits()


def _reject_client_scalar(
    label: str, raw: Any, *, kind: str
) -> InputLimitViolation | None:
    """Client values: reject bool / 0 / negatives / non-finite; ints must be >= 1."""
    if isinstance(raw, bool):
        return RefusalKind.INPUT_INVALID, f"input_limits.{label} bool forbidden"
    if kind == "int":
        if not isinstance(raw, int):
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} must be int>=1, got {type(raw).__name__}",
            )
        if raw < 1:
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} {raw} < 1 (client cannot disable)",
            )
    elif kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} must be number>0, got {type(raw).__name__}",
            )
        f = float(raw)
        if not math.isfinite(f):
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} must be finite (not NaN/inf)",
            )
        if f <= 0.0:
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} {raw} <= 0 (client cannot disable)",
            )
    return None


def merge_input_limits(
    profile: InputLimits,
    client: InputLimits | dict[str, Any] | None,
) -> InputLimits | InputLimitViolation:
    """Profile owns caps (``None`` = off). Client may only LOWER with ints ``>= 1``.

    Rejects bool, 0, negatives, NaN/inf, unknown keys, and any raise above the
    profile cap.
    """
    if client is None:
        return profile
    if isinstance(client, InputLimits):
        raw_map = client.model_dump()
        # Model dump includes every field — only treat explicitly-lowered as client.
        # Unknown keys cannot appear on a typed model.
    elif isinstance(client, dict):
        raw_map = dict(client)
        unknown = set(raw_map) - _CLIENT_LIMIT_KEYS
        if unknown:
            keys = ", ".join(sorted(str(k) for k in unknown))
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits unknown keys: {keys}",
            )
    else:
        return (
            RefusalKind.INPUT_INVALID,
            f"input_limits_invalid:{type(client).__name__}",
        )

    int_fields = (
        "max_bytes",
        "max_pages",
        "max_rows",
        "max_zip_members",
        "max_wire_bytes",
    )
    out: dict[str, Any] = profile.model_dump()
    for label in int_fields:
        if label not in raw_map:
            continue
        raw = raw_map[label]
        # Client must not send None (disabling is profile-only).
        if raw is None:
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} None forbidden for client",
            )
        bad = _reject_client_scalar(label, raw, kind="int")
        if bad is not None:
            return bad
        cap = getattr(profile, label)
        if cap is not None and int(raw) > int(cap):
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.{label} {raw} > profile {cap}",
            )
        out[label] = int(raw)

    if "max_unpack_ratio" in raw_map:
        raw = raw_map["max_unpack_ratio"]
        if raw is None:
            return (
                RefusalKind.INPUT_INVALID,
                "input_limits.max_unpack_ratio None forbidden for client",
            )
        bad = _reject_client_scalar("max_unpack_ratio", raw, kind="float")
        if bad is not None:
            return bad
        cap_f = profile.max_unpack_ratio
        if cap_f is not None and float(raw) > float(cap_f):
            return (
                RefusalKind.INPUT_INVALID,
                f"input_limits.max_unpack_ratio {raw} > profile {cap_f}",
            )
        out["max_unpack_ratio"] = float(raw)

    try:
        return InputLimits.model_validate(out)
    except Exception as e:  # noqa: BLE001 — pydantic ValidationError → refuse
        return RefusalKind.INPUT_INVALID, f"input_limits_invalid:{type(e).__name__}"


def coerce_user_input(raw: Any) -> UserInput:
    if isinstance(raw, UserInput):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return UserInput(name="input", content=bytes(raw), encoding="raw")
    if isinstance(raw, str):
        return UserInput(name="input", content=raw, encoding="utf-8")
    if isinstance(raw, dict):
        # Drop client pages/rows claims — computed from content later.
        data = {k: v for k, v in raw.items() if k not in ("pages", "rows")}
        return UserInput.model_validate(data)
    raise TypeError(f"unsupported user input type: {type(raw).__name__}")


def decode_user_content(inp: UserInput) -> bytes:
    """Strict decode. base64 uses validate=True — no fallback padding branches."""
    raw = inp.content
    enc = inp.encoding
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if not isinstance(raw, str):
        raise TypeError(f"content must be bytes|str, got {type(raw).__name__}")
    if enc == "base64":
        return base64.b64decode(raw, validate=True)
    if enc == "utf-8":
        return raw.encode("utf-8")
    return raw.encode("utf-8")


def wire_size_of_inputs(inputs: list[Any]) -> int:
    """Encoded wire size before decode (DoS gate)."""
    total = 0
    for raw in inputs or []:
        if isinstance(raw, (bytes, bytearray)):
            total += len(raw)
        elif isinstance(raw, str):
            total += len(raw.encode("utf-8"))
        elif isinstance(raw, dict):
            c = raw.get("content", b"")
            if isinstance(c, (bytes, bytearray)):
                total += len(c)
            elif isinstance(c, str):
                total += len(c.encode("utf-8"))
            total += len(str(raw.get("name") or "").encode("utf-8"))
        elif isinstance(raw, UserInput):
            c = raw.content
            if isinstance(c, (bytes, bytearray)):
                total += len(c)
            else:
                total += len(str(c).encode("utf-8"))
    return total


def count_pages(data: bytes, media_type: str) -> int:
    mt = (media_type or "").lower()
    if not data:
        return 0
    if "pdf" in mt or data[:4] == b"%PDF":
        # Count page objects (heuristic; fail-closed vs claimed pages).
        return max(1, data.count(b"/Type /Page") + data.count(b"/Type/Page"))
    # Form-feed separated / newlines-as-pages for plain text.
    if mt.startswith("text/") or mt in ("", "application/octet-stream"):
        return max(1, data.count(b"\f") + 1) if b"\f" in data else 1
    return 1


def count_rows(data: bytes, media_type: str) -> int:
    mt = (media_type or "").lower()
    if not data:
        return 0
    if "csv" in mt or "tsv" in mt or "text/" in mt:
        # Non-empty lines.
        return sum(1 for line in data.splitlines() if line.strip())
    return 0


def inspect_zip_limits(
    data: bytes,
    limits: InputLimits,
    *,
    name: str = "archive.zip",
) -> InputLimitViolation | None:
    """Streaming zip check (never trusts ZipInfo declared sizes)."""
    return inspect_archive(
        data,
        max_bytes=limits.max_bytes,
        max_unpack_ratio=limits.max_unpack_ratio,
        max_zip_members=limits.max_zip_members,
        name=name,
        media_type="application/zip",
    )


def enforce_request_body_limit(
    raw: bytes | str | bytearray | dict[str, Any],
    *,
    max_wire_bytes: int | None,
) -> InputLimitViolation | None:
    """Gate request size **before** JSON/base64 decode into memory."""
    if max_wire_bytes is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        n = len(raw)
    elif isinstance(raw, dict):
        # Dict path (tests / in-process): estimate encoded size before validate.
        import json as _json

        try:
            n = len(_json.dumps(raw, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            n = len(str(raw).encode("utf-8"))
    else:
        n = len(str(raw).encode("utf-8"))
    if n > max_wire_bytes:
        return (
            RefusalKind.INPUT_TOO_LARGE,
            f"request body {n} > max_wire_bytes {max_wire_bytes}",
        )
    return None


def check_user_inputs_limits(
    inputs: list[Any],
    limits: InputLimits | None = None,
) -> InputLimitViolation | None:
    """Code checks before SPEC.

    Decode/coerce and archive format/decompress failures return a violation.
    Unexpected internal bugs from ``inspect_archive`` propagate.
    """
    lim = limits or default_input_limits()
    # Wire-size gate BEFORE decode into expanded memory.
    wire = wire_size_of_inputs(list(inputs or []))
    if lim.max_wire_bytes is not None and wire > lim.max_wire_bytes:
        return (
            RefusalKind.INPUT_TOO_LARGE,
            f"wire bytes {wire} > max_wire_bytes {lim.max_wire_bytes}",
        )
    try:
        items = [coerce_user_input(x) for x in (inputs or [])]
    except Exception as e:  # noqa: BLE001
        return RefusalKind.INPUT_INVALID, f"malformed_input:{type(e).__name__}"
    total = 0
    for inp in items:
        try:
            data = decode_user_content(inp)
        except Exception as e:  # noqa: BLE001
            return (
                RefusalKind.INPUT_INVALID,
                f"decode_error:{type(e).__name__}:{inp.name}",
            )
        total += len(data)
        if lim.max_bytes is not None and len(data) > lim.max_bytes:
            return (
                RefusalKind.INPUT_TOO_LARGE,
                f"input {inp.name!r} bytes {len(data)} > max_bytes {lim.max_bytes}",
            )
        if lim.max_bytes is not None and total > lim.max_bytes:
            return (
                RefusalKind.INPUT_TOO_LARGE,
                f"total input bytes {total} > max_bytes {lim.max_bytes}",
            )
        pages = count_pages(data, inp.media_type)
        if lim.max_pages is not None and pages > lim.max_pages:
            return (
                RefusalKind.INPUT_TOO_LARGE,
                f"input {inp.name!r} pages {pages} > max_pages {lim.max_pages}",
            )
        rows = count_rows(data, inp.media_type)
        if lim.max_rows is not None and rows > lim.max_rows:
            return (
                RefusalKind.INPUT_TOO_LARGE,
                f"input {inp.name!r} rows {rows} > max_rows {lim.max_rows}",
            )
        # inspect_archive returns violations for format/decompress errors;
        # internal bugs must propagate (no bare Exception swallow).
        bad = inspect_archive(
            data,
            max_bytes=lim.max_bytes,
            max_unpack_ratio=lim.max_unpack_ratio,
            max_zip_members=lim.max_zip_members,
            name=inp.name,
            media_type=inp.media_type,
        )
        if bad is not None:
            return bad
    return None


def inputs_digest_of(bindings: list[InputBinding] | list[dict[str, Any]]) -> str:
    """``Attempt.inputs_digest = sha256(JCS(sorted bindings))`` — no retrieved_at."""
    rows: list[dict[str, Any]] = []
    for b in bindings:
        if isinstance(b, dict):
            row = {
                "input": b.get("input"),
                "artifact_id": b.get("artifact_id") or "",
                "source": b.get("source"),
                "kind": b.get("kind", "artifact"),
                "ref": b.get("ref"),
                "digest": b.get("digest"),
                "digest_version": b.get("digest_version"),
                "source_uri": b.get("source_uri"),
            }
        else:
            row = {
                "input": b.input,
                "artifact_id": b.artifact_id or "",
                "source": b.source,
                "kind": b.kind,
                "ref": b.ref,
                "digest": b.digest,
                "digest_version": b.digest_version,
                "source_uri": b.source_uri,
            }
        rows.append(row)
    rows.sort(
        key=lambda r: (
            str(r.get("input") or ""),
            str(r.get("kind") or ""),
            str(r.get("ref") or ""),
        )
    )
    return sha256_hex(canonical_json(rows))


def bindings_have_null_digest(
    bindings: list[InputBinding] | list[dict[str, Any]] | None,
) -> bool:
    if not bindings:
        return False
    for b in bindings:
        if isinstance(b, dict):
            if b.get("digest") is None:
                return True
        elif b.digest is None:
            return True
    return False


def bindings_have_stale_digest_version(
    bindings: list[InputBinding] | list[dict[str, Any]] | None,
) -> bool:
    """True when any binding lacks current ``digest_version`` (legacy formula)."""
    if not bindings:
        return False
    for b in bindings:
        if isinstance(b, dict):
            ver = b.get("digest_version")
        else:
            ver = b.digest_version
        if ver is None:
            return True
        try:
            ver_i = int(ver)
        except (TypeError, ValueError):
            return True
        if ver_i < INPUTS_DIGEST_VERSION:
            return True
    return False


def snapshot_bindings_at_p2(
    bindings: list[InputBinding],
    *,
    now: datetime,
    get_meta: GetMeta | None = None,
) -> tuple[list[InputBinding], list[Limitation]]:
    """Stamp retrieved_at + CAS content digest; keep client ``source_uri``.

    ``get_meta`` errors **propagate** (never silently None). Missing meta →
    ``input_digest_unavailable`` limitation. Never copies CAS ``file://`` into
    ``source_uri``.
    """
    out: list[InputBinding] = []
    lims: list[Limitation] = []
    for b in bindings:
        updates: dict[str, Any] = {
            "retrieved_at": now,
            "digest_version": INPUTS_DIGEST_VERSION,
        }
        if (
            b.kind == "artifact"
            and b.artifact_id
            and not str(b.artifact_id).startswith("stub:")
        ):
            if get_meta is None:
                lims.append(
                    Limitation(text="input_digest_unavailable", ref=b.artifact_id)
                )
            else:
                meta = get_meta(b.artifact_id)  # may raise — caller must not swallow
                sha = None
                if meta is not None:
                    sha = getattr(meta, "sha256", None) or (
                        meta.get("sha256") if isinstance(meta, dict) else None
                    )
                if sha:
                    updates["digest"] = str(sha)
                else:
                    lims.append(
                        Limitation(
                            text="input_digest_unavailable",
                            ref=b.artifact_id,
                        )
                    )
        out.append(b.model_copy(update=updates))
    return out, lims


def apply_input_drift(
    *,
    provenance: Provenance,
    limitations: list[Limitation],
    inputs_digest: str,
    previous_inputs_digest: str | None,
    previous_bindings: list[InputBinding] | list[dict[str, Any]] | None = None,
) -> tuple[Provenance, list[Limitation]]:
    """Drift vs prior attempt.

    Legacy bindings with ``digest=None`` or absent/old ``digest_version`` →
    ``input_drift_unknown`` **without** forcing provenance=partial.
    """
    if previous_bindings is not None and (
        bindings_have_null_digest(previous_bindings)
        or bindings_have_stale_digest_version(previous_bindings)
    ):
        lims = list(limitations)
        lims.append(
            Limitation(
                text="input_drift_unknown",
                ref="legacy_digest_or_version",
            )
        )
        return provenance, lims
    if not previous_inputs_digest or previous_inputs_digest == inputs_digest:
        return provenance, limitations
    lims = list(limitations)
    lims.append(
        Limitation(
            text="input_drift",
            ref=f"prev={previous_inputs_digest[:12]}… curr={inputs_digest[:12]}…",
        )
    )
    return worst_provenance(provenance, "partial"), lims


def previous_attempt_inputs_state(
    attempts: dict[str, dict[str, Any]],
    task_attempt_ids: list[str],
    *,
    exclude_aid: str | None = None,
) -> tuple[str | None, list[Any] | None]:
    """Return ``(inputs_digest, bindings)`` of the most recent prior attempt."""
    for aid in reversed(list(task_attempt_ids)):
        if exclude_aid and aid == exclude_aid:
            continue
        att = attempts.get(aid) or {}
        bindings = att.get("inputs") or []
        dig = att.get("inputs_digest")
        if dig:
            return str(dig), list(bindings) if bindings else None
        if bindings:
            return inputs_digest_of(bindings), list(bindings)
    return None, None


def previous_attempt_inputs_digest(
    attempts: dict[str, dict[str, Any]],
    task_attempt_ids: list[str],
    *,
    exclude_aid: str | None = None,
) -> str | None:
    dig, _ = previous_attempt_inputs_state(
        attempts, task_attempt_ids, exclude_aid=exclude_aid
    )
    return dig


def study_user_input_bindings(
    *,
    user_input_ids: list[str],
    now: datetime,
    get_meta: GetMeta,
) -> list[InputBinding]:
    """Bind Study.user_input_ids into InputBinding rows for DAG attempts / drift."""
    out: list[InputBinding] = []
    for aid in user_input_ids or []:
        if not aid:
            continue
        meta = get_meta(aid)  # propagate errors
        if meta is None:
            raise LookupError(f"user_input artifact missing: {aid}")
        sha = getattr(meta, "sha256", None) or (
            meta.get("sha256") if isinstance(meta, dict) else None
        )
        name = getattr(meta, "name", None) or (
            meta.get("name") if isinstance(meta, dict) else None
        )
        out.append(
            InputBinding(
                input=f"user:{name or aid}",
                artifact_id=str(aid),
                source="real",
                kind="artifact",
                digest=str(sha) if sha else None,
                digest_version=INPUTS_DIGEST_VERSION,
                retrieved_at=now,
                source_uri=None,  # client URI stays on ArtifactRegistered only
            )
        )
    return out
