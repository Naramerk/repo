"""Claim-core derivation helpers for Executor.record (DESIGN §4 / KindSpec).

Moved out of ``executor.py`` without behavior change. Callers import from this
module — no re-export shim on ``executor``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coscientist.checks.evaluate import role_from_catalog
from coscientist.core.events.ids import criterion_node_id
from coscientist.core.execution.domain import (
    ExperimentMethod,
    ReqStatus,
    TaskOutcome,
)


def _req_to_criterion_status(status: ReqStatus) -> str:
    if status in (ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE):
        return "met"
    if status in (ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR):
        return "not_met"
    return "unassessed"


def _check_field(c: Any, name: str) -> Any:
    if isinstance(c, dict):
        return c.get(name)
    return getattr(c, name, None)


def _normalize_to_template_id(key: str, checks: list[Any], criteria: list[Any]) -> str:
    """Map check_id (``value_match#0``) → template_id; leave template_id as-is."""
    for c in checks:
        tid = _check_field(c, "template_id")
        cid = _check_field(c, "check_id")
        if cid is not None and str(cid) == key and tid:
            return str(tid)
        if tid is not None and str(tid) == key:
            return str(tid)
    for ref in criteria:
        tid = _check_field(ref, "template_id")
        if tid is not None and str(tid) == key:
            return str(tid)
    return key


def _criterion_key_for_claim(raw: dict[str, Any], checks: list[Any], criteria: list[Any]) -> str | None:
    """Resolve checked_against → template_id, else sole check / sole plan criterion."""
    checked = raw.get("checked_against")
    if checked:
        return _normalize_to_template_id(str(checked), checks, criteria)
    if len(checks) == 1:
        tid = _check_field(checks[0], "template_id")
        return str(tid) if tid else None
    if len(criteria) == 1:
        tid = _check_field(criteria[0], "template_id")
        return str(tid) if tid else None
    return None


def _criterion_status_for_claim(raw: dict[str, Any], checks: list[Any], criteria: list[Any] | None = None) -> str:
    """Map claim.checked_against → ReqStatus of that criterion's check."""
    crit = list(criteria or [])
    key = _criterion_key_for_claim(raw, checks, crit)
    if key:
        for c in checks:
            tid = _check_field(c, "template_id")
            cid = _check_field(c, "check_id")
            st = _check_field(c, "status")
            # Exact match only (no endswith — avoids C11 matching ``1``).
            if (tid is not None and str(tid) == key) or (cid is not None and str(cid) == key):
                if isinstance(st, ReqStatus):
                    return _req_to_criterion_status(st)
                try:
                    return _req_to_criterion_status(ReqStatus(str(st)))
                except ValueError:
                    return "unassessed"
        return "unassessed"
    if len(checks) == 1:
        st = _check_field(checks[0], "status")
        if isinstance(st, ReqStatus):
            return _req_to_criterion_status(st)
    return "unassessed"


def _check_role(c: Any) -> str:
    role = _check_field(c, "role")
    if role in ("prediction", "validity", "unknown"):
        return str(role)
    return "unknown"


def _is_rubric_check(c: Any) -> bool:
    """Rubric checks are validity-role but must not flip claim validity_ok (PR13)."""
    effect = _check_field(c, "effect")
    if isinstance(effect, dict) and effect.get("rubric_kind") == "rubric":
        return True
    tid = str(_check_field(c, "template_id") or "")
    cid = str(_check_field(c, "check_id") or "")
    return tid.startswith("rubric:") or cid.startswith("rubric:")


def _required_validity_failed(checks: list[Any]) -> bool:
    """True when a required validity check FAILED (Duhem–Quine cap).

    Rubric checks are excluded: a failed rubric must not mark claims
    ``validity_failed`` (PR 8b counting stays independent of rubric).
    """
    for c in checks:
        if _is_rubric_check(c):
            continue
        if _check_role(c) != "validity":
            continue
        if not bool(_check_field(c, "required")):
            continue
        st = _check_field(c, "status")
        if isinstance(st, ReqStatus):
            failed = st in (ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR)
        else:
            failed = str(st) in (
                ReqStatus.FAILED.value,
                ReqStatus.MIGRATION_ERROR.value,
            )
        if failed:
            return True
    return False


_NO_EFFECT_DETAIL_ALIASES = {
    "foreign_artifact": "foreign_artifact",
    "output_not_submitted": "output_not_submitted",
    "artifact_missing": "artifact_missing",
    "pointer_empty": "pointer_empty",
    # Pre-normalization detail strings (compat).
    "output missing": "output_not_submitted",
    "artifact missing": "artifact_missing",
}


def _no_effect_reason_from_checks(checks: list[Any]) -> str | None:
    """Specific reason when a prediction FAILED with no numeric effect."""
    for c in checks or ():
        if _check_role(c) != "prediction":
            continue
        st = _check_field(c, "status")
        if isinstance(st, ReqStatus):
            failed = st in (ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR)
        else:
            failed = str(st) in (
                ReqStatus.FAILED.value,
                ReqStatus.MIGRATION_ERROR.value,
            )
        if not failed:
            continue
        # No-effect failures never carry a countable effect blob.
        if isinstance(_check_field(c, "effect"), dict):
            continue
        detail = str(_check_field(c, "detail") or "")
        mapped = _NO_EFFECT_DETAIL_ALIASES.get(detail)
        if mapped is not None:
            return mapped
    return None


def _role_for_claim_key(
    raw: dict[str, Any],
    checks: list[Any],
    criteria: list[Any],
    *,
    catalog: Any | None = None,
) -> str:
    """Role of the check this claim is checked_against (catalog / CheckResult only)."""
    key = _criterion_key_for_claim(raw, checks, criteria)
    if key:
        for c in checks:
            tid = _check_field(c, "template_id")
            cid = _check_field(c, "check_id")
            if (tid is not None and str(tid) == key) or (cid is not None and str(cid) == key):
                return _check_role(c)
        # Fall back to catalog by template_id (never plan RequirementRef.role).
        return role_from_catalog(str(key), catalog)
    if len(checks) == 1:
        return _check_role(checks[0])
    return "unknown"


def _criterion_node(graph: Any, study_id: str, template_id: Any) -> Any | None:
    if not template_id or graph is None:
        return None
    key = str(template_id)
    node = graph.get_node(criterion_node_id(study_id, key))
    if node is None:
        node = graph.get_node(key)
    if node is None or getattr(node, "kind", None) != "criterion":
        return None
    return node


def _criterion_is_frozen(graph: Any, study_id: str, template_id: Any) -> bool:
    """True when plan-accept freeze marked the criterion node (keyed by template_id)."""
    node = _criterion_node(graph, study_id, template_id)
    if node is None:
        return False
    attrs = node.attrs or {}
    return attrs.get("frozen_at_seq") is not None or bool(attrs.get("frozen"))


def _criterion_freeze_attrs(graph: Any, study_id: str, template_id: Any) -> dict[str, Any]:
    """sesoi / predicts_null / uses_sesoi from the frozen criterion (catalog)."""
    node = _criterion_node(graph, study_id, template_id)
    attrs = (node.attrs or {}) if node is not None else {}
    out: dict[str, Any] = {}
    if attrs.get("sesoi") is not None:
        try:
            out["sesoi"] = float(attrs["sesoi"])
        except (TypeError, ValueError):
            pass
    if attrs.get("alpha") is not None:
        try:
            out["alpha"] = float(attrs["alpha"])
        except (TypeError, ValueError):
            pass
    if "predicts_null" in attrs:
        out["predicts_null"] = bool(attrs.get("predicts_null"))
    if "uses_sesoi" in attrs:
        out["uses_sesoi"] = bool(attrs.get("uses_sesoi"))
    if attrs.get("predicted_direction") in ("positive", "negative", "two_sided"):
        out["predicted_direction"] = str(attrs["predicted_direction"])
    return out


def _effect_from_checks(
    checks: list[Any],
    *,
    attempt_id: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Core-computed effect from CheckResult.effect (never agent detail / claims)."""
    from coscientist.core.claims.verdict import recompute_effect_provenance

    for c in checks or ():
        if _check_role(c) != "prediction":
            continue
        data = _check_field(c, "effect")
        if not isinstance(data, dict):
            continue
        try:
            effect: dict[str, Any] = {
                "est": float(data["est"]) if data.get("est") is not None else None,
                "kind": str(data.get("kind") or "point"),
                "producer": str(data.get("producer") or "agent"),
                "core_evaluated": bool(data.get("core_evaluated", False)),
                "produced_by_call_id": str(data.get("produced_by_call_id") or ""),
                "artifact_id": str(data.get("artifact_id") or ""),
            }
            if data.get("ci_low") is not None and data.get("ci_high") is not None:
                effect["ci_low"] = float(data["ci_low"])
                effect["ci_high"] = float(data["ci_high"])
            # Preserve isolation / observation stamps (effect-only; not from meta).
            for key in (
                "net_isolation",
                "fs_isolation",
                "trusted",
                "countable",
                "note",
                "observation_only",
                "timeout_kind",
                "refute_unresolved",
                "cpu_usage_s",
                "wall_s",
                # PR20 proposed_check stamps
                "proposed_check",
                "proposal_id",
                "proposal_digest",
                "delegated",
                "qualitative",
                "never_severe",
                "severe_eligible",
                "exclusion_reason",
            ):
                if key in data:
                    effect[key] = data[key]
            recomputed = recompute_effect_provenance(effect, attempt_id=attempt_id)
            if recomputed is not None:
                effect = recomputed
        except (TypeError, ValueError, KeyError):
            continue
        used = bool(_check_field(c, "used_sesoi"))
        return effect, used
    return None, False


def _derive_claim_core_attrs(
    task: Any,
    task_outcome: TaskOutcome,
    *,
    raw: dict[str, Any],
    checks: list[Any],
    graph: Any,
    study_id: str,
    validated: bool,
    catalog: Any | None = None,
    bindings: list[Any] | None = None,
    artifact_store: Any | None = None,
    results_map: Mapping[str, Mapping[str, Any]] | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Core-owned counting fields from plan/checks/freeze — never agent attrs.

    ``accepted`` means the result was recorded and the claim is structurally
    valid — **not** that criteria passed (graph_schema invariant 10).

    Check ``role`` (catalog only, Duhem–Quine):
    - ``prediction`` FAILED → ``criterion_status=not_met`` → counting refute.
    - required ``validity`` FAILED → claim not counted (``validity_ok=False``);
      excluded as ``validity_failed`` (per experiment; other valid claims still count).
    - ``unknown`` → ``validity_ok=False``, excluded as ``unknown_role``; never
      refutes, never validates, never forces a question/hypothesis verdict.
    """
    del task_outcome  # outcome is scientific whenever we derive; criteria ≠ accepted
    spec = getattr(task, "spec", None)
    method = getattr(spec, "method", None) if spec is not None else None
    lit = method == ExperimentMethod.LITERATURE_SEARCH or str(method or "") == ("literature_search")
    criteria = list(getattr(spec, "criteria", None) or []) if spec is not None else []
    key = _criterion_key_for_claim(raw, checks, criteria)
    role = _role_for_claim_key(raw, checks, criteria, catalog=catalog)
    status = _criterion_status_for_claim(raw, checks, criteria)
    validity_ok = True
    exclusion_reason: str | None = None
    # PR20: unapproved proposed_check never counts (even if evaluation ran).
    for c in checks or ():
        eff = getattr(c, "effect", None)
        if isinstance(eff, dict) and eff.get("exclusion_reason") == "unapproved_check":
            exclusion_reason = "unapproved_check"
            validity_ok = False
            status = "unassessed"
            break
        detail = str(getattr(c, "detail", "") or "")
        if detail.startswith("unapproved_check"):
            exclusion_reason = "unapproved_check"
            validity_ok = False
            status = "unassessed"
            break
    # Required validity failure invalidates this experiment's claims only.
    # Rubric (even role=validity) never flips validity_ok — see _is_rubric_check.
    if exclusion_reason == "unapproved_check":
        pass
    elif _required_validity_failed(checks):
        validity_ok = False
        status = "unassessed"
        exclusion_reason = "validity_failed"
    elif role == "unknown":
        validity_ok = False
        status = "unassessed"
        exclusion_reason = "unknown_role"
    elif role == "validity" and status == "not_met" and not any(_is_rubric_check(c) for c in (checks or ())):
        validity_ok = False
        status = "unassessed"
        exclusion_reason = "validity_failed"
    from coscientist.core.claims.verdict import (
        claim_is_severe,
        classify_outcome,
        effect_has_interval,
        effect_is_countable,
        independence_key_for,
        outcome_to_status,
        root_data_digests_from_bindings,
    )

    frozen = _criterion_is_frozen(graph, study_id, key)
    crit = _criterion_freeze_attrs(graph, study_id, key)
    sesoi = crit.get("sesoi")
    uses_sesoi = bool(crit.get("uses_sesoi", False))
    predicts_null = bool(crit.get("predicts_null", False))
    predicted_direction = str(crit.get("predicted_direction") or "two_sided")
    pred_statuses: list[str] = []
    has_prediction_check = False
    for c in checks or ():
        if _check_role(c) == "prediction":
            has_prediction_check = True
            st = getattr(getattr(c, "status", None), "value", getattr(c, "status", None))
            pred_statuses.append(str(st or ""))
    if not pred_statuses and status in ("met", "not_met", "unassessed"):
        pred_statuses = ["PASSED" if status == "met" else ("FAILED" if status == "not_met" else "")]
    # Effect from CheckResult.effect only — never agent detail / claim attrs.
    effect, used_sesoi = _effect_from_checks(list(checks or []), attempt_id=attempt_id)
    from coscientist.checks.kinds._util import check_is_guest_timeout

    timeout_kind: str | None = None
    refute_unresolved = False
    timeout_checks = [c for c in (checks or ()) if check_is_guest_timeout(c)]
    claim_outcome: str | None
    validity_s: str
    if timeout_checks:
        # Guest timeout is never a counting outcome (not k_ref / not k_sup).
        for tc in timeout_checks:
            te = getattr(tc, "effect", None) or {}
            if isinstance(te, dict) and te.get("timeout_kind"):
                timeout_kind = str(te["timeout_kind"])
            if isinstance(te, dict) and te.get("refute_unresolved"):
                refute_unresolved = True
            elif _check_role(tc) == "prediction":
                refute_unresolved = True
        claim_outcome = "indeterminate"
        validity_s = "valid" if validity_ok else "invalid"
        if effect is None:
            effect = {}
        effect = {
            **effect,
            "countable": False,
            "observation_only": True,
            "timeout_kind": timeout_kind,
            "refute_unresolved": refute_unresolved,
        }
    else:
        validity_s, claim_outcome, _ = classify_outcome(
            validity_ok=validity_ok,
            prediction_statuses=pred_statuses,
            predicts_null=predicts_null,
            sesoi=sesoi if isinstance(sesoi, float) else None,
            effect=effect,
            r17_ok=True,  # R17 runtime metadata still deferred (PR8)
        )
    if validity_s == "invalid":
        validity_ok = False
        if exclusion_reason is None:
            exclusion_reason = "validity_failed"
    elif claim_outcome == "indeterminate" and exclusion_reason is None:
        # Prefer specific no-effect reason over generic indeterminate.
        exclusion_reason = _no_effect_reason_from_checks(list(checks or [])) or ("indeterminate")
    status = outcome_to_status(
        claim_outcome,  # type: ignore[arg-type]
        validity="valid" if validity_ok else "invalid",
    )
    exp_id = ""
    try:
        exp_id = str(getattr(task, "task_id", None) or "")
    except Exception:  # noqa: BLE001
        exp_id = ""
    lookup = None
    arts = artifact_store
    if arts is not None and hasattr(arts, "get_meta"):
        lookup = arts.get_meta
    data_digests, indep_notes = root_data_digests_from_bindings(
        bindings,
        artifact_lookup=lookup,
        results=results_map,
    )
    # Plan order (deduped); shared-any structural merge uses the set.
    # Empty plan criteria → catalog.defaults["experiment"] (same as evaluate_task_checks).
    criterion_template_ids: list[str] = []
    seen_tmpl: set[str] = set()
    for r in criteria:
        tid = str(getattr(r, "template_id", None) or r or "")
        if tid and tid not in seen_tmpl:
            seen_tmpl.add(tid)
            criterion_template_ids.append(tid)
    if not criterion_template_ids and catalog is not None:
        for tid in getattr(catalog, "defaults", {}).get("experiment", []) or []:
            stid = str(tid)
            if stid and stid not in seen_tmpl:
                seen_tmpl.add(stid)
                criterion_template_ids.append(stid)
    # Base key is experiment_id; overlap / structural merge happens at count time.
    indep = independence_key_for(experiment_id=exp_id or None)
    probe = type(
        "P",
        (),
        {
            "claim_outcome": claim_outcome,
            "validity_ok": validity_ok,
            "frozen": frozen,
            "sesoi": sesoi if isinstance(sesoi, float) else None,
            "uses_sesoi": uses_sesoi,
            "used_sesoi": used_sesoi,
            "predicts_null": predicts_null,
            "predicted_direction": predicted_direction,
            "effect": effect,
            "severe": None,  # never pre-trust
            "refute_capable": None,
            "intent": "refute",  # probe capability gate only
            "check_role": "prediction" if has_prediction_check else role,
            "criterion_frozen_at_seq": None,
            "first_cited_evidence_seq": None,
        },
    )()
    severe = claim_is_severe(probe)
    # Capable refute: recompute — never trust a stored True later on replay.
    # Countable core-evaluated evidence required (trusted is a separate tag).
    refute_capable = bool(
        frozen
        and has_prediction_check
        and uses_sesoi
        and isinstance(sesoi, float)
        and used_sesoi
        and effect_is_countable(effect)
        and (effect_has_interval(effect) if predicts_null else True)
    )
    from coscientist.core.claims.derivation import (
        EVIDENCE_KIND_DERIVATION,
        executable_check_met,
        is_derivation_task_spec,
    )

    if lit:
        ek = "literature"
    elif is_derivation_task_spec(spec):
        ek = EVIDENCE_KIND_DERIVATION
    else:
        ek = "computational"
    # §6.6: only the check bound to this claim may lift review-only derivation.
    der_exec = bool(
        ek == EVIDENCE_KIND_DERIVATION
        and executable_check_met(list(checks or []), bound_key=key)
    )
    out: dict[str, Any] = {
        "criterion_status": status,
        "evidence_kind": ek,
        "frozen": frozen,
        "lit_own_criterion": bool(lit and criteria),
        "accepted": bool(validated),
        "validity_ok": validity_ok,
        "validity": validity_s,
        "check_role": role,
        "claim_outcome": claim_outcome,
        "outcome": claim_outcome,
        "severe": severe,
        "independence_key": indep,
        "experiment_id": exp_id,
        "refute_capable": refute_capable,
        "uses_sesoi": uses_sesoi,
        "used_sesoi": used_sesoi,
        "predicts_null": predicts_null,
        "predicted_direction": predicted_direction,
        "data_digests": data_digests,
        "criterion_template_ids": criterion_template_ids,
        "study_id": study_id,
        "derivation_executable": der_exec,
    }
    # KindSpec-required checked_against: agent value only — never invent from
    # sole-check / sole-criterion heuristic key (inputs drops if absent).
    raw_checked = raw.get("checked_against")
    if type(raw_checked) is str and raw_checked:
        out["checked_against"] = raw_checked
    if indep_notes:
        out["independence_notes"] = list(indep_notes)
    if isinstance(sesoi, float):
        out["sesoi"] = sesoi
    if isinstance(crit.get("alpha"), float):
        out["alpha"] = crit["alpha"]
    if effect is not None:
        out["effect"] = effect
    if timeout_kind:
        out["timeout_kind"] = timeout_kind
    if refute_unresolved:
        out["refute_unresolved"] = True
    if lit and exclusion_reason is None:
        exclusion_reason = "lit"
    if exclusion_reason is not None:
        out["exclusion_reason"] = exclusion_reason
    return out

