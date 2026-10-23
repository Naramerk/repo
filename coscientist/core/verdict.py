"""Hypothesis verdict model (M1 PR8b / DESIGN_HYP_DELTA).

Pure functions: claim classification, counting filter, hypothesis ``verdict()``,
and report payload builders.

Deviations (see PR body / CHANGELOG):
- ``VerdictSnapshotsRecorded`` kept for bind; finalize recomputes report rows.
- StudyCreated is v6 (``supported_needed``; confirmations_needed remains PR8 ``k``).
- validity_failed / unknown_role per-experiment exclusion from PR8 kept.
- Real journal events use GraphNodeUpserted (claims), not ClaimRecorded.
- E-value aggregation deferred (``aggregate='evalue'`` rejected at StudyCreated).
- R17 runtime metadata still deferred (PR8).
- SESOI / predicts_null from catalog template only (not RequirementRef.params).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

from coscientist.core.digest import canonical_json
from coscientist.core.r17 import claim_counts_under_r17
from coscientist.core.stance import DEFAULT_TESTS_INTENT, TestsIntent, claim_stance

ClaimOutcome = Literal["consistent", "inconsistent", "indeterminate"]
ClaimValidity = Literal["valid", "invalid"]
ExclusionReason = Literal[
    "stub",
    "partial",
    "invalid",
    "indeterminate",
    "lit",
    "r17",
    "superseded",
    "unknown_role",
    "validity_failed",
    # No numeric effect (safe weakening vs main — never a counting refute).
    "foreign_artifact",
    "output_not_submitted",
    "artifact_missing",
    "pointer_empty",
]

NO_EFFECT_EXCLUSION_REASONS = frozenset(
    {
        "foreign_artifact",
        "output_not_submitted",
        "artifact_missing",
        "pointer_empty",
    }
)
PredictedDirection = Literal["positive", "negative", "two_sided"]


class _HypClaim(Protocol):
    accepted: bool
    provenance: str
    intent: TestsIntent
    criterion_status: str
    evidence_kind: str
    frozen: bool
    attempt_id: str
    artifact_id: str
    validity_ok: bool
    check_role: str

    @property
    def is_lit(self) -> bool: ...

    def counts(self) -> bool: ...


def status_to_outcome(criterion_status: str) -> ClaimOutcome | None:
    """Backward-compat mapping (SPEC §1 removed semantics)."""
    if criterion_status == "met":
        return "consistent"
    if criterion_status == "not_met":
        return "inconsistent"
    if criterion_status == "unassessed":
        return "indeterminate"
    return None


def outcome_to_status(outcome: ClaimOutcome | None, *, validity: ClaimValidity) -> str:
    if validity == "invalid" or outcome is None:
        return "unassessed"
    if outcome == "consistent":
        return "met"
    if outcome == "inconsistent":
        return "not_met"
    return "unassessed"


_MAX_PROVENANCE_DEPTH = 64


def _media_type_is_code(media_type: str | None) -> bool:
    mt = (media_type or "").lower()
    if any(
        tok in mt
        for tok in (
            "python",
            "javascript",
            "typescript",
            "x-sh",
            "x-shellscript",
            "x-script",
            "x-bytecode",
        )
    ):
        return True
    return mt.startswith("text/x-")


def artifact_is_code(meta: Any | None) -> bool:
    """Legacy meta heuristic (role / media_type). Prefer ``output_kinds``.

    Unknown → DATA. Never uses InputBinding slot names.
    """
    if meta is None:
        return False
    role = getattr(meta, "role", None)
    if role == "code":
        return True
    if role in ("data", "paper", "figure", "table", "log", "report"):
        return False
    return _media_type_is_code(getattr(meta, "media_type", None))


# Back-compat name used by older imports/tests.
def binding_is_code(
    input_name: str | None = None,
    *,
    code_output_names: Sequence[str] | None = None,
    meta: Any | None = None,
) -> bool:
    """Deprecated: use ``artifact_is_code(meta)``. Slot names are ignored."""
    del input_name, code_output_names
    return artifact_is_code(meta)


def output_kinds_from_catalog(
    *,
    output_names: Sequence[str] | None,
    catalog: Any | None,
) -> dict[str, str]:
    """name → code|data from catalog ``code_output_names`` (ignore planner role).

    Names listed in the catalog are code; everything else defaults to **data**.
    Planner ``OutputSpec.role`` / media_type are ignored for this decision.
    """
    code_names = {
        str(x)
        for x in (getattr(catalog, "code_output_names", None) or ())
        if x
    }
    out: dict[str, str] = {}
    for name in output_names or ():
        n = str(name or "")
        if not n:
            continue
        out[n] = "code" if n in code_names else "data"
    return out


def output_kinds_from_task_spec(
    spec: Any | None,
    catalog: Any | None = None,
) -> dict[str, str]:
    """Backward-compat wrapper: kinds from catalog names on plan outputs."""
    names: list[str] = []
    if spec is not None:
        for ospec in getattr(spec, "expected_outputs", None) or ():
            n = str(getattr(ospec, "name", "") or "")
            if n:
                names.append(n)
    return output_kinds_from_catalog(output_names=names, catalog=catalog)


def _kind_from_producer(
    artifact_id: str,
    producer: Mapping[str, Any] | None,
) -> Literal["code", "data"] | None:
    """Core ``output_kinds`` for this artifact if the producer recorded them."""
    if producer is None:
        return None
    kinds = producer.get("output_kinds")
    if not isinstance(kinds, Mapping) or not kinds:
        return None
    omap = producer.get("output_map")
    if not isinstance(omap, Mapping):
        return None
    for name, aid in omap.items():
        if aid and str(aid) == artifact_id and name in kinds:
            k = kinds[name]
            return "code" if k == "code" else "data"
    # Produced but no OutputSpec entry → DATA (never agent media_type).
    return "data"


def _walk_artifact_is_code(
    artifact_id: str,
    *,
    producers: Mapping[str, Mapping[str, Any]],
    artifact_lookup: Callable[[str], Any] | None,
    notes: list[str],
) -> bool:
    """Code vs data: ``ResultRecorded.output_kinds`` wins; else legacy meta."""
    kind = _kind_from_producer(artifact_id, producers.get(artifact_id))
    if kind is not None:
        return kind == "code"
    meta = _safe_get_meta(
        artifact_id, artifact_lookup=artifact_lookup, notes=notes
    )
    return artifact_is_code(meta)


def _binding_fields(b: Any) -> tuple[str, str, str | None, str | None]:
    """kind, input_name, artifact_id, binding digest (optional fallback)."""
    if isinstance(b, Mapping):
        return (
            str(b.get("kind", "artifact")),
            str(b.get("input") or ""),
            str(b.get("artifact_id") or "") or None,
            str(b.get("digest") or "") or None,
        )
    return (
        str(getattr(b, "kind", "artifact")),
        str(getattr(b, "input", "") or ""),
        str(getattr(b, "artifact_id", "") or "") or None,
        str(getattr(b, "digest", "") or "") or None,
    )


def _safe_get_meta(
    artifact_id: str,
    *,
    artifact_lookup: Callable[[str], Any] | None,
    notes: list[str],
) -> Any | None:
    if not artifact_id or artifact_lookup is None:
        return None
    try:
        return artifact_lookup(artifact_id)
    except Exception as exc:  # noqa: BLE001 — never crash StudyFinalized path
        notes.append(f"get_meta:{artifact_id}:{type(exc).__name__}")
        return None


def _content_digest(
    artifact_id: str,
    *,
    artifact_lookup: Callable[[str], Any] | None,
    fallback: str | None = None,
    notes: list[str] | None = None,
) -> str | None:
    note_buf = notes if notes is not None else []
    meta = _safe_get_meta(
        artifact_id, artifact_lookup=artifact_lookup, notes=note_buf
    )
    if meta is not None:
        d = getattr(meta, "sha256", None) or getattr(meta, "digest", None)
        if d:
            return str(d)
    return str(fallback) if fallback else None


def _producer_index(
    results: Mapping[str, Mapping[str, Any]] | None,
    *,
    notes: list[str] | None = None,
) -> dict[str, Mapping[str, Any]]:
    """artifact_id → ResultRecorded payload (first writer wins). Never raises."""
    idx: dict[str, Mapping[str, Any]] = {}
    note_buf = notes if notes is not None else []
    if not results:
        return idx
    try:
        seen_payloads: set[int] = set()
        for _key, payload in results.items():
            try:
                if not isinstance(payload, Mapping):
                    continue
                pid = id(payload)
                if pid in seen_payloads:
                    continue
                seen_payloads.add(pid)
                for aid in payload.get("artifacts") or ():
                    idx.setdefault(str(aid), payload)
                omap = payload.get("output_map")
                if isinstance(omap, Mapping):
                    for aid in omap.values():
                        if aid:
                            idx.setdefault(str(aid), payload)
            except Exception as exc:  # noqa: BLE001 — never crash StudyFinalized
                note_buf.append(f"producer_index:{type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        note_buf.append(f"producer_index:{type(exc).__name__}")
    return idx


def root_data_digests_from_bindings(
    bindings: Sequence[Any] | None,
    *,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    artifact_lookup: Callable[[str], Any] | None = None,
    code_output_names: Sequence[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Content digests of **root** data artifacts reachable from bindings.

    Walks InputBinding → producer ResultRecorded.inputs (depth-guarded DFS).
    Code vs data from core ``ResultRecorded.output_kinds`` (plan OutputSpec);
    agent ``media_type`` is ignored when kinds are present. Diamonds reuse a
    ``done`` set; only true cycles (``on_stack``) are noted. An artifact with
    no upstream producer, or whose upstream inputs are all code, is a root.
    Unknown kind → DATA.

    Returns ``(sorted_root_digests, error_notes)``. Never raises.
    """
    del code_output_names  # slot-name catalog lists are not used for this decision
    notes: list[str] = []
    producers = _producer_index(results, notes=notes)
    roots: set[str] = set()

    def _walk(artifact_id: str, fallback: str | None, depth: int,
              on_stack: set[str], done: set[str]) -> None:
        if not artifact_id:
            return
        if artifact_id in done:
            return
        if artifact_id in on_stack:
            notes.append(f"cycle:{artifact_id}")
            return
        if depth > _MAX_PROVENANCE_DEPTH:
            notes.append(f"depth:{artifact_id}")
            d = _content_digest(
                artifact_id,
                artifact_lookup=artifact_lookup,
                fallback=fallback,
                notes=notes,
            )
            if d:
                roots.add(d)
            done.add(artifact_id)
            return

        on_stack.add(artifact_id)
        try:
            if _walk_artifact_is_code(
                artifact_id,
                producers=producers,
                artifact_lookup=artifact_lookup,
                notes=notes,
            ):
                return

            producer = producers.get(artifact_id)
            if producer is None:
                d = _content_digest(
                    artifact_id,
                    artifact_lookup=artifact_lookup,
                    fallback=fallback,
                    notes=notes,
                )
                if d:
                    roots.add(d)
                return

            upstream = [
                b
                for b in (producer.get("inputs") or ())
                if _binding_fields(b)[0] == "artifact" and _binding_fields(b)[2]
            ]
            if not upstream:
                d = _content_digest(
                    artifact_id,
                    artifact_lookup=artifact_lookup,
                    fallback=fallback,
                    notes=notes,
                )
                if d:
                    roots.add(d)
                return

            data_children: list[tuple[str, str | None]] = []
            for b in upstream:
                _k, _n, child, cdig = _binding_fields(b)
                if not child:
                    continue
                if _walk_artifact_is_code(
                    child,
                    producers=producers,
                    artifact_lookup=artifact_lookup,
                    notes=notes,
                ):
                    continue
                data_children.append((child, cdig))

            if not data_children:
                d = _content_digest(
                    artifact_id,
                    artifact_lookup=artifact_lookup,
                    fallback=fallback,
                    notes=notes,
                )
                if d:
                    roots.add(d)
                return

            for child, cdig in data_children:
                _walk(child, cdig, depth + 1, on_stack, done)
        finally:
            on_stack.discard(artifact_id)
            done.add(artifact_id)

    on_stack: set[str] = set()
    done: set[str] = set()
    for b in bindings or ():
        kind, _iname, aid, bdig = _binding_fields(b)
        if kind != "artifact" or not aid:
            continue
        _walk(aid, bdig, 0, on_stack, done)

    return sorted(roots), notes


def data_digests_from_bindings(
    bindings: Sequence[Any] | None,
    *,
    artifact_lookup: Callable[[str], Any] | None = None,
    code_output_names: Sequence[str] | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Root data digests for independence (artifact meta; transitive walk)."""
    digs, _notes = root_data_digests_from_bindings(
        bindings,
        results=results,
        artifact_lookup=artifact_lookup,
        code_output_names=code_output_names,
    )
    return digs


# Back-compat alias used by older tests/imports.
def digests_from_bindings(
    bindings: Sequence[Any] | None,
    *,
    artifact_lookup: Callable[[str], Any] | None = None,
    code_output_names: Sequence[str] | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str | None, str | None]:
    """Deprecated shape: (folded_data, None). Prefer ``data_digests_from_bindings``."""
    data = data_digests_from_bindings(
        bindings,
        artifact_lookup=artifact_lookup,
        code_output_names=code_output_names,
        results=results,
    )
    if not data:
        return None, None
    if len(data) == 1:
        return data[0], None
    return hashlib.sha256(canonical_json(data)).hexdigest(), None


def structural_source_key(claim: Any) -> tuple[str, tuple[str, ...]] | None:
    """(target, sorted criterion templates) — diagnostic only; merge uses share-any."""
    target = str(getattr(claim, "target", "") or "")
    raw = getattr(claim, "criterion_template_ids", None) or ()
    templates = tuple(sorted({str(x) for x in raw if x}))
    if not target and not templates:
        return None
    return (target, templates)


def _claim_templates(claim: Any) -> frozenset[str]:
    raw = getattr(claim, "criterion_template_ids", None) or ()
    return frozenset(str(x) for x in raw if x)


def _claim_target(claim: Any) -> str:
    return str(getattr(claim, "target", "") or "")


def independence_key_for(*, experiment_id: str | None = None) -> str:
    """Base independence key: sha256(JCS({experiment_id})). Method is not in the key."""
    return hashlib.sha256(
        canonical_json({"experiment_id": experiment_id or ""})
    ).hexdigest()


def _claim_data_digests(claim: Any) -> frozenset[str]:
    raw = getattr(claim, "data_digests", None) or ()
    return frozenset(str(x) for x in raw if x)


def _union_find_parent(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union_find_merge(parent: dict[str, str], a: str, b: str) -> None:
    ra, rb = _union_find_parent(parent, a), _union_find_parent(parent, b)
    if ra != rb:
        parent[rb] = ra


def source_id_for_claim(claim: Any) -> str:
    """Fallback singleton key (experiment_id-derived)."""
    return str(
        getattr(claim, "independence_key", None)
        or independence_key_for(
            experiment_id=getattr(claim, "experiment_id", None)
            or getattr(claim, "attempt_id", "")
            or "x"
        )
    )


def distinct_by_key(claims: Sequence[Any]) -> list[Any]:
    """Keep first claim per independence source.

    Merge when:
    1. root-data digest sets overlap (transitive provenance), or
    2. same target, empty roots, and **any shared** criterion template id
       (adding an auxiliary template must not mint a new source).
    """
    items = list(claims)
    if not items:
        return []

    ids = [f"c{i}" for i in range(len(items))]
    parent = {i: i for i in ids}

    # (1) Overlap of root-data digests — one union per digest covers multi-digest
    # claims transitively (A∈d1∩d2 links owners of d1 with owners of d2).
    digest_owners: dict[str, list[str]] = {}
    for cid, c in zip(ids, items, strict=True):
        for d in _claim_data_digests(c):
            digest_owners.setdefault(d, []).append(cid)
    for owners in digest_owners.values():
        for other in owners[1:]:
            _union_find_merge(parent, owners[0], other)

    # (2) Structural: same target + any shared criterion template, no root data.
    tmpl_owners: dict[tuple[str, str], list[str]] = {}
    for cid, c in zip(ids, items, strict=True):
        if _claim_data_digests(c):
            continue
        target = _claim_target(c)
        tmpls = _claim_templates(c)
        if not target or not tmpls:
            continue
        for tid in tmpls:
            tmpl_owners.setdefault((target, tid), []).append(cid)
    for owners in tmpl_owners.values():
        for other in owners[1:]:
            _union_find_merge(parent, owners[0], other)

    # Singletons by independence_key when neither rule applied a group.
    key_owners: dict[str, list[str]] = {}
    for cid, c in zip(ids, items, strict=True):
        if _claim_data_digests(c):
            continue
        if _claim_target(c) and _claim_templates(c):
            continue
        key_owners.setdefault(source_id_for_claim(c), []).append(cid)
    for owners in key_owners.values():
        for other in owners[1:]:
            _union_find_merge(parent, owners[0], other)

    seen: set[str] = set()
    out: list[Any] = []
    for cid, c in zip(ids, items, strict=True):
        root = _union_find_parent(parent, cid)
        if root in seen:
            continue
        seen.add(root)
        out.append(c)
    return out


def effective_outcome(claim: Any) -> ClaimOutcome | None:
    """Read stored outcome, else derive from criterion_status / validity."""
    stored = getattr(claim, "claim_outcome", None) or getattr(claim, "outcome", None)
    if stored in ("consistent", "inconsistent", "indeterminate"):
        return stored  # type: ignore[return-value]
    if getattr(claim, "validity_ok", True) is False:
        return None
    return status_to_outcome(str(getattr(claim, "criterion_status", "unassessed")))


def effective_validity(claim: Any) -> ClaimValidity:
    if getattr(claim, "validity_ok", True) is False:
        return "invalid"
    stored = getattr(claim, "validity", None)
    if stored in ("valid", "invalid"):
        return stored  # type: ignore[return-value]
    return "valid"


def effect_has_interval(effect: Mapping[str, Any] | None) -> bool:
    """True when effect carries a real CI (ci_low < ci_high), not a point estimate."""
    if not isinstance(effect, Mapping):
        return False
    try:
        lo = float(effect["ci_low"])  # type: ignore[index]
        hi = float(effect["ci_high"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return False
    return lo < hi


def call_id_belongs_to_attempt(call_id: str | None, attempt_id: str | None) -> bool:
    if not call_id or not attempt_id:
        return False
    cid = str(call_id)
    return cid == attempt_id or cid.startswith(f"{attempt_id}:")


def recompute_effect_provenance(
    effect: Mapping[str, Any] | None,
    *,
    attempt_id: str | None = None,
    artifact_meta: Any | None = None,
) -> dict[str, Any] | None:
    """Recompute ``trusted`` / ``countable`` from provenance facts (not journal bits).

    Prefer live ``artifact_meta`` (``producer``, ``produced_by.call_id``) when the
    artifact store is available on replay. Otherwise use effect-stamped facts
    written at eval from that same meta (``producer``, ``produced_by_call_id``,
    ``core_evaluated``). Journal ``trusted``/``countable`` bits are never trusted.

    ``trusted`` = producer core, or sandbox with ``net_isolation != soft``.
    Soft sandbox evidence stays core_evaluated/countable but not trusted.
    ``countable`` = core-evaluated catalog check on an artifact bound to the attempt.
    """
    if not isinstance(effect, Mapping):
        return None
    out = dict(effect)
    producer = out.get("producer", "agent")
    call_id = str(out.get("produced_by_call_id") or "")
    net_isolation = out.get("net_isolation")
    if artifact_meta is not None:
        meta_prod = getattr(artifact_meta, "producer", None)
        if meta_prod in ("agent", "core", "sandbox"):
            producer = meta_prod
        produced = getattr(artifact_meta, "produced_by", None)
        meta_call = str(getattr(produced, "call_id", "") or "")
        if meta_call:
            call_id = meta_call
            out["produced_by_call_id"] = meta_call
        aid = getattr(artifact_meta, "artifact_id", None)
        if aid:
            out["artifact_id"] = str(aid)
        meta_ni = getattr(artifact_meta, "net_isolation", None)
        if meta_ni in ("hard", "soft", "none"):
            net_isolation = meta_ni
    if producer not in ("agent", "core", "sandbox"):
        producer = "agent"
    out["producer"] = producer
    if net_isolation in ("hard", "soft", "none"):
        out["net_isolation"] = net_isolation
    if producer == "core":
        out["trusted"] = True
    elif producer == "sandbox":
        out["trusted"] = net_isolation != "soft"
    else:
        out["trusted"] = False
    core_eval = bool(out.get("core_evaluated"))
    if attempt_id and call_id:
        out["countable"] = core_eval and call_id_belongs_to_attempt(
            call_id, attempt_id
        )
    else:
        # No binding facts (unit probe / legacy): core_evaluated is the fact.
        out["countable"] = core_eval
    return out


# Back-compat alias.
def recompute_effect_trusted(
    effect: Mapping[str, Any] | None,
    *,
    attempt_id: str | None = None,
) -> dict[str, Any] | None:
    return recompute_effect_provenance(effect, attempt_id=attempt_id)


def effect_producer_trusted(effect: Mapping[str, Any] | None) -> bool:
    """True when metric producer is core/sandbox (stronger tag; not M1 counting)."""
    if not isinstance(effect, Mapping):
        return False
    return effect.get("producer") in ("core", "sandbox")


def effect_is_countable(effect: Mapping[str, Any] | None) -> bool:
    """True when core catalog-evaluated evidence is countable for verdicts."""
    if not isinstance(effect, Mapping):
        return False
    return effect.get("countable") is True


def evidence_countable_for_verdict(claim: Any) -> bool:
    """Countable evidence required for both refuted and supported counting."""
    return effect_is_countable(getattr(claim, "effect", None))


def effect_is_trusted(effect: Mapping[str, Any] | None) -> bool:
    """Trusted producer **and** a real CI interval (reported; not M1 counting)."""
    return effect_producer_trusted(effect) and effect_has_interval(effect)


def tost_holds_numerically(
    *, sesoi: float | None, effect: Mapping[str, Any] | None
) -> bool:
    """CI strictly inside (−sesoi, +sesoi). Requires a real interval, not a point."""
    if sesoi is None or not effect_has_interval(effect):
        return False
    assert effect is not None
    try:
        lo = float(effect["ci_low"])  # type: ignore[index]
        hi = float(effect["ci_high"])  # type: ignore[index]
        s = float(sesoi)
    except (KeyError, TypeError, ValueError):
        return False
    return (-s) < lo and hi < s


def tost_holds(*, sesoi: float | None, effect: Mapping[str, Any] | None) -> bool:
    """Severe-grade TOST: numerical hold + countable core-evaluated evidence."""
    return effect_is_countable(effect) and tost_holds_numerically(
        sesoi=sesoi, effect=effect
    )


def effect_exceeds_sesoi(
    *,
    sesoi: float | None,
    effect: Mapping[str, Any] | None,
    direction: PredictedDirection | str | None = "two_sided",
) -> bool:
    """Non-null: countable point-estimate exceeds sesoi (not TOST)."""
    if sesoi is None or not effect_is_countable(effect):
        return False
    assert effect is not None
    try:
        est = float(effect["est"])  # type: ignore[index]
        s = float(sesoi)
    except (KeyError, TypeError, ValueError):
        return False
    d = direction or "two_sided"
    if d == "positive":
        return est > s
    if d == "negative":
        return est < -s
    return abs(est) > s


def claim_is_severe(claim: Any) -> bool:
    """Severe from catalog + check results — never trust stored ``severe=True``.

    Explicit ``severe=False`` remains definitive (V5 legacy never supported).
    Requires **countable** core-evaluated evidence (not sandbox ``trusted``).
    ``effect.trusted`` is a separate stronger tag recorded for reporting.
    """
    if effective_outcome(claim) != "consistent":
        return False
    if effective_validity(claim) != "valid":
        return False
    # Legacy / explicit non-severe stamp is definitive (V5).
    if getattr(claim, "severe", None) is False:
        return False
    if not bool(getattr(claim, "frozen", False)):
        return False
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    if frozen_at is not None and first_ev is not None and not (frozen_at < first_ev):
        return False
    # Never honor severe=True from the journal — recompute below.
    sesoi = getattr(claim, "sesoi", None)
    uses = getattr(claim, "uses_sesoi", None)
    if uses is False:
        return False
    if uses is not True and sesoi is None:
        return False
    if sesoi is None:
        return False
    used = getattr(claim, "used_sesoi", None)
    if used is not True:
        return False
    effect = getattr(claim, "effect", None)
    if not effect_is_countable(effect):
        return False
    predicts_null = bool(getattr(claim, "predicts_null", False))
    if predicts_null:
        return tost_holds(sesoi=float(sesoi), effect=effect)
    direction = getattr(claim, "predicted_direction", None) or "two_sided"
    return effect_exceeds_sesoi(
        sesoi=float(sesoi), effect=effect, direction=direction
    )


def refute_intent_counts(claim: Any) -> bool:
    """Refute intent counts toward supported only if the prediction could fail.

    Never trusts stored ``refute_capable=True``. Recomputes from catalog + check.
    Requires countable core-evaluated evidence (same rule as severe).
    """
    if _intent(claim) != "refute":
        return False
    # Explicit False (core) is definitive; True is ignored (legacy forgery).
    if getattr(claim, "refute_capable", None) is False:
        return False
    if not bool(getattr(claim, "frozen", False)):
        return False
    if getattr(claim, "check_role", "unknown") != "prediction":
        return False
    uses = getattr(claim, "uses_sesoi", None)
    if uses is not True:
        return False
    if getattr(claim, "sesoi", None) is None:
        return False
    if getattr(claim, "used_sesoi", None) is not True:
        return False
    effect = getattr(claim, "effect", None)
    if bool(getattr(claim, "predicts_null", False)):
        return effect_is_countable(effect) and effect_has_interval(effect)
    return effect_is_countable(effect)


def classify_outcome(
    *,
    validity_ok: bool,
    prediction_statuses: Sequence[str],
    predicts_null: bool = False,
    sesoi: float | None = None,
    effect: Mapping[str, Any] | None = None,
    r17_ok: bool = True,
) -> tuple[str, str | None, bool]:
    """Return (validity, outcome|None, severe_hint_inputs_ok).

    Non-countable failing metrics are indeterminate (never refutation).
    Countable evidence is required symmetrically for inconsistent and severe.
    """
    if not validity_ok or not r17_ok:
        return "invalid", None, False
    preds = [s for s in prediction_statuses if s]
    if any(s == "FAILED" or s == "not_met" for s in preds):
        # Foreign / non-core-evaluated fail → never a counting refute.
        if not effect_is_countable(effect):
            outcome: str | None = "indeterminate"
        else:
            outcome = "inconsistent"
    elif preds and all(s in ("PASSED", "MET", "met") for s in preds):
        outcome = "consistent"
    else:
        outcome = "indeterminate"
    if predicts_null and outcome == "consistent":
        # Consistency uses numerical TOST; severity separately requires countable.
        if not tost_holds_numerically(sesoi=sesoi, effect=effect):
            outcome = "indeterminate"
    return "valid", outcome, outcome == "consistent"


def exclusion_reason(claim: Any) -> ExclusionReason | None:
    """Why a claim is out of the counting set (SPEC §3.2 + PR8 roles)."""
    if claim.counts():
        return None
    stored = getattr(claim, "exclusion_reason", None)
    if stored in (
        "stub",
        "partial",
        "invalid",
        "indeterminate",
        "lit",
        "r17",
        "superseded",
        "unknown_role",
        "validity_failed",
        "foreign_artifact",
        "output_not_submitted",
        "artifact_missing",
        "pointer_empty",
    ):
        return stored  # type: ignore[return-value]
    if getattr(claim, "check_role", "unknown") == "unknown" and not getattr(
        claim, "validity_ok", True
    ):
        return "unknown_role"
    if effective_validity(claim) == "invalid":
        return "validity_failed"
    prov = getattr(claim, "provenance", "stub")
    if prov == "stub":
        return "stub"
    if prov == "partial":
        return "partial"
    if getattr(claim, "is_lit", False) or getattr(claim, "evidence_kind", "") == "literature":
        return "lit"
    oc = effective_outcome(claim)
    if oc == "indeterminate" or oc is None:
        return "indeterminate"
    return "invalid"


def _intent(claim: Any) -> TestsIntent:
    intent = getattr(claim, "intent", None) or DEFAULT_TESTS_INTENT
    if intent not in ("confirm", "refute"):
        return DEFAULT_TESTS_INTENT
    return intent  # type: ignore[return-value]


def claim_counts_for_verdict(claim: Any) -> bool:
    """Base ``counts()`` plus R17 when freeze/evidence metadata is present.

    Lives here (not ``rules``) so ``rules``↔``verdict`` has no import cycle.
    """
    fn = getattr(claim, "counts_under_r17", None)
    if callable(fn):
        return bool(fn())
    base = getattr(claim, "counts", None)
    if callable(base):
        if not base():
            return False
    else:
        return False
    try:
        stance = claim_stance(str(getattr(claim, "criterion_status", "unassessed")))
    except KeyError:
        stance = "inconclusive"
    if stance == "refutes":
        return True
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    confirming = getattr(claim, "confirming_evidence_ids", None)
    threshold_src = getattr(claim, "threshold_source_evidence_ids", None)
    if frozen_at is None and first_ev is None and confirming is None:
        return True
    return claim_counts_under_r17(
        criterion_frozen_at_seq=frozen_at,
        first_cited_evidence_seq=first_ev,
        confirming_evidence_ids=set(confirming or ()),
        threshold_source_evidence_ids=set(threshold_src or ()),
        stance=stance,
    )


def hypothesis_verdict(
    claims: Sequence[Any],
    any_experiment_done: bool = False,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = True,
    supported_needed: int = 2,
    confirmations_needed: int = 1,
) -> tuple[str, bool, str | None]:
    """Hypothesis/question verdict (SPEC §5).

    ``supported_needed`` (≥2) is the severe-support threshold. ``confirmations_needed``
    is retained for PR8 snapshot ``k`` compat and is not the support gate.
    Returns ``(verdict, by_literature, rule)``.
    """
    del confirmations_needed  # PR8 k — not used for supported gate
    del any_experiment_done
    about = list(claims)
    if not about:
        return "untested", False, "no_counted"

    counted = [c for c in about if claim_counts_for_verdict(c)]

    # Symmetric countability: non-countable inconsistent never counts toward k_ref.
    inconsistent = distinct_by_key(
        [
            c
            for c in counted
            if effective_outcome(c) == "inconsistent"
            and evidence_countable_for_verdict(c)
        ]
    )
    consistent_severe = distinct_by_key(
        [
            c
            for c in counted
            if effective_outcome(c) == "consistent" and claim_is_severe(c)
        ]
    )
    k_ref = max(1, int(refutations_needed))
    k_sup = max(2, int(supported_needed))
    n_inc = len(inconsistent)
    n_con = len(consistent_severe)

    if n_inc >= k_ref and refutation_priority:
        return "refuted", False, "k_ref"
    if n_inc >= k_ref and not refutation_priority:
        if consistent_severe:
            return "inconclusive", False, "k_ref"
        return "refuted", False, "k_ref"
    if n_inc > 0:
        return "inconclusive", False, "inc_below_k_ref"
    if n_con >= k_sup and any(refute_intent_counts(c) for c in consistent_severe):
        return "supported", False, "support_min"
    if n_con >= 1:
        return "survived", False, "single_severe"
    if counted:
        return "inconclusive", False, "no_counted"
    if any(effective_validity(c) == "invalid" for c in about):
        return "inconclusive", False, "validity_failed"
    # Only excluded (stub/partial/lit/…) claims — inconclusive, not untested.
    return "inconclusive", False, "no_counted"


def verdict(
    claims: Sequence[Any],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    supported_needed: int = 2,
) -> tuple[str, bool]:
    """Compat wrapper matching ``rules.verdict`` return shape.

    ``k`` is PR8 ``confirmations_needed`` (snapshot field); support gate uses
    ``supported_needed``.
    """
    v, by_lit, _rule = hypothesis_verdict(
        claims,
        any_experiment_done,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        supported_needed=supported_needed,
        confirmations_needed=k,
    )
    return v, by_lit


def excluded_confirmations_note(
    claims: Sequence[Any],
    *,
    verdict_value: str,
) -> str | None:
    """PR8 priority-refuted note: stub/partial supporting results excluded."""
    if verdict_value != "refuted":
        return None
    stub_partial = []
    for c in claims:
        if getattr(c, "provenance", "") not in ("stub", "partial"):
            continue
        oc = effective_outcome(c)
        try:
            stance = claim_stance(str(getattr(c, "criterion_status", "unassessed")))
        except KeyError:
            stance = "inconclusive"
        if oc == "consistent" or stance == "supports":
            stub_partial.append(c)
    if not stub_partial:
        return None
    keys = {
        getattr(c, "attempt_id", "") or getattr(c, "artifact_id", "") or id(c)
        for c in stub_partial
    }
    keys.discard("")
    n = len(keys) if keys else len(stub_partial)
    if n <= 0:
        return None
    return f"refuted; {n} supporting results were stub/partial"


def hypothesis_report_row(
    *,
    hypothesis_id: str,
    question_id: str,
    claims: Sequence[Any],
    verdict_value: str,
    rule: str | None,
    legacy_claims: int = 0,
) -> dict[str, Any]:
    """SPEC hypotheses[] item (also used as hypothesis_row)."""
    counted_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    supporting: list[str] = []
    refuting: list[str] = []
    aux: list[str] = []
    for c in claims:
        cid = getattr(c, "node_id", None) or getattr(c, "attempt_id", "") or "?"
        if claim_counts_for_verdict(c):
            oc = effective_outcome(c) or "indeterminate"
            counted_rows.append(
                {
                    "claim_id": cid,
                    "outcome": oc,
                    "intent": _intent(c),
                    "severe": claim_is_severe(c),
                    "independence_key": getattr(c, "independence_key", "") or "",
                }
            )
            if oc == "consistent":
                supporting.append(cid)
            elif oc == "inconsistent":
                refuting.append(cid)
            role = getattr(c, "check_role", None)
            if role == "validity" and getattr(c, "validity_ok", True):
                tid = getattr(c, "template_id", None) or "validity"
                if tid not in aux:
                    aux.append(str(tid))
        else:
            excluded_rows.append(
                {"claim_id": cid, "reason": exclusion_reason(c) or "invalid"}
            )
    conflict = None
    if verdict_value == "refuted" and supporting and refuting:
        conflict = {"supporting": supporting, "refuting": refuting}
    severity_note = None
    if verdict_value in ("survived", "supported"):
        severity_note = (
            "survived != proven; tests that could have refuted: "
            + ", ".join(
                r["claim_id"]
                for r in counted_rows
                if r.get("intent") == "refute" or r.get("severe")
            )
        )
    elif verdict_value == "refuted" and supporting:
        severity_note = (
            f"refuted despite {len(supporting)} supporting claims (conflict)"
        )
    if excluded_rows:
        reasons: dict[str, int] = {}
        for row in excluded_rows:
            r = str(row.get("reason") or "invalid")
            reasons[r] = reasons.get(r, 0) + 1
        excl_txt = (
            f"{sum(reasons.values())} claims excluded ("
            + ", ".join(f"{r} {n}" for r, n in sorted(reasons.items()))
            + ")"
        )
        severity_note = f"{severity_note}; {excl_txt}" if severity_note else excl_txt
    return {
        "hypothesis_id": hypothesis_id,
        "question_id": question_id,
        "verdict": verdict_value,
        "rule": rule or "no_counted",
        "counted": counted_rows,
        "excluded": excluded_rows,
        "conflict": conflict,
        "severity_note": severity_note,
        "auxiliary_assumptions": aux,
        "legacy_claims": legacy_claims,
    }


# Alias required by review wording.
hypothesis_row = hypothesis_report_row


def disposition_for_question(
    *,
    hypothesis_verdicts: Mapping[str, str],
    question_verdict: str | None,
    has_hypotheses: bool,
) -> str:
    """SPEC disposition mapping (with/without hypotheses)."""
    if not has_hypotheses:
        mapping = {
            "supported": "answered",
            "survived": "survived",
            "refuted": "refuted",
            "inconclusive": "inconclusive",
            "untested": "not_attempted",
        }
        return mapping.get(question_verdict or "untested", "inconclusive")
    vals = list(hypothesis_verdicts.values())
    if any(v == "supported" for v in vals):
        return "answered"
    if any(v == "survived" for v in vals) and not any(v == "supported" for v in vals):
        return "survived"
    if vals and all(v == "refuted" for v in vals):
        return "refuted"
    return "inconclusive"
