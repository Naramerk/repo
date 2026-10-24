"""quote_in_source CodeCheck (DESIGN_M1 §6.3 / K9).

Normalize quote + CAS source text, then token-boundary (or CJK/Thai substring)
match. Never raises for scientific failure — returns FAILED / UNASSESSED with a
stable reason.

Sources must be journaled ``SourceFetched`` digests (``is_fetch`` tool via
capability-gated ``put_fetched``). Only ``is_quote_source_producer`` (``tool``)
is accepted — agent ``put`` / ``put_sandbox`` / ``put_user`` (PR19) are
rejected. Network content stays ``tainted=True``; journal match proves
provenance only — content remains untrusted. ``countable`` only when that
journal match holds.

``fetched_digests`` is **study-scoped** (all SourceFetched in the study).

**Limitation (polarity):** this check verifies quote *presence* after
normalization, not whether surrounding context negates the quote. See
``quote_norm`` module docs.
"""

from __future__ import annotations

import json
from typing import Any, Collection

from coscientist.checks.kinds.quote_norm import (
    MAX_SOURCE_BYTES,
    MIN_QUOTE_WORD_TOKENS,
    NORMALIZATION_VERSION,
    NormalizedSource,
    cas_text_from_bytes,
    normalize_for_quote_match,
    normalize_source_cached,
    quote_in_normalized_source,
    quote_token_ok,
)
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import ArtifactCorrupt
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    CheckResult,
    CheckRole,
    CodeCheck,
    EvidenceRef,
    ReqStatus,
    SourceQuote,
)
from coscientist.core.execution.input_limits import is_quote_source_producer

# Re-export for tests / callers.
__all__ = [
    "NORMALIZATION_VERSION",
    "MIN_QUOTE_WORD_TOKENS",
    "evaluate_quote_in_source",
    "normalize_for_quote_match",
    "quote_digest",
]


def _belongs_to_attempt(meta: Any | None, attempt_id: str | None) -> bool:
    if meta is None or not attempt_id:
        return False
    from coscientist.core.claims.verdict import call_id_belongs_to_attempt

    produced = getattr(meta, "produced_by", None)
    if produced is None:
        return False
    return call_id_belongs_to_attempt(produced, attempt_id)


def _no_effect(
    *,
    check_id: str,
    template_id: str,
    role: CheckRole,
    reason: str,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
        role=role,
        detail=reason,
        effect=None,
        used_sesoi=False,
    )


def _unassessed(
    *,
    check_id: str,
    template_id: str,
    role: CheckRole,
    reason: str,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.UNASSESSED,
        is_code=True,
        required=True,
        role=role,
        detail=reason,
        effect=None,
        used_sesoi=False,
    )


def quote_digest(quote: str) -> str:
    return sha256_hex(canonical_json(quote))


def _parse_source_quote(raw: Any) -> SourceQuote | None:
    if not isinstance(raw, dict):
        return None
    from pydantic import ValidationError

    data = dict(raw)
    quote = data.get("quote")
    if not isinstance(quote, str):
        return None
    if not data.get("digest"):
        data["digest"] = quote_digest(quote)
    if data.get("locator") is None:
        data["locator"] = ""
    # source_sha256 required by model
    if not data.get("source_sha256"):
        return None
    try:
        return SourceQuote.model_validate(data)
    except ValidationError:
        # Malformed quote payload → honest quote_payload_invalid upstream.
        return None


def _load_quotes(
    check: CodeCheck,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    *,
    attempt_id: str | None,
) -> tuple[list[SourceQuote] | None, str | None]:
    """JSON output slot only — values.source_quote removed (no MET without artifact)."""
    slot = check.output or "quote"
    handle = out.outputs.get(slot)
    if handle is None:
        return None, "output_not_submitted"
    meta = artifacts.get_meta(handle.artifact_id)
    if meta is None:
        return None, "artifact_missing"
    if attempt_id and not _belongs_to_attempt(meta, attempt_id):
        return None, "foreign_artifact"
    try:
        blob = artifacts.read(handle.artifact_id)
    except (FileNotFoundError, ArtifactCorrupt):
        return None, "artifact_missing"
    try:
        raw_doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "quote_payload_invalid"

    collected: list[SourceQuote] = []
    if isinstance(raw_doc, dict) and "quotes" in raw_doc:
        items = raw_doc["quotes"]
        if not isinstance(items, list) or not items:
            return None, "quote_missing"
        for item in items:
            sq = _parse_source_quote(item)
            if sq is None:
                return None, "quote_payload_invalid"
            collected.append(sq)
    else:
        sq = _parse_source_quote(raw_doc)
        if sq is None:
            return None, "quote_payload_invalid"
        collected.append(sq)
    return collected, None


# Cache value: NormalizedSource | failure reason string sentinel via tuple.
_SourceCacheVal = tuple[NormalizedSource | None, str | None]


def _source_cache_get(
    cache: dict[str, _SourceCacheVal],
    artifacts: LocalArtifactStore,
    artifact_id: str,
) -> _SourceCacheVal:
    """Return (NormalizedSource|None, reason|None); normalize once per artifact."""
    if artifact_id in cache:
        return cache[artifact_id]
    meta = artifacts.get_meta(artifact_id)
    if meta is None:
        cache[artifact_id] = (None, "artifact_missing")
        return cache[artifact_id]
    if meta.size > MAX_SOURCE_BYTES:
        cache[artifact_id] = (None, "source_too_large")
        return cache[artifact_id]
    try:
        raw = artifacts.read(artifact_id)
    except FileNotFoundError:
        cache[artifact_id] = (None, "artifact_missing")
        return cache[artifact_id]
    except ArtifactCorrupt as e:
        cache[artifact_id] = (None, str(e))
        return cache[artifact_id]
    if len(raw) > MAX_SOURCE_BYTES:
        cache[artifact_id] = (None, "source_too_large")
        return cache[artifact_id]
    text = cas_text_from_bytes(raw)
    if text is None:
        cache[artifact_id] = (None, "source_not_text")
        return cache[artifact_id]
    cache[artifact_id] = (normalize_source_cached(text), None)
    return cache[artifact_id]


def _match_one(
    sq: SourceQuote,
    artifacts: LocalArtifactStore,
    *,
    fetched_digests: Collection[str],
    source_cache: dict[str, _SourceCacheVal],
) -> tuple[str, str | None, EvidenceRef | None, dict[str, Any] | None]:
    """Return (status, detail, evidence, effect).

    status: ``met`` | ``failed`` | ``unassessed`` | ``no_effect``.
    """
    if not sq.quote.strip():
        return "failed", "quote_empty", None, None

    expected_digest = quote_digest(sq.quote)
    if sq.digest != expected_digest:
        return "failed", "quote_digest_mismatch", None, None

    # Missing version ≠ current — UNASSESSED (replay-safe).
    if sq.normalization_version is None:
        return "unassessed", "normalization_version_mismatch", None, None
    if sq.normalization_version != NORMALIZATION_VERSION:
        return "unassessed", "normalization_version_mismatch", None, None

    meta = artifacts.get_meta(sq.artifact_id)
    if meta is None:
        return "no_effect", "artifact_missing", None, None

    producer = getattr(meta, "producer", "agent")
    if not is_quote_source_producer(producer):
        return "failed", "source_not_fetched", None, None

    # Required source_sha256 must match artifact CAS and journaled fetch.
    # Taint is orthogonal: network fetches stay tainted=True; journal match
    # proves provenance only (content remains untrusted).
    if sq.source_sha256 != meta.sha256:
        return "failed", "source_sha256_mismatch", None, None
    if meta.sha256 not in fetched_digests:
        return "failed", "source_not_fetched", None, None
    if sq.source_sha256 not in fetched_digests:
        return "failed", "source_not_fetched", None, None

    norm_src, read_reason = _source_cache_get(source_cache, artifacts, sq.artifact_id)
    if read_reason is not None:
        if read_reason in ("artifact_missing",):
            return "no_effect", read_reason, None, None
        return "failed", read_reason, None, None
    assert norm_src is not None

    norm_quote = normalize_for_quote_match(sq.quote)
    token_reason = quote_token_ok(norm_quote)
    if token_reason is not None:
        return "failed", token_reason, None, None

    if not quote_in_normalized_source(norm_quote, norm_src):
        return "failed", "quote_not_in_source", None, None

    evid = EvidenceRef(
        artifact_id=sq.artifact_id,
        json_pointer=None,
        digest=meta.sha256,
    )
    # Countable via journaled SourceFetched match on this attempt (PR8b /
    # DESIGN_M1 §6.3): core-evaluated presence evidence. trusted stays False —
    # network bytes remain untrusted even when countable.
    pb = getattr(meta, "produced_by", None)
    pb_aid = getattr(pb, "attempt_id", None) if pb is not None else None
    effect = {
        "producer": "tool",
        "trusted": False,
        "tainted": bool(getattr(meta, "tainted", False)),
        "core_evaluated": True,
        "produced_by_call_id": (
            ""
            if pb is None or getattr(pb, "call_id", None) is None
            else str(pb.call_id)
        ),
        "artifact_id": sq.artifact_id,
        "countable": True,
        "source_sha256": meta.sha256,
        "normalization_version": NORMALIZATION_VERSION,
        "lang": sq.lang,
        "locator": sq.locator,
        "quote_digest": expected_digest,
        "kind": "quote_in_source",
        "source_fetched": True,
    }
    if pb_aid:
        effect["attempt_id"] = str(pb_aid)
    return "met", None, evid, effect


def _aggregate_effects(effects: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-quote effects into one CheckResult.effect."""
    if len(effects) == 1:
        return effects[0]
    return {
        "producer": "tool",
        "trusted": False,
        "core_evaluated": True,
        "countable": all(bool(e.get("countable")) for e in effects),
        "kind": "quote_in_source",
        "source_fetched": True,
        "normalization_version": NORMALIZATION_VERSION,
        "quotes": effects,
        "source_sha256": [e.get("source_sha256") for e in effects],
        "quote_digest": [e.get("quote_digest") for e in effects],
        "artifact_id": [e.get("artifact_id") for e in effects],
    }


def evaluate_quote_in_source(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    role: CheckRole,
    attempt_id: str | None = None,
    fetched_digests: Collection[str] | None = None,
    prov_fields: dict[str, Any] | None = None,
) -> CheckResult:
    """All quotes must match CAS text from journaled SourceFetched sources.

    ``fetched_digests`` is study-scoped (see ``SourceFetchIndex``).
    """
    del prov_fields  # countable comes only from SourceFetched match
    quotes, miss = _load_quotes(check, out, artifacts, attempt_id=attempt_id)
    if quotes is None:
        return _no_effect(
            check_id=check_id,
            template_id=template_id,
            role=role,
            reason=miss or "quote_missing",
        )

    digests = frozenset(fetched_digests or ())
    source_cache: dict[str, _SourceCacheVal] = {}
    evidence: list[EvidenceRef] = []
    met_effects: list[dict[str, Any]] = []

    for sq in quotes:
        status, detail, evid, effect = _match_one(
            sq,
            artifacts,
            fetched_digests=digests,
            source_cache=source_cache,
        )
        if status == "no_effect":
            return _no_effect(
                check_id=check_id,
                template_id=template_id,
                role=role,
                reason=detail or "artifact_missing",
            )
        if status == "unassessed":
            return _unassessed(
                check_id=check_id,
                template_id=template_id,
                role=role,
                reason=detail or "normalization_version_mismatch",
            )
        if status != "met":
            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.FAILED,
                is_code=True,
                required=True,
                role=role,
                detail=detail,
                effect=None,
                used_sesoi=False,
            )
        assert evid is not None
        assert effect is not None
        evidence.append(evid)
        met_effects.append(effect)

    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.MET,
        is_code=True,
        required=True,
        role=role,
        evidence=evidence,
        detail=None,
        effect=_aggregate_effects(met_effects),
        used_sesoi=False,
    )
