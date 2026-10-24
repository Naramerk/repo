"""Minimal InputBinding / dependency resolution (DESIGN §3.4 + M1 PR7/PR8)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from coscientist.core.claims.claims import (
    namespaced_claim_id,
    producer_attempt_failed,
    resolve_existing_target,
    whitelist_claim_attrs,
)
from coscientist.core.claims.graph import GraphEdge, GraphNode
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import experiment_node_id
from coscientist.core.events.schema_versions import (
    GRAPH_EDGE_ADDED_SCHEMA_VERSION,
    GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
)
from coscientist.core.execution.domain import (
    VALID_PROVENANCE,
    InputBinding,
    InputSpec,
    Provenance,
    Task,
    TaskOutcome,
    TaskSpec,
    TaskState,
    worst_provenance,
)

# Re-export for callers/tests that imported from inputs.
__all__ = [
    "InputMode",
    "InputUnavailable",
    "worst_provenance",
    "normalize_input_mode",
    "input_binding_digest",
    "verdict_binding_digest",
    "bind_inputs",
    "claim_nodes_from_attempt",
    "uses_claim_graph_events",
    "validate_input_specs",
    "collect_bindable_claims",
]

class InputMode(StrEnum):
    USE_PARTIAL = "use_partial"
    SIMULATE = "simulate"
    FORBID = "forbid"

def normalize_input_mode(spec: InputSpec) -> InputMode:
    return InputMode(spec.mode)


def _require_bool(value: object, *, what: str) -> bool:
    if type(value) is not bool:
        raise JournalFormatError(f"{what} must be bool, got {type(value).__name__}")
    return value


def _require_str(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise JournalFormatError(f"{what} must be str, got {type(value).__name__}")
    return value


def _coerce_float(value: object, *, what: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise JournalFormatError(f"{what} must be numeric, got {type(value).__name__}")
    return float(value)  # audit-ok[coerce_float]: coerce after int|float type gate (bool rejected)


class InputUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

def input_binding_digest(
    *,
    kind: str,
    input_name: str,
    artifact_id: str,
    source: str,
    ref: str | None,
) -> str:
    """P2 snapshot digest of **binding metadata** (journaled on AttemptStarted).

    Covers which input was bound and at what provenance — not claim/artifact
    body bytes. Claim content lives on the graph node; content changes are
    separate ``GraphNodeUpserted`` revisions (DESIGN_M1 §3).
    """
    return sha256_hex(
        canonical_json(
            {
                "kind": kind,
                "input": input_name,
                "artifact_id": artifact_id,
                "source": source,
                "ref": ref,
                "at": "P2",
            }
        )
    )

def verdict_binding_digest(*, ref: str, snapshot: dict[str, Any]) -> str:
    """P2 digest of a persisted verdict snapshot (``InputSpec.kind=verdict``)."""
    return sha256_hex(
        canonical_json(
            {
                "kind": "verdict",
                "ref": ref,
                "verdict": snapshot.get("verdict"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                "by_literature": _require_bool(
                    snapshot["by_literature"], what="verdict.by_literature"
                ),
                "provenance": snapshot["provenance"],
                "refutations_needed": snapshot["refutations_needed"],
                "refutation_priority": _require_bool(
                    snapshot["refutation_priority"], what="verdict.refutation_priority"
                ),
                "at": "P2",
            }
        )
    )

def _claim_source(
    ref: str,
    *,
    claim_provenance: dict[str, Provenance],
) -> Provenance:
    """Provenance of the producing attempt (H-b).

    Unknown / missing attrs → ``stub`` (never treat as real).
    """
    prov = claim_provenance.get(ref)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if prov not in VALID_PROVENANCE:
        return "stub"
    return prov  # type: ignore[return-value]

def bind_inputs(
    task: Task,
    *,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str] | None = None,
    claim_provenance: dict[str, Provenance] | None = None,
    verdict_snapshots: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[InputBinding], Provenance]:
    """
    Resolve TaskSpec.inputs (artifact | claim | verdict).

    Raises InputUnavailable when mode=forbid and data missing / dep FAILED, when
    claim ``ref`` is missing / unknown / produced by a FAILED attempt, or when
    a verdict snapshot for ``ref`` is unavailable.
    """
    bindings = []
    provs: list[Provenance] = ["real"]
    if known_claim_ids is None:
        claims = set()
    else:
        claims = known_claim_ids
    if claim_provenance is None:
        claim_prov = {}
    else:
        claim_prov = claim_provenance
    if verdict_snapshots is None:
        snapshots = {}
    else:
        snapshots = verdict_snapshots
    for spec in task.spec.inputs:
        kind = spec.kind
        if kind == "verdict":
            if not spec.ref:
                raise InputUnavailable("verdict_ref_missing")
            snap = snapshots.get(spec.ref)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if snap is None:
                raise InputUnavailable("verdict_unavailable")
            if "provenance" not in snap:
                raise InputUnavailable("verdict_provenance_missing")
            source_raw = snap["provenance"]
            if source_raw not in VALID_PROVENANCE:
                raise InputUnavailable(f"verdict_provenance_invalid:{source_raw!r}")
            source: Provenance = source_raw  # type: ignore[assignment]
            digest = verdict_binding_digest(ref=spec.ref, snapshot=snap)
            bindings.append(
                InputBinding(
                    input=spec.ref,
                    artifact_id="",
                    source=source,
                    kind="verdict",
                    ref=spec.ref,
                    digest=digest,
                )
            )
            provs.append(source)
            continue
        if kind == "claim":
            if not spec.ref:
                raise InputUnavailable("claim_ref_missing")
            if spec.ref not in claims:
                raise InputUnavailable("claim_unavailable")
            source = _claim_source(spec.ref, claim_provenance=claim_prov)
            digest = input_binding_digest(
                kind="claim",
                input_name=spec.ref,
                artifact_id="",
                source=source,
                ref=spec.ref,
            )
            bindings.append(
                InputBinding(
                    input=spec.ref,
                    artifact_id="",
                    source=source,
                    kind="claim",
                    ref=spec.ref,
                    digest=digest,
                )
            )
            provs.append(source)
            continue

        # kind=artifact (default)
        if not spec.from_task or not spec.output:
            raise InputUnavailable("artifact_spec_incomplete")
        mode = normalize_input_mode(spec)
        dep = dep_tasks.get(spec.from_task)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if dep is None or dep.state != TaskState.DONE:
            raise InputUnavailable("input_unavailable")
        if spec.from_task not in dep_outputs:
            if mode == InputMode.SIMULATE:
                arts = {}
            else:
                raise InputUnavailable("dep_outputs_missing")
        else:
            arts = dep_outputs[spec.from_task]
        aid = arts.get(spec.output)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if spec.from_task not in dep_provenance:
            if mode == InputMode.SIMULATE:
                dep_prov: Provenance = "stub"  # audit-ok[field_default]: structural absent provenance under simulate
            else:
                raise InputUnavailable("dep_provenance_missing")
        else:
            dep_prov = dep_provenance[spec.from_task]

        if dep.outcome == TaskOutcome.FAILED and mode == InputMode.FORBID:
            raise InputUnavailable("input_unavailable")

        if aid and dep.outcome == TaskOutcome.SUCCESS:
            source = "real"
        elif aid and mode in {InputMode.USE_PARTIAL, InputMode.SIMULATE}:
            source = "partial"
        elif mode == InputMode.SIMULATE:
            source = "stub"
            aid = f"stub:{spec.from_task}:{spec.output}"
        else:
            raise InputUnavailable("input_unavailable")

        if source != "stub":
            source = worst_provenance(source, dep_prov)
        if not isinstance(aid, str) or not aid:
            raise InputUnavailable("artifact_id_missing")
        digest = input_binding_digest(
            kind="artifact",
            input_name=spec.output,
            artifact_id=aid,
            source=source,
            ref=None,
        )
        bindings.append(
            InputBinding(
                input=spec.output,
                artifact_id=aid,
                source=source,
                kind="artifact",
                digest=digest,
            )
        )
        provs.append(source)
    return bindings, worst_provenance(*provs)

def _claim_structurally_ok(
    *,
    claim_id: str | None,
    target: str,
    target_id: str | None,
    graph: Any | None,
    id_collides: bool,
    target_changed: bool,
    about_retarget: bool,
) -> bool:
    """True when claim id/target/about are structurally sound for ``accepted``."""
    if not claim_id or ":claim:" not in claim_id:
        return False
    if not target or not target_id:
        return False
    if id_collides or target_changed or about_retarget:  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        return False
    if graph is None:
        return False
    dst = graph.get_node(target_id)
    return dst is not None and dst.kind in ("question", "hypothesis")

def claim_nodes_from_attempt(
    *,
    study_id: str,
    attempt_id: str,
    provenance: Provenance,
    claims: list[dict[str, Any]],
    now: datetime,
    graph: Any | None = None,
    attempt_outcome: str,
    derived: dict[str, Any] | None = None,
    derive_attrs: Any | None = None,
    infra_failure: bool,
) -> tuple[list[UnstampedEvent], list[dict[str, str]]]:
    """Emit ``GraphNodeUpserted`` (+ ``about`` edges) for agent claims.

    Returns ``(events, drops)`` where drops are ``{key, reason}`` for rejected
    claims (never raises). Agent fields are whitelisted; counting fields come
    from ``derive_attrs(raw, validated)`` or static ``derived`` (Executor).

    ``infra_failure=True`` (crash / timeout / catalog / invalid output) drops
    all claims. Scientific FAILED/PARTIAL (criteria not met) **keep** claims so
    ``not_met`` can refute (graph_schema invariant 10).
    """
    drops: list[dict[str, str]] = []  # audit-ok[field_default]: structural empty = collecting drop records
    if infra_failure:
        return [], drops
    if provenance not in VALID_PROVENANCE:
        provenance = "stub"  # type: ignore[assignment]
    events = []
    for i, raw in enumerate(claims):
        if not isinstance(raw, dict):
            drops.append({"key": str(i), "reason": "not_a_dict"})
            continue
        agent_key = str(raw.get("id") or raw.get("claim_id") or f"idx{i}")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # audit-ok[or_call]: coalesce existing call result; no invented literal  # audit-ok[or_call]: coalesce existing call result; no invented literal
        cid = namespaced_claim_id(study_id, attempt_id, agent_key)
        if cid is None:
            drops.append({"key": agent_key, "reason": "foreign_claim_id"})
            continue
        attrs = whitelist_claim_attrs(raw)
        target = attrs.get("target")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if not target:
            drops.append({"key": agent_key, "reason": "target_missing"})
            continue
        resolved = resolve_existing_target(study_id, str(target), graph)
        if resolved is None:
            drops.append({"key": agent_key, "reason": "target_unknown"})
            continue
        target_id, _target_kind = resolved

        id_collides = False
        target_changed = False
        about_retarget = False
        if graph is not None:
            existing = graph.get_node(cid)
            if existing is not None:
                if existing.kind != "claim":
                    id_collides = True
                else:
                    old_target = existing.attrs.get("target")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    if old_target is not None and str(old_target) != str(target):
                        target_changed = True
                    old_about = [e for e in graph.edges_from(cid) if e.kind == "about"]
                    if old_about and old_about[0].dst_id != target_id:
                        about_retarget = True
        if id_collides:
            drops.append({"key": agent_key, "reason": "id_collides_non_claim"})
            continue
        if target_changed:
            drops.append({"key": agent_key, "reason": "target_change_forbidden"})
            continue
        if about_retarget:
            drops.append({"key": agent_key, "reason": "about_retarget_forbidden"})
            continue

        # Re-verify structural gates independently (not control-flow tautology).
        structurally_ok = _claim_structurally_ok(
            claim_id=cid,
            target=_require_str(target, what="claim.target"),
            target_id=target_id,
            graph=graph,
            id_collides=id_collides,
            target_changed=target_changed,
            about_retarget=about_retarget,
        )

        new_prov: Provenance = provenance
        if graph is not None:
            existing = graph.get_node(cid)
            if existing is not None:
                old = existing.attrs.get("provenance")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                if old in VALID_PROVENANCE:
                    new_prov = worst_provenance(old, provenance)  # type: ignore[arg-type]

        # Core-derived counting fields; derive_attrs must never raise into caller.
        try:
            if derive_attrs is not None:
                derived_core = derive_attrs(raw, structurally_ok)
                if derived_core is None:
                    raise JournalFormatError("derive_attrs returned None")
                core = dict(derived_core)
            else:
                if derived is None:
                    core = {}
                else:
                    core = dict(derived)
        except Exception:  # audit-ok[broad_except]: derive_attrs must stay total; drop claim not abort attempt
            drops.append({"key": agent_key, "reason": "derive_attrs_error"})
            continue # audit-ok[except_silent_continue]: paired with derive_attrs totality drop above  # audit-ok[except_silent_continue]: skip malformed row; fail-closed on required paths elsewhere
        attrs["provenance"] = new_prov
        attrs["attempt_id"] = attempt_id
        attrs["attempt_outcome"] = attempt_outcome
        for req in (
            "criterion_status",
            "evidence_kind",
            "frozen",
            "lit_own_criterion",
            "accepted",
            "validity_ok",
            "check_role",
        ):
            if req not in core:
                raise JournalFormatError(f"claim core missing {req!r}")
        attrs["criterion_status"] = core["criterion_status"]
        attrs["evidence_kind"] = core["evidence_kind"]
        attrs["frozen"] = _require_bool(core["frozen"], what="claim.frozen")
        attrs["lit_own_criterion"] = _require_bool(
            core["lit_own_criterion"], what="claim.lit_own_criterion"
        )
        if "derivation_executable" in core:
            attrs["derivation_executable"] = _require_bool(
                core["derivation_executable"], what="claim.derivation_executable"
            )
        attrs["accepted"] = _require_bool(core["accepted"], what="claim.accepted")
        attrs["validity_ok"] = _require_bool(core["validity_ok"], what="claim.validity_ok")
        role = core["check_role"]
        if role not in ("prediction", "validity", "unknown"):
            raise JournalFormatError(f"claim check_role invalid: {role!r}")
        attrs["check_role"] = role
        excl = core.get("exclusion_reason")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if excl in (
            "validity_failed",
            "unknown_role",
            "stub",
            "partial",
            "invalid",
            "indeterminate",
            "lit",
            "r17",
            "superseded",
            "foreign_artifact",
            "output_not_submitted",
            "artifact_missing",
            "pointer_empty",
        ):
            attrs["exclusion_reason"] = excl
        # Sole outcome key is claim_outcome — never invent from criterion_status.
        if "outcome" in core and "claim_outcome" not in core:
            raise JournalFormatError(
                "claim core uses deprecated 'outcome'; require 'claim_outcome'"
            )
        if "claim_outcome" not in core:
            raise JournalFormatError(
                "claim claim_outcome required "
                "(consistent|inconsistent|indeterminate); "
                "do not derive from criterion_status"
            )
        oc = core["claim_outcome"]
        if oc is None or oc not in ("consistent", "inconsistent", "indeterminate"):
            raise JournalFormatError(
                "claim claim_outcome required "
                f"(consistent|inconsistent|indeterminate), got {oc!r}"
            )
        attrs["claim_outcome"] = oc
        # sesoi only from core (catalog-frozen criterion) — never stamp 0.0 freely.
        for num_key in ("p_value", "power", "sesoi", "alpha"):
            if core.get(num_key) is not None:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                attrs[num_key] = _coerce_float(core[num_key], what=f"claim.{num_key}")
        for flag in ("uses_sesoi", "used_sesoi", "predicts_null"):
            if flag in core:
                attrs[flag] = _require_bool(core[flag], what=f"claim.{flag}")
        if core.get("predicted_direction") in ("positive", "negative", "two_sided"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["predicted_direction"] = _require_str(
                core["predicted_direction"], what="claim.predicted_direction"
            )
        if isinstance(core.get("data_digests"), list):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["data_digests"] = [
                _require_str(x, what="claim.data_digests[]") for x in core["data_digests"]
            ]
        if isinstance(core.get("criterion_template_ids"), list):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["criterion_template_ids"] = [
                _require_str(x, what="claim.criterion_template_ids[]")
                for x in core["criterion_template_ids"]
                if x
            ]
        if isinstance(core.get("independence_notes"), list) and core["independence_notes"]:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["independence_notes"] = [
                _require_str(x, what="claim.independence_notes[]")
                for x in core["independence_notes"]
            ]
        if core.get("study_id"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["study_id"] = _require_str(core["study_id"], what="claim.study_id")
        if core.get("experiment_id"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["experiment_id"] = _require_str(
                core["experiment_id"], what="claim.experiment_id"
            )
        else:
            # Structured attempt ids are ``{task_id}#{n}`` — exactly one # + digits.
            if not isinstance(attempt_id, str) or attempt_id.count("#") != 1:
                raise JournalFormatError(
                    f"claim experiment_id missing and attempt_id not structured: {attempt_id!r}"
                )
            base, sep, n = attempt_id.partition("#")  # audit-ok[str_partition]: structured attempt_id allowlist parse
            if sep != "#" or not base or not n.isdigit():
                raise JournalFormatError(
                    f"claim experiment_id empty/invalid from attempt_id: {attempt_id!r}"
                )
            attrs["experiment_id"] = base
        if core.get("independence_key"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["independence_key"] = _require_str(
                core["independence_key"], what="claim.independence_key"
            )
        else:
            from coscientist.core.claims.verdict import independence_key_for

            exp_id = attrs.get("experiment_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if not isinstance(exp_id, str) or not exp_id:
                raise JournalFormatError("claim experiment_id required for independence_key")
            attrs["independence_key"] = independence_key_for(experiment_id=exp_id)
        if "refute_capable" in core:
            attrs["refute_capable"] = _require_bool(
                core["refute_capable"], what="claim.refute_capable"
            )
        if "severe" in core:
            attrs["severe"] = _require_bool(core["severe"], what="claim.severe")
        elif not attrs["validity_ok"]:
            attrs["severe"] = False
        else:
            from coscientist.core.claims.verdict import claim_is_severe

            for req in ("claim_outcome", "frozen", "predicts_null", "predicted_direction"):
                if req not in attrs and req not in ("claim_outcome",):
                    pass
            if "predicted_direction" not in attrs:
                raise JournalFormatError("claim missing predicted_direction for severe probe")
            if "predicts_null" not in attrs:
                raise JournalFormatError("claim missing predicts_null for severe probe")
            probe = type(
                "P",
                (),
                {
                    "claim_outcome": attrs.get("claim_outcome"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    "validity_ok": attrs["validity_ok"],
                    "frozen": attrs["frozen"],
                    "sesoi": attrs.get("sesoi"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    "uses_sesoi": attrs.get("uses_sesoi"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    "used_sesoi": attrs.get("used_sesoi"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                    "predicts_null": attrs["predicts_null"],
                    "predicted_direction": attrs["predicted_direction"],
                    "effect": (
                        dict(core["effect"])
                        if isinstance(core.get("effect"), dict)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                        else None  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
                    ),
                    "severe": None,
                    "criterion_frozen_at_seq": None,
                    "first_cited_evidence_seq": None,
                },
            )()
            attrs["severe"] = claim_is_severe(probe)
        if core.get("artifact_id"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            attrs["artifact_id"] = core["artifact_id"]
        if isinstance(core.get("effect"), dict):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            # No invented call_id: missing produced_by_call_id ⇒ not countable.
            attrs["effect"] = dict(core["effect"])

        node = GraphNode(
            node_id=cid,
            kind="claim",
            study_id=study_id,
            ref_id=cid,
            attrs=attrs,
            created_at=now,
        )
        events.append(
            UnstampedEvent(
                study_id=study_id,
                type="GraphNodeUpserted",
                entity_key=cid,
                idem_key=make_idem("GraphNodeUpserted", cid, attempt_id),
                payload=node.model_dump(mode="json"),
                ts=now,
                schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
            )
        )
        have_about = False
        if graph is not None:
            have_about = any(e.kind == "about" for e in graph.edges_from(cid))
        if any(
            ev.type == "GraphEdgeAdded"
            and ev.payload["src_id"] == cid
            and ev.payload["kind"] == "about"
            for ev in events
        ):
            have_about = True
        if not have_about:
            eid = f"about:{cid}:{target_id}"
            edge = GraphEdge(
                edge_id=eid,
                kind="about",
                study_id=study_id,
                src_id=cid,
                dst_id=target_id,
                attrs={},
                created_at=now,
            )
            events.append(
                UnstampedEvent(
                    study_id=study_id,
                    type="GraphEdgeAdded",
                    entity_key=eid,
                    idem_key=make_idem("GraphEdgeAdded", eid),
                    payload=edge.model_dump(mode="json"),
                    ts=now,
                    schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
                )
            )
    return events, drops

def uses_claim_graph_events(
    *,
    study_id: str,
    task: Task,
    bindings: list[InputBinding],
    now: datetime,
    graph: Any | None = None,
) -> list[UnstampedEvent]:
    """Build GraphNodeUpserted (experiment) + GraphEdgeAdded (uses_claim) events.

    Claim nodes must already exist in ``graph`` (or be upserted by the caller).
    """
    claim_bindings = [b for b in bindings if b.kind == "claim" and b.ref]
    if not claim_bindings:
        return []
    exp_id = experiment_node_id(study_id, task.spec.task_key)
    events = []
    # Do not emit empty-attrs upserts that would clobber richer nodes.
    if graph is None or graph.get_node(exp_id) is None:
        node = GraphNode(
            node_id=exp_id,
            kind="experiment",
            study_id=study_id,
            ref_id=task.spec.task_key,
            attrs={"task_key": task.spec.task_key},
            created_at=now,
        )
        events.append(
            UnstampedEvent(
                study_id=study_id,
                type="GraphNodeUpserted",
                entity_key=exp_id,
                idem_key=make_idem("GraphNodeUpserted", exp_id),
                payload=node.model_dump(mode="json"),
                ts=now,
                schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
            )
        )
    for b in claim_bindings:
        if b.ref is None:
            raise InputUnavailable("claim_ref_missing")
        if graph is not None and graph.get_node(b.ref) is None:
            raise InputUnavailable("claim_node_missing")
        eid = f"uses_claim:{exp_id}:{b.ref}"
        edge = GraphEdge(
            edge_id=eid,
            kind="uses_claim",
            study_id=study_id,
            src_id=exp_id,
            dst_id=b.ref,
            attrs={},
            created_at=now,
        )
        events.append(
            UnstampedEvent(
                study_id=study_id,
                type="GraphEdgeAdded",
                entity_key=eid,
                idem_key=make_idem("GraphEdgeAdded", eid),
                payload=edge.model_dump(mode="json"),
                ts=now,
                schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
            )
        )
    return events

def validate_input_specs(tasks: list[TaskSpec]) -> None:
    """Reject invalid InputSpec at accept_plan (kind-specific fields)."""
    from coscientist.core.execution.quorum import validate_quorum_specs

    for t in tasks:
        for inp in t.inputs:
            kind = inp.kind
            if kind == "verdict":
                if not inp.ref:
                    raise ValueError(
                        f"InputSpec.kind=verdict requires ref "
                        f"(task {t.task_key!r})"
                    )
            elif kind == "claim":
                if not inp.ref:
                    raise ValueError(
                        f"InputSpec.kind=claim requires ref "
                        f"(task {t.task_key!r})"
                    )
            elif kind == "artifact":
                if not inp.from_task or not inp.output:
                    raise ValueError(
                        f"InputSpec.kind=artifact requires from_task and output "
                        f"(task {t.task_key!r})"
                    )
            else:
                raise ValueError(f"unknown InputSpec.kind: {kind!r}")
    validate_quorum_specs(tasks)

_FORBIDDEN_THRESHOLD_PARAMS = frozenset(
    {"sesoi", "min_power", "predicts_null", "power", "uses_sesoi", "predicted_direction"}
)

def validate_plan_criteria(tasks: list[TaskSpec], catalog: Any) -> None:
    """Reject unknown templates, role mismatches, and planner-set SESOI params.

    Role / sesoi / predicts_null are catalog-authored. ``RequirementRef.params``
    must not carry threshold fields; out-of-range catalog sesoi is rejected at
    catalog load. Unknown ``template_id`` → reject.
    """
    if catalog is None:
        raise ValueError("accept_plan requires catalog to validate criteria")
    try:
        templates = catalog.templates
    except AttributeError as e:
        raise ValueError("catalog.templates required") from e
    by_id = {t.id: t for t in templates}
    for spec in tasks:
        for ref in list(spec.criteria):
            tid = ref.template_id
            if not tid or tid not in by_id:
                raise ValueError(
                    f"unknown criterion template {tid!r} "
                    f"(task {spec.task_key!r})"
                )
            tmpl = by_id[tid]
            raw_params = ref.params
            if raw_params is None:
                params = {}
            else:
                params = dict(raw_params)
            bad = sorted(_FORBIDDEN_THRESHOLD_PARAMS & set(params))
            if bad:
                raise ValueError(
                    f"criterion params {bad} must come from the catalog template "
                    f"{tid!r}, not RequirementRef.params (task {spec.task_key!r})"
                )
            # Catalog sesoi already validated at load; re-check bounds here.
            if tmpl.uses_sesoi:
                sesoi = tmpl.sesoi
                lo = tmpl.sesoi_min
                hi = tmpl.sesoi_max
                if sesoi is None:
                    raise ValueError(
                        f"template {tid!r} uses_sesoi but sesoi is unset"
                    )
                if lo is not None and _coerce_float(sesoi, what=f"template {tid!r}:sesoi") < _coerce_float(
                    lo, what=f"template {tid!r}:sesoi_min"
                ):
                    raise ValueError(
                        f"template {tid!r} sesoi {sesoi} < sesoi_min {lo}"
                    )
                if hi is not None and _coerce_float(sesoi, what=f"template {tid!r}:sesoi") > _coerce_float(
                    hi, what=f"template {tid!r}:sesoi_max"
                ):
                    raise ValueError(
                        f"template {tid!r} sesoi {sesoi} > sesoi_max {hi}"
                    )
            plan_role = ref.role
            if plan_role is None:
                continue
            tmpl_role = tmpl.role
            if plan_role != tmpl_role:
                raise ValueError(
                    f"criterion role {plan_role!r} disagrees with template "
                    f"{tid!r} role {tmpl_role!r} (task {spec.task_key!r})"
                )

def collect_bindable_claims(
    graph: Any,
    *,
    results: dict[str, dict[str, Any]] | None = None,
) -> tuple[set[str], dict[str, Provenance]]:
    """Claim ids eligible as ``kind=claim`` inputs + their provenance.

    Skips claims whose producing attempt is an **infra** failure; scientific
    FAILED (criteria not met) remains bindable.
    """
    known: set[str] = set()
    claim_provenance: dict[str, Provenance] = {}  # audit-ok[field_default]: structural empty = collecting claim provenance
    if graph is None:
        return known, claim_provenance
    try:
        nodes = graph.nodes_by_kind("claim")
    except KeyError as e:
        raise JournalFormatError("graph missing claim kind") from e
    for node in nodes:
        attrs = node.attrs
        aid = attrs.get("attempt_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if producer_attempt_failed(str(aid) if aid else None, results=results):  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
            continue
        known.add(node.node_id)
        p = attrs.get("provenance")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        if p in VALID_PROVENANCE:
            claim_provenance[node.node_id] = p  # type: ignore[assignment]
    return known, claim_provenance
