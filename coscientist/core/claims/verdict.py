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

from coscientist.core.claims.r17 import claim_counts_under_r17
from coscientist.core.claims.stance import TestsIntent, claim_stance
from coscientist.core.events.digest import canonical_json
from coscientist.core.events.errors import JournalFormatError, require_journal_field

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
    # M1 PR20: proposed_check not yet CheckApproved (or rejected / timed out).
    "unapproved_check",
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


def artifact_is_code(meta: Any | None) -> bool:
    """True when artifact meta.role is ``code`` (required; never inferred from media_type)."""
    if meta is None:
        return False
    role = getattr(meta, "role", None)
    if role == "code":
        return True
    if role in ("data", "paper", "figure", "table", "log", "report"):
        return False
    raise ValueError(f"artifact meta missing/unknown role: {role!r}")


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
    if catalog is None:
        code_names: set[str] = set()
    else:
        raw_names = getattr(catalog, "code_output_names", None)
        if raw_names is None:
            raise JournalFormatError(
                "catalog missing required code_output_names",
                field="code_output_names",
            )
        code_names = {str(x) for x in raw_names if x}
    out: dict[str, str] = {}
    for name in output_names or ():
        n = str(name)
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
        outputs = getattr(spec, "expected_outputs", None)
        if outputs is None:
            outputs = ()
        for ospec in outputs:
            n_raw = getattr(ospec, "name", None)
            if n_raw is None:
                raise JournalFormatError(
                    "OutputSpec missing required name",
                    field="name",
                )
            n = str(n_raw)
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
        kind = require_journal_field(b, "kind", ctx="binding")
        inp = require_journal_field(b, "input", ctx="binding")
        aid = b.get("artifact_id")
        dig = b.get("digest")
        return (
            str(kind),
            str(inp),
            str(aid) if aid is not None and str(aid) != "" else None,
            str(dig) if dig is not None and str(dig) != "" else None,
        )
    kind = getattr(b, "kind", None)
    if kind is None:
        raise JournalFormatError("binding missing required kind", field="kind")
    inp = getattr(b, "input", None)
    if inp is None:
        raise JournalFormatError("binding missing required input", field="input")
    aid = getattr(b, "artifact_id", None)
    dig = getattr(b, "digest", None)
    return (
        str(kind),
        str(inp),
        str(aid) if aid is not None and str(aid) != "" else None,
        str(dig) if dig is not None and str(dig) != "" else None,
    )


def _safe_get_meta(
    artifact_id: str,
    *,
    artifact_lookup: Callable[[str], Any] | None,
    notes: list[str],
) -> Any | None:
    """Resolve artifact meta; lookup errors propagate (callers catch specifically)."""
    del notes  # retained for API compat; soft-fail notes are caller-owned
    if not artifact_id or artifact_lookup is None:
        return None
    return artifact_lookup(artifact_id)


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
        if hasattr(meta, "sha256"):
            d = meta.sha256
            if d:
                return str(d)
        elif hasattr(meta, "digest"):
            # Typed digest-only meta (no sha256 field) — refuse silent aliasing.
            raise JournalFormatError(
                "artifact meta missing sha256 (digest-only not accepted)",
                field="sha256",
            )
        else:
            raise JournalFormatError(
                "artifact meta missing required sha256",
                field="sha256",
            )
    return str(fallback) if fallback else None


def _producer_index(
    results: Mapping[str, Mapping[str, Any]] | None,
    *,
    notes: list[str] | None = None,
) -> dict[str, Mapping[str, Any]]:
    """artifact_id → ResultRecorded payload (first writer wins). Fail-closed.

    Lookup / shape errors propagate (no broad except swallow). ``notes`` is
    retained for API compat with ``root_data_digests_from_bindings`` walk notes.
    """
    del notes  # walk notes are recorded by the caller; index itself does not soft-fail
    idx: dict[str, Mapping[str, Any]] = {}
    if not results:
        return idx
    seen_payloads: set[int] = set()
    for key, payload in results.items():
        if not isinstance(payload, Mapping):
            raise JournalFormatError(
                f"result payload for {key!r} is not a mapping",
                field="results",
            )
        pid = id(payload)
        if pid in seen_payloads:
            continue
        seen_payloads.add(pid)
        if "artifacts" not in payload:
            raise JournalFormatError(
                f"result {key!r} missing required artifacts",
                field="artifacts",
            )
        arts = payload["artifacts"]
        if arts is None:
            arts = ()
        if not isinstance(arts, (list, tuple)):
            raise JournalFormatError(
                f"result {key!r} artifacts must be a sequence",
                field="artifacts",
            )
        for aid in arts:
            idx.setdefault(str(aid), payload)
        omap = payload.get("output_map")
        if omap is not None and not isinstance(omap, Mapping):
            raise JournalFormatError(
                f"result {key!r} output_map must be a mapping",
                field="output_map",
            )
        if isinstance(omap, Mapping):
            for aid in omap.values():
                if aid:
                    idx.setdefault(str(aid), payload)
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

    Returns ``(sorted_root_digests, error_notes)``. Producer-index lookup
    errors propagate (fail-closed); walk notes cover cycles/depth only.
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
    raw = getattr(claim, "criterion_template_ids", None) or ()
    templates = tuple(sorted({str(x) for x in raw if x}))
    if isinstance(claim, Mapping):
        has_target = "target" in claim
        target_raw = claim.get("target") if has_target else None
    else:
        has_target = hasattr(claim, "target")
        target_raw = claim.target if has_target else None
    if not has_target and not templates:
        return None
    if not has_target or target_raw is None or str(target_raw) == "":
        if not templates:
            return None
        raise JournalFormatError("claim missing required target", field="target")
    return (str(target_raw), templates)


def _claim_templates(claim: Any) -> frozenset[str]:
    raw = getattr(claim, "criterion_template_ids", None) or ()
    return frozenset(str(x) for x in raw if x)


def _claim_target_optional(claim: Any) -> str | None:
    """Target for structural independence merge — absent means skip that rule."""
    if isinstance(claim, Mapping):
        if "target" not in claim:
            return None
        t = claim.get("target")
    else:
        if not hasattr(claim, "target"):
            return None
        t = claim.target
    if t is None or str(t) == "":
        return None
    return str(t)


def independence_key_for(*, experiment_id: str) -> str:
    """Base independence key: sha256(JCS({experiment_id})). Method is not in the key."""
    if experiment_id is None or str(experiment_id) == "":
        raise JournalFormatError(
            "independence_key_for requires experiment_id",
            field="experiment_id",
        )
    return hashlib.sha256(
        canonical_json({"experiment_id": str(experiment_id)})
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
    """Independence source key from stamped claim fields (no invent)."""
    keyed = getattr(claim, "independence_key", None)
    if keyed is not None and str(keyed) != "":
        return str(keyed)
    if isinstance(claim, Mapping):
        exp = claim.get("experiment_id")
    elif hasattr(claim, "experiment_id"):
        exp = claim.experiment_id
    else:
        exp = None
    if exp is None or str(exp) == "":
        raise JournalFormatError(
            "claim missing independence_key/experiment_id",
            field="experiment_id",
        )
    return str(independence_key_for(experiment_id=str(exp)))


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
        target = _claim_target_optional(c)
        tmpls = _claim_templates(c)
        if target is None or not tmpls:
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
        if _claim_target_optional(c) is not None and _claim_templates(c):
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


def _claim_validity_ok(claim: Any) -> bool:
    """Require ``validity_ok`` on the claim (never default True)."""
    if isinstance(claim, Mapping):
        return bool(require_journal_field(claim, "validity_ok", ctx="claim"))
    if not hasattr(claim, "validity_ok"):
        raise JournalFormatError("claim missing required validity_ok", field="validity_ok")
    return bool(claim.validity_ok)


def effective_outcome(claim: Any) -> ClaimOutcome | None:
    """Read stored claim_outcome, else derive from criterion_status / validity."""
    if isinstance(claim, Mapping):
        if "claim_outcome" in claim:
            stored = claim["claim_outcome"]
        else:
            stored = None
    elif hasattr(claim, "claim_outcome"):
        stored = claim.claim_outcome
    else:
        stored = None
    if stored in ("consistent", "inconsistent", "indeterminate"):
        return stored  # type: ignore[return-value]
    if stored is not None and stored != "":
        raise JournalFormatError(
            f"invalid claim_outcome: {stored!r}",
            field="claim_outcome",
        )
    if _claim_validity_ok(claim) is False:
        return None
    return status_to_outcome(str(claim.criterion_status))


def effective_validity(claim: Any) -> ClaimValidity:
    if _claim_validity_ok(claim) is False:
        return "invalid"
    if isinstance(claim, Mapping):
        if "validity" not in claim:
            raise JournalFormatError(
                "claim missing required validity after validity_ok",
                field="validity",
            )
        stored = claim["validity"]
    elif hasattr(claim, "validity"):
        stored = claim.validity
    else:
        raise JournalFormatError(
            "claim missing required validity after validity_ok",
            field="validity",
        )
    if stored in ("valid", "invalid"):
        return stored  # type: ignore[return-value]
    if stored is None or stored == "":
        raise JournalFormatError(
            "claim missing required validity after validity_ok",
            field="validity",
        )
    raise JournalFormatError(
        f"invalid claim validity: {stored!r}",
        field="validity",
    )


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


def _stamped_attempt_id(obj: Any | None) -> str | None:
    """Return non-empty structural ``attempt_id`` from a mapping or attribute object."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        raw = obj.get("attempt_id")
    else:
        raw = getattr(obj, "attempt_id", None)
    if raw is None or str(raw) == "":
        return None
    return str(raw)


def call_id_belongs_to_attempt(
    call_or_produced: str | Mapping[str, Any] | Any | None,
    attempt_id: str | None,
) -> bool:
    """True when structured ``attempt_id`` matches (never call_id prefix parse).

    Preferred: mapping/object with field ``attempt_id`` — compare equality.
    Fallback when only a call_id string is available: require exact equality
    with the parallel ``attempt_id`` argument (``{attempt}:{suffix}`` is not
    authority; keep call_id for logging only).
    """
    if attempt_id is None or str(attempt_id) == "":
        return False
    aid = str(attempt_id)
    if call_or_produced is None:
        return False
    if not isinstance(call_or_produced, str):
        stamped = _stamped_attempt_id(call_or_produced)
        if stamped is not None:
            return stamped == aid
        # Object/mapping without attempt_id: equality on call_id if present.
        if isinstance(call_or_produced, Mapping):
            cid = call_or_produced.get("produced_by_call_id") or call_or_produced.get(
                "call_id"
            )
        else:
            cid = getattr(call_or_produced, "call_id", None)
        return cid is not None and str(cid) != "" and str(cid) == aid
    cid = str(call_or_produced)
    return bool(cid) and cid == aid


def recompute_effect_provenance(
    effect: Mapping[str, Any] | None,
    *,
    attempt_id: str | None = None,
    artifact_meta: Any | None = None,
) -> dict[str, Any] | None:
    """Recompute ``trusted`` / ``countable`` from provenance facts (not journal bits).

    Prefer live ``artifact_meta`` for ``producer`` / ``produced_by.call_id`` /
    ``produced_by.attempt_id`` / ``artifact_id`` when the store is available.
    Isolation (net/fs) for replay comes from the core journal effect only —
    never from artifact meta (B2).

    ``trusted`` = producer core, or sandbox with both isolations hard.
    ``countable`` requires core_evaluated and structural attempt binding
    (``effect.attempt_id`` / ``produced_by.attempt_id``), not call_id prefixes.
    ``observation_only`` (effect-stamped, e.g. tests_pass) ⇒ never countable.
    ``core_evaluated`` always from the effect — never from artifact meta.
    """
    if not isinstance(effect, Mapping):
        return None
    out = dict(effect)
    producer = out.get("producer")
    raw_call = out.get("produced_by_call_id")
    if raw_call is None:
        call_id: str | None = None
    elif str(raw_call) == "":
        raise JournalFormatError(
            "effect produced_by_call_id empty",
            field="produced_by_call_id",
        )
    else:
        call_id = str(raw_call)
    produced_by: Any | None = None
    # Isolation + core_evaluated: journal effect only (never artifact meta).
    net_isolation = out.get("net_isolation")
    fs_isolation = out.get("fs_isolation")
    observation_only = bool(out.get("observation_only"))
    if artifact_meta is not None:
        meta_prod = getattr(artifact_meta, "producer", None)
        if meta_prod is not None:
            producer = meta_prod
        produced_by = getattr(artifact_meta, "produced_by", None)
        if produced_by is not None:
            try:
                meta_call = produced_by.call_id
            except AttributeError as e:
                raise JournalFormatError(
                    "artifact produced_by missing call_id",
                    field="call_id",
                ) from e
            if meta_call is None or str(meta_call) == "":
                raise JournalFormatError(
                    "artifact produced_by missing call_id",
                    field="call_id",
                )
            call_id = str(meta_call)
            out["produced_by_call_id"] = call_id
            pb_aid = _stamped_attempt_id(produced_by)
            if pb_aid is not None:
                out["attempt_id"] = pb_aid
        aid = getattr(artifact_meta, "artifact_id", None)
        if aid is not None and str(aid) != "":
            out["artifact_id"] = str(aid)
        elif aid is not None and str(aid) == "":
            raise JournalFormatError(
                "artifact_meta artifact_id empty",
                field="artifact_id",
            )
    if producer not in ("agent", "core", "sandbox"):
        raise JournalFormatError(
            f"effect missing/unknown producer: {producer!r}",
            field="producer",
        )
    out["producer"] = producer
    if net_isolation in ("hard", "soft", "none"):
        out["net_isolation"] = net_isolation
    if fs_isolation in ("hard", "soft", "none"):
        out["fs_isolation"] = fs_isolation
    if observation_only:
        out["observation_only"] = True
    if out.get("timeout_kind"):
        out["timeout_kind"] = str(out["timeout_kind"])
        observation_only = True
        out["observation_only"] = True
    if out.get("refute_unresolved"):
        out["refute_unresolved"] = True
    if producer == "core":
        out["trusted"] = out.get("trusted") is not False
    elif producer == "sandbox":
        out["trusted"] = net_isolation == "hard" and fs_isolation == "hard"
    else:
        out["trusted"] = False
    core_eval = bool(out.get("core_evaluated"))
    # Countable requires attempt binding when attempt_id is known.
    # Structural effect/produced_by.attempt_id is authority; call_id is logging.
    # Without attempt context (unit derive), honor stamped countable ∧ core_eval.
    # Observation-only / guest timeout never count toward k_ref/k_sup.
    if observation_only or out.get("timeout_kind"):
        out["countable"] = False
    elif attempt_id:
        belongs = False
        eff_aid = _stamped_attempt_id(out)
        pb_aid = _stamped_attempt_id(produced_by)
        if eff_aid is not None:
            belongs = eff_aid == str(attempt_id)
        elif pb_aid is not None:
            belongs = pb_aid == str(attempt_id)
        elif call_id:
            belongs = call_id_belongs_to_attempt(call_id, attempt_id)
        out["countable"] = bool(core_eval and call_id and belongs)
    else:
        out["countable"] = bool(out.get("countable")) and core_eval
    return out


# Back-compat alias.
def recompute_effect_trusted(
    effect: Mapping[str, Any] | None,
    *,
    attempt_id: str | None = None,
) -> dict[str, Any] | None:
    return recompute_effect_provenance(effect, attempt_id=attempt_id)


def effect_producer_trusted(effect: Mapping[str, Any] | None) -> bool:
    """True when producer is core, or sandbox with both isolations hard (MJ3)."""
    if not isinstance(effect, Mapping):
        return False
    producer = effect.get("producer")
    if producer == "core":
        return effect.get("trusted") is not False
    if producer == "sandbox":
        return (
            effect.get("net_isolation") == "hard"
            and effect.get("fs_isolation") == "hard"
        )
    return False


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
    direction: PredictedDirection | str,
) -> bool:
    """Non-null: countable point-estimate exceeds sesoi (not TOST)."""
    if sesoi is None or not effect_is_countable(effect):
        return False
    assert effect is not None
    try:
        est = float(effect["est"])  # type: ignore[index]
        s = float(sesoi)
    except (KeyError, TypeError, ValueError) as e:
        raise JournalFormatError(
            f"invalid sesoi/effect.est for effect_exceeds_sesoi: {e}",
            field="sesoi",
        ) from e
    d = str(direction)
    if d == "positive":
        return est > s
    if d == "negative":
        return est < -s
    if d == "two_sided":
        return abs(est) > s
    raise JournalFormatError(
        f"invalid predicted_direction: {d!r}",
        field="predicted_direction",
    )


def claim_is_severe(claim: Any) -> bool:
    """Severe from catalog + check results — never trust stored ``severe=True``.

    Explicit ``severe=False`` remains definitive (V5). Absent ``severe`` is
    **not** treated as False — recompute from evidence. Journal mappings should
    only seed False when the field is present and explicitly False.

    Requires **countable** core-evaluated evidence (not sandbox ``trusted``).
    ``effect.trusted`` is a separate stronger tag recorded for reporting.

    PR20: proposed qualitative rubrics are never severe; proposed executable
    checks are severe only when ``effect.trusted`` (sandbox hard isolation).
    """
    if effective_outcome(claim) != "consistent":
        return False
    if effective_validity(claim) != "valid":
        return False
    # Explicit False only — missing attribute/key must not short-circuit.
    if isinstance(claim, Mapping):
        if "severe" in claim and claim["severe"] is False:
            return False
    elif hasattr(claim, "severe") and claim.severe is False:
        return False
    effect = getattr(claim, "effect", None)
    if isinstance(effect, dict):
        if effect.get("never_severe") or effect.get("qualitative"):
            return False
        if effect.get("proposed_check") and not effect.get("trusted"):
            return False
        if effect.get("severe_eligible") is False:
            return False
    if isinstance(claim, Mapping):
        frozen = require_journal_field(claim, "frozen", ctx="claim")
        uses = require_journal_field(claim, "uses_sesoi", ctx="claim")
        predicts_null = bool(require_journal_field(claim, "predicts_null", ctx="claim"))
        direction = str(
            require_journal_field(claim, "predicted_direction", ctx="claim")
        )
    else:
        if not hasattr(claim, "frozen"):
            raise JournalFormatError("claim missing required frozen", field="frozen")
        frozen = claim.frozen
        if not hasattr(claim, "uses_sesoi"):
            raise JournalFormatError(
                "claim missing required uses_sesoi", field="uses_sesoi"
            )
        uses = claim.uses_sesoi
        if not hasattr(claim, "predicts_null"):
            raise JournalFormatError(
                "claim missing required predicts_null", field="predicts_null"
            )
        predicts_null = bool(claim.predicts_null)
        if not hasattr(claim, "predicted_direction"):
            raise JournalFormatError(
                "claim missing required predicted_direction",
                field="predicted_direction",
            )
        direction = str(claim.predicted_direction)
    if not bool(frozen):
        return False
    frozen_at = getattr(claim, "criterion_frozen_at_seq", None)
    first_ev = getattr(claim, "first_cited_evidence_seq", None)
    if frozen_at is not None and first_ev is not None and not (frozen_at < first_ev):
        return False
    # Never honor severe=True from the journal — recompute below.
    if uses is False:
        return False
    if isinstance(claim, Mapping):
        sesoi = require_journal_field(claim, "sesoi", ctx="claim")
        used = require_journal_field(claim, "used_sesoi", ctx="claim")
    else:
        if not hasattr(claim, "sesoi"):
            raise JournalFormatError("claim missing required sesoi", field="sesoi")
        if not hasattr(claim, "used_sesoi"):
            raise JournalFormatError(
                "claim missing required used_sesoi", field="used_sesoi"
            )
        sesoi = claim.sesoi
        used = claim.used_sesoi
    if uses is not True and sesoi is None:
        return False
    if sesoi is None:
        return False
    if used is not True:
        return False
    if not effect_is_countable(effect):
        return False
    try:
        sesoi_f = float(sesoi)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise JournalFormatError(
            f"invalid sesoi: {sesoi!r}",
            field="sesoi",
        ) from e
    if predicts_null:
        return tost_holds(sesoi=sesoi_f, effect=effect)
    return effect_exceeds_sesoi(
        sesoi=sesoi_f, effect=effect, direction=direction
    )


def refute_intent_counts(claim: Any) -> bool:
    """Refute intent counts toward supported only if the prediction could fail.

    Never trusts stored ``refute_capable=True``. Recomputes from catalog + check.
    Explicit ``refute_capable=False`` is definitive; absent is recomputed.
    Requires countable core-evaluated evidence (same rule as severe).
    """
    if _intent(claim) != "refute":
        return False
    # Explicit False only — no getattr(..., None) absent⇒False invent.
    if isinstance(claim, Mapping):
        if "refute_capable" in claim and claim["refute_capable"] is False:
            return False
    elif hasattr(claim, "refute_capable") and claim.refute_capable is False:
        return False
    if isinstance(claim, Mapping):
        frozen = require_journal_field(claim, "frozen", ctx="claim")
        uses = require_journal_field(claim, "uses_sesoi", ctx="claim")
        predicts_null = bool(require_journal_field(claim, "predicts_null", ctx="claim"))
    else:
        if not hasattr(claim, "frozen"):
            raise JournalFormatError("claim missing required frozen", field="frozen")
        frozen = claim.frozen
        if not hasattr(claim, "uses_sesoi"):
            raise JournalFormatError(
                "claim missing required uses_sesoi", field="uses_sesoi"
            )
        uses = claim.uses_sesoi
        if not hasattr(claim, "predicts_null"):
            raise JournalFormatError(
                "claim missing required predicts_null", field="predicts_null"
            )
        predicts_null = bool(claim.predicts_null)
    if not bool(frozen):
        return False
    if claim.check_role != "prediction":
        return False
    if uses is not True:
        return False
    if isinstance(claim, Mapping):
        sesoi = require_journal_field(claim, "sesoi", ctx="claim")
        used_sesoi = require_journal_field(claim, "used_sesoi", ctx="claim")
    else:
        if not hasattr(claim, "sesoi"):
            raise JournalFormatError("claim missing required sesoi", field="sesoi")
        if not hasattr(claim, "used_sesoi"):
            raise JournalFormatError(
                "claim missing required used_sesoi", field="used_sesoi"
            )
        sesoi = claim.sesoi
        used_sesoi = claim.used_sesoi
    if sesoi is None:
        return False
    if used_sesoi is not True:
        return False
    effect = getattr(claim, "effect", None)
    if predicts_null:
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
        "unapproved_check",
    ):
        return stored  # type: ignore[return-value]
    if claim.check_role == "unknown" and not _claim_validity_ok(claim):
        return "unknown_role"
    if effective_validity(claim) == "invalid":
        return "validity_failed"
    prov = claim.provenance
    if prov == "stub":
        return "stub"
    if prov == "partial":
        return "partial"
    if isinstance(claim, Mapping):
        ek = require_journal_field(claim, "evidence_kind", ctx="claim")
        is_lit = require_journal_field(claim, "is_lit", ctx="claim")
    else:
        if not hasattr(claim, "evidence_kind"):
            raise JournalFormatError(
                "claim missing required evidence_kind", field="evidence_kind"
            )
        if not hasattr(claim, "is_lit"):
            raise JournalFormatError("claim missing required is_lit", field="is_lit")
        ek = claim.evidence_kind
        is_lit = claim.is_lit
    if is_lit is True or ek == "literature":
        return "lit"
    oc = effective_outcome(claim)
    if oc == "indeterminate" or oc is None:
        return "indeterminate"
    raise JournalFormatError(
        f"unclassified exclusion for claim outcome={oc!r}",
        field="exclusion_reason",
    )


def _intent(claim: Any) -> TestsIntent:
    if isinstance(claim, Mapping):
        intent = require_journal_field(claim, "intent", ctx="claim")
    else:
        if not hasattr(claim, "intent") or claim.intent is None:
            raise JournalFormatError(
                "claim missing required intent",
                field="intent",
            )
        intent = claim.intent
    if intent not in ("confirm", "refute"):
        raise JournalFormatError(
            f"invalid claim intent: {intent!r}",
            field="intent",
        )
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
    stance = claim_stance(str(claim.criterion_status))
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
    refutations_needed: int,
    refutation_priority: bool,
    supported_needed: int,
    confirmations_needed: int,
) -> tuple[str, bool, bool, str | None]:
    """Hypothesis/question verdict (SPEC §5).

    ``supported_needed`` (≥2) is the severe-support threshold. ``confirmations_needed``
    is retained for PR8 snapshot ``k`` compat and is not the support gate.
    Caps are required kwargs (pass from ``study.*`` — no silent defaults).
    Returns ``(verdict, by_literature, by_theory, rule)``.

    PR15 derivation: review-only derivation never feeds k_sup/k_ref; any
    derivation claim sets ``by_theory`` (marked «теоретически»). When only
    review-only derivation is present → ``inconclusive``.
    """
    from coscientist.core.claims.derivation import (
        apply_derivation_ceiling,
        by_theory_mark,
        derivation_caps_inconclusive,
    )

    del confirmations_needed  # PR8 k — not used for supported gate
    del any_experiment_done
    about = list(claims)
    if not about:
        return "untested", False, False, "no_counted"

    counted = [c for c in about if claim_counts_for_verdict(c)]
    theory = by_theory_mark(list(about))
    if not counted and derivation_caps_inconclusive(list(about)):
        return "inconclusive", False, True, "derivation_review_only"

    def _is_delegated_proposed(c: Any) -> bool:
        eff = getattr(c, "effect", None)
        return bool(isinstance(eff, dict) and eff.get("proposed_check") and eff.get("delegated"))

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
    # PR20: delegated proposed_check counts, but must not tip a catalog tie.
    catalog_inc = [c for c in inconsistent if not _is_delegated_proposed(c)]
    catalog_con = [c for c in consistent_severe if not _is_delegated_proposed(c)]
    k_ref = int(refutations_needed)
    k_sup = int(supported_needed)
    n_inc = len(inconsistent)
    n_con = len(consistent_severe)
    n_inc_cat = len(catalog_inc)
    n_con_cat = len(catalog_con)

    def _timeout_kind(c: Any) -> str | None:
        tk = getattr(c, "timeout_kind", None)
        if tk:
            return str(tk)
        eff = getattr(c, "effect", None)
        if isinstance(eff, dict) and eff.get("timeout_kind"):
            return str(eff["timeout_kind"])
        return None

    def _refute_unresolved(c: Any) -> bool:
        """True when claim/effect stamps unresolved. False = not stamped (filter)."""
        if isinstance(c, Mapping):
            if c.get("refute_unresolved"):
                return True
            eff = c.get("effect")
            return bool(isinstance(eff, dict) and eff.get("refute_unresolved"))
        # RuntimeClaim.refute_unresolved is a typed field (default False).
        try:
            if bool(c.refute_unresolved):
                return True
        except AttributeError:
            pass
        try:
            eff = c.effect
        except AttributeError:
            return False
        return bool(isinstance(eff, dict) and eff.get("refute_unresolved"))

    unresolved = [c for c in about if _refute_unresolved(c)]
    idle_unresolved = any(_timeout_kind(c) == "idle_timeout" for c in unresolved)

    def _ret(v: str, rule: str | None) -> tuple[str, bool, bool, str | None]:
        capped = apply_derivation_ceiling(v, list(about))
        if capped != v:
            return capped, False, theory, "derivation_review_only"
        return capped, False, theory, rule

    # Prefer catalog-only counts when delegated proposals would tip a catalog tie
    # (including the empty 0:0 catalog tie — DESIGN_M1 §16).
    use_inc = n_inc
    use_con = n_con
    use_severe = consistent_severe
    catalog_tied = n_inc_cat == n_con_cat
    delegated_changes = n_inc != n_inc_cat or n_con != n_con_cat
    if catalog_tied and delegated_changes:
        use_inc = n_inc_cat
        use_con = n_con_cat
        use_severe = catalog_con
    elif n_inc >= k_ref and n_inc_cat < k_ref and catalog_tied:
        use_inc = n_inc_cat
        use_con = n_con_cat
        use_severe = catalog_con
    elif n_con >= k_sup and n_con_cat < k_sup and catalog_tied:
        use_inc = n_inc_cat
        use_con = n_con_cat
        use_severe = catalog_con

    if use_inc >= k_ref and refutation_priority:
        return _ret("refuted", "k_ref")
    if use_inc >= k_ref and not refutation_priority:
        if use_severe:
            return _ret("inconclusive", "k_ref")
        return _ret("refuted", "k_ref")
    if use_inc > 0:
        return _ret("inconclusive", "inc_below_k_ref")
    # Guest timeout on a refute-role check hard-blocks supported (never k_sup).
    if unresolved and idle_unresolved:
        return _ret("untestable_by_agent", "idle_timeout")
    if use_con >= k_sup and any(refute_intent_counts(c) for c in use_severe):
        if unresolved:
            return _ret("inconclusive", "refute_unresolved")
        return _ret("supported", "support_min")
    if use_con >= 1:
        if unresolved and idle_unresolved:
            return _ret("untestable_by_agent", "idle_timeout")
        return _ret("survived", "single_severe")
    if unresolved and idle_unresolved:
        return _ret("untestable_by_agent", "idle_timeout")
    if unresolved:
        return _ret("inconclusive", "refute_unresolved")
    if counted:
        return _ret("inconclusive", "no_counted")
    if any(effective_validity(c) == "invalid" for c in about):
        return _ret("inconclusive", "validity_failed")
    # Only excluded (stub/partial/lit/…) claims — inconclusive, not untested.
    return _ret("inconclusive", "no_counted")


def verdict(
    claims: Sequence[Any],
    any_experiment_done: bool,
    k: int,
    *,
    refutations_needed: int,
    refutation_priority: bool,
    supported_needed: int,
) -> tuple[str, bool]:
    """Compat wrapper matching ``rules.verdict`` return shape.

    ``k`` is PR8 ``confirmations_needed`` (snapshot field); support gate uses
    ``supported_needed``. Caps are required (pass from ``study.*``).
    ``by_theory`` is available via ``hypothesis_verdict``.
    """
    v, by_lit, _by_theory, _rule = hypothesis_verdict(
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
        if isinstance(c, Mapping):
            prov = str(require_journal_field(c, "provenance", ctx="claim"))
        else:
            if not hasattr(c, "provenance"):
                raise JournalFormatError(
                    "claim missing required provenance", field="provenance"
                )
            prov = str(c.provenance)
        if prov not in ("stub", "partial"):
            continue
        oc = effective_outcome(c)
        stance = claim_stance(str(c.criterion_status))
        if oc == "consistent" or stance == "supports":
            stub_partial.append(c)
    if not stub_partial:
        return None
    keys: set[Any] = set()
    for c in stub_partial:
        aid = getattr(c, "attempt_id", None)
        arid = getattr(c, "artifact_id", None)
        if aid is not None and str(aid) != "":
            keys.add(str(aid))
        elif arid is not None and str(arid) != "":
            keys.add(str(arid))
        else:
            keys.add(id(c))
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
        cid_raw = getattr(c, "node_id", None)
        if cid_raw is None:
            cid_raw = getattr(c, "attempt_id", None)
        if cid_raw is None or str(cid_raw) == "":
            raise JournalFormatError(
                "claim missing required node_id/attempt_id", field="claim_id"
            )
        cid = str(cid_raw)
        if claim_counts_for_verdict(c):
            oc = effective_outcome(c)
            if oc is None:
                raise JournalFormatError(
                    f"claim {cid!r} missing countable outcome",
                    field="claim_outcome",
                )
            ik = getattr(c, "independence_key", None)
            if ik is None:
                raise JournalFormatError(
                    f"claim {cid!r} missing required independence_key",
                    field="independence_key",
                )
            counted_rows.append(
                {
                    "claim_id": cid,
                    "outcome": oc,
                    "intent": _intent(c),
                    "severe": claim_is_severe(c),
                    "independence_key": str(ik),
                }
            )
            if oc == "consistent":
                supporting.append(cid)
            elif oc == "inconsistent":
                refuting.append(cid)
            role = getattr(c, "check_role", None)
            if role == "validity" and _claim_validity_ok(c):
                tid = getattr(c, "template_id", None)
                if tid is None or str(tid) == "":
                    raise JournalFormatError(
                        f"validity claim {cid!r} missing template_id",
                        field="template_id",
                    )
                if tid not in aux:
                    aux.append(str(tid))
        else:
            ex = exclusion_reason(c)
            if ex is None:
                raise JournalFormatError(
                    f"excluded claim {cid!r} missing exclusion_reason",
                    field="exclusion_reason",
                )
            excluded_rows.append({"claim_id": cid, "reason": ex})
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
            r = require_journal_field(row, "reason", ctx="excluded")
            reasons[str(r)] = reasons.get(str(r), 0) + 1
        excl_txt = (
            f"{sum(reasons.values())} claims excluded ("
            + ", ".join(f"{r} {n}" for r, n in sorted(reasons.items()))
            + ")"
        )
        severity_note = f"{severity_note}; {excl_txt}" if severity_note else excl_txt
    if rule is None or str(rule) == "":
        raise JournalFormatError(
            "hypothesis_report_row requires typed rule",
            field="rule",
        )
    rule_s = str(rule)
    out: dict[str, Any] = {
        "hypothesis_id": hypothesis_id,
        "question_id": question_id,
        "verdict": verdict_value,
        "rule": rule_s,
        "counted": counted_rows,
        "excluded": excluded_rows,
        "conflict": conflict,
        "severity_note": severity_note,
        "auxiliary_assumptions": aux,
        "legacy_claims": legacy_claims,
    }
    # Refute-role timeout: verdict alone is inconclusive — surface explicit
    # not-tested so conclusions/report consumers do not treat it as a finished
    # inconclusive assessment.
    if rule_s == "refute_unresolved":
        out["not_tested"] = True
    return out


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
        key = question_verdict if question_verdict is not None else "untested"
        return mapping[key]
    vals = list(hypothesis_verdicts.values())
    if any(v == "supported" for v in vals):
        return "answered"
    if any(v == "survived" for v in vals) and not any(v == "supported" for v in vals):
        return "survived"
    if vals and all(v == "refuted" for v in vals):
        return "refuted"
    return "inconclusive"
