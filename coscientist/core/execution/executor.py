"""Executor.record — sole result-write path (DESIGN §4)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from coscientist.checks.evaluate import evaluate_task_checks, role_from_catalog
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.digest import canonical_json, payload_digest, sha256_hex
from coscientist.core.events.errors import (
    AttemptClosedError,
    CatalogError,
    CoreBugError,
    JournalFormatError,
    SandboxInfraError,
)
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import criterion_node_id, result_id
from coscientist.core.events.mapping import optional_item
from coscientist.core.events.schema_versions import (
    ATTEMPT_CLOSED_SCHEMA_VERSION,
    DOWNGRADE_REJECTED_SCHEMA_VERSION,
    LATE_WRITE_REJECTED_SCHEMA_VERSION,
    RESULT_PENDING_SCHEMA_VERSION,
    RESULT_RECORDED_SCHEMA_VERSION,
    REVIEW_RECORDED_SCHEMA_VERSION,
    ROUTE_RETURNED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.attempts import RETRY_BACKOFF_S
from coscientist.core.execution.domain import (
    VALID_PROVENANCE,
    AgentTaskOutput,
    Catalog,
    CriterionStatus,
    ExperimentMethod,
    Outcome,
    Provenance,
    ReqStatus,
    Result,
    TaskOutcome,
)
from coscientist.core.execution.inputs import claim_nodes_from_attempt
from coscientist.core.execution.outcome import compute_outcome


def _maybe_value(obj, attr="value"):  # audit-ok[param_default]: executor.py:56 param_default
    if obj is None:
        return None
    return getattr(obj, attr)


def _maybe_dump(obj, **kwargs):
    if obj is None:
        return None
    return obj.model_dump(**kwargs)


def _maybe_call(fn):
    if fn is None:
        return None
    return fn()


def _as_float_or_none(v):
    if type(v) is float:
        return v
    return None


def _as_dict_or_none(v):
    if isinstance(v, dict):
        return v
    return None


def _join_or(kinds, fallback):
    if kinds:
        return ";".join(kinds)
    return fallback


def _spec_criteria(spec: object) -> object:
    if spec is None:
        return None
    return spec.criteria  # type: ignore[attr-defined]


def _valid_label(validity_ok: bool) -> Literal["valid", "invalid"]:
    if validity_ok:
        return "valid"
    return "invalid"


def _role_or_prediction(has_prediction_check: bool, role: object) -> object:
    if has_prediction_check:
        return "prediction"
    return role


def _lit_own(lit: object, criteria: object) -> bool:
    if not lit:
        return False
    if not criteria:
        return False
    return True


def _outcome_from_payload(payload: Mapping[str, Any]) -> Outcome | None:
    ac = optional_item(payload, "agent_claim")
    if ac is None:
        return None
    return Outcome(ac)


def _or_empty_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return value


def _or_zero_int(value: object) -> int:
    if value is None:
        return 0
    if any((type(value) is bool, not isinstance(value, int))):
        raise TypeError(f"expected int|None, got {type(value).__name__}")
    return value  # type: ignore[return-value]


_EMPTY_TUPLE = ()


def _or_empty_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"expected list|tuple|None, got {type(value).__name__}")


def _or_empty_dict(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected dict|None, got {type(value).__name__}")


def _or_empty_tuple(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    return tuple(value)  # type: ignore[arg-type]


def _attempt_get(attempt: Any, name: str) -> Any:
    """Optional attempt flag: missing → None (only explicit True counts for taint)."""
    if isinstance(attempt, dict):
        # caller uses `is True`
        return optional_item(attempt, name)
    try:
        return getattr(attempt, name)
    except AttributeError:
        return None  # audit-ok[except_silent_return]: executor.py:177 except_silent_return


def _attempt_req(attempt: Any, name: str) -> Any:
    if isinstance(attempt, dict):
        return attempt[name]
    return getattr(attempt, name)


log = logging.getLogger(__name__)

def _provenance(attempt: Any) -> Provenance:
    p = attempt.provenance
    if p is None and isinstance(attempt, dict):
        p = optional_item(attempt, "provenance")
    if p not in VALID_PROVENANCE:
        raise ValueError(f"invalid attempt provenance: {p!r}")
    return p  # type: ignore[return-value]

def _req_to_criterion_status(status: ReqStatus) -> str:
    if status in (ReqStatus.MET, ReqStatus.WAIVED, ReqStatus.NOT_APPLICABLE):
        return "met"
    if status in (ReqStatus.FAILED, ReqStatus.MIGRATION_ERROR):
        return "not_met"
    return "unassessed"

def _check_field(c: Any, name: str) -> Any:
    if isinstance(c, dict):
        return optional_item(c, name)
    try:
        return getattr(c, name)
    except AttributeError:
        return None  # audit-ok[except_silent_return]: executor.py:209 except_silent_return

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
    checked = optional_item(raw, "checked_against")
    if checked:
        return _normalize_to_template_id(str(checked), checks, criteria)
    # No sole-check / sole-criterion inference (REVIEW_CHECKLIST rule 9).
    return None

def _criterion_status_for_claim(raw: dict[str, Any], checks: list[Any], criteria: list[Any] | None = None) -> str:
    """Map claim.checked_against → ReqStatus of that criterion's check."""
    crit = _or_empty_list(criteria)
    key = _criterion_key_for_claim(raw, checks, crit)
    if key is None:
        # No checked_against and no sole-criterion inference (rule 9).
        return "unassessed"
    for c in checks:
        tid = _check_field(c, "template_id")
        cid = _check_field(c, "check_id")
        st = _check_field(c, "status")
        # Exact match only (no endswith — avoids C11 matching ``1``).
        if any((tid is not None and str(tid) == key, cid is not None and str(cid) == key)):
            if isinstance(st, ReqStatus):
                return _req_to_criterion_status(st)
            try:
                return _req_to_criterion_status(ReqStatus(str(st)))
            except ValueError as e:
                raise JournalFormatError(f"bad criterion status: {st!r}") from e
    # Bound criterion not among evaluated checks → still unassessed (not invented met).
    return "unassessed"

def _check_role(c: Any) -> str:
    role = _check_field(c, "role")
    if role in ("prediction", "validity", "unknown"):
        return str(role)
    if role is None:
        raise JournalFormatError("check missing role")
    raise JournalFormatError(f"check role invalid: {role!r}")

def _is_rubric_check(c: Any) -> bool:
    """Rubric checks are validity-role but must not flip claim validity_ok (PR13)."""
    effect = _check_field(c, "effect")
    if isinstance(effect, dict) and optional_item(effect, "rubric_kind") == "rubric":
        return True
    # No template_id/check_id prefix heuristic (rule 6) — structural effect only.
    return False

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
        if _check_field(c, "required") is not True:
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
    for c in _or_empty_tuple(checks):
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
        detail = _or_empty_str(_check_field(c, 'detail'))
        mapped = optional_item(_NO_EFFECT_DETAIL_ALIASES, detail)
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
            if any((tid is not None and str(tid) == key, cid is not None and str(cid) == key)):
                return _check_role(c)
        # Fall back to catalog by template_id (never plan RequirementRef.role).
        return role_from_catalog(str(key), catalog)
    return "unknown"

def _criterion_node(graph: Any, study_id: str, template_id: Any) -> Any | None:
    if any((not template_id, graph is None)):
        return None
    key = str(template_id)
    node = graph.get_node(criterion_node_id(study_id, key))
    if node is None:
        node = graph.get_node(key)
    if (node is None) or (node.kind != 'criterion'):
        return None
    return node

def _criterion_is_frozen(graph: Any, study_id: str, template_id: Any) -> bool:
    """True when plan-accept freeze marked the criterion node (keyed by template_id)."""
    node = _criterion_node(graph, study_id, template_id)
    if node is None:
        return False
    attrs = node.attrs
    if attrs is None:
        attrs = {}
    return optional_item(attrs, "frozen_at_seq") is not None or optional_item(attrs, "frozen") is True

def _criterion_freeze_attrs(graph: Any, study_id: str, template_id: Any) -> dict[str, Any]:
    """sesoi / predicts_null / uses_sesoi from the frozen criterion (catalog)."""
    node = _criterion_node(graph, study_id, template_id)
    if node is None:
        attrs = {}
    else:
        attrs = _or_empty_dict(node.attrs)
    out: dict[str, Any]
    out = {}
    if optional_item(attrs, "sesoi") is not None:
        try:
            out["sesoi"] = attrs["sesoi"]
        except (TypeError, ValueError) as e:
            raise JournalFormatError(f"criterion sesoi invalid: {attrs['sesoi']!r}") from e
    if optional_item(attrs, "alpha") is not None:
        try:
            out["alpha"] = attrs["alpha"]
        except (TypeError, ValueError) as e:
            raise JournalFormatError(f"criterion alpha invalid: {attrs['alpha']!r}") from e
    if "predicts_null" in attrs:
        out["predicts_null"] = optional_item(attrs, "predicts_null") is True
    if "uses_sesoi" in attrs:
        out["uses_sesoi"] = optional_item(attrs, "uses_sesoi") is True
    if optional_item(attrs, "predicted_direction") in ("positive", "negative", "two_sided"):
        out["predicted_direction"] = _or_empty_str(attrs["predicted_direction"])
    return out

def _effect_from_checks(
    checks: list[Any],
    *,
    attempt_id: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Core-computed effect from CheckResult.effect (never agent detail / claims)."""
    from coscientist.core.claims.verdict import recompute_effect_provenance

    for c in _or_empty_tuple(checks):
        if _check_role(c) != "prediction":
            continue
        data = _check_field(c, "effect")
        if not isinstance(data, dict):
            continue
        try:
            kind_raw = optional_item(data, "kind")
            producer_raw = optional_item(data, "producer")
            if any((not isinstance(kind_raw, str), not kind_raw)):
                raise ValueError("effect.kind required")
            if any((not isinstance(producer_raw, str), not producer_raw)):
                raise ValueError("effect.producer required")
            effect: dict[str, Any] = {
                "est": data["est"] if optional_item(data, "est") is not None else None,
                "kind": kind_raw,
                "producer": producer_raw,
                "core_evaluated": optional_item(data, "core_evaluated") is True,
                "produced_by_call_id": _or_empty_str(optional_item(data, "produced_by_call_id")),
                "artifact_id": _or_empty_str(optional_item(data, "artifact_id")),
            }
            if optional_item(data, "ci_low") is not None and optional_item(data, "ci_high") is not None:
                effect["ci_low"] = data["ci_low"]
                effect["ci_high"] = data["ci_high"]
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
            continue  # audit-ok[except_silent_continue]: executor.py:458 except_silent_continue
        used = _check_field(c, "used_sesoi") is True
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
    spec = task.spec
    if spec is not None:
        method = spec.method
    else:
        method = None
    lit = method == ExperimentMethod.LITERATURE_SEARCH or _or_empty_str(method) == ("literature_search")
    criteria = _or_empty_list(_spec_criteria(spec))
    key = _criterion_key_for_claim(raw, checks, criteria)
    role = _role_for_claim_key(raw, checks, criteria, catalog=catalog)
    status = _criterion_status_for_claim(raw, checks, criteria)
    validity_ok = True
    exclusion_reason: str | None = None
    # PR20: unapproved proposed_check never counts (even if evaluation ran).
    for c in _or_empty_tuple(checks):
        eff = c.effect
        if isinstance(eff, dict) and optional_item(eff, "exclusion_reason") == "unapproved_check":
            exclusion_reason = "unapproved_check"
            validity_ok = False
            status = CriterionStatus.UNASSESSED.value
            break
    # Required validity failure invalidates this experiment's claims only.
    # Rubric (even role=validity) never flips validity_ok — see _is_rubric_check.
    if exclusion_reason == "unapproved_check":
        pass
    elif _required_validity_failed(checks):
        validity_ok = False
        status = CriterionStatus.UNASSESSED.value
        exclusion_reason = "validity_failed"
    elif role == "unknown":
        # Missing/unresolved checked_against: prefer a concrete prediction
        # no-effect reason (foreign_artifact / output_not_submitted / …) over
        # a blanket unknown_role — never invent a criterion via len==1 (rule 9).
        validity_ok = False
        status = CriterionStatus.UNASSESSED.value
        specific = _no_effect_reason_from_checks(list(_or_empty_tuple(checks)))
        if specific is not None:
            exclusion_reason = specific
        else:
            exclusion_reason = "unknown_role"
    elif (
        role == "validity"
        and status == CriterionStatus.NOT_MET.value
        and not any(_is_rubric_check(c) for c in (_or_empty_tuple(checks)))
    ):
        validity_ok = False
        status = CriterionStatus.UNASSESSED.value
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
    sesoi = optional_item(crit, "sesoi")
    if frozen:
        for req_k in ("uses_sesoi", "predicts_null", "predicted_direction"):
            if req_k not in crit:
                raise JournalFormatError(
                    f"frozen criterion missing required attr: {req_k}"
                )
        uses_sesoi = crit["uses_sesoi"] is True
        predicts_null = crit["predicts_null"] is True
        predicted_direction = _or_empty_str(crit["predicted_direction"])
    else:
        if "uses_sesoi" in crit:
            uses_sesoi = crit["uses_sesoi"] is True
        else:
            uses_sesoi = False
        if "predicts_null" in crit:
            predicts_null = crit["predicts_null"] is True
        else:
            predicts_null = False
        if "predicted_direction" in crit:
            predicted_direction = _or_empty_str(crit["predicted_direction"])
        else:
            predicted_direction = "two_sided"
    if predicted_direction not in ("positive", "negative", "two_sided"):
        raise JournalFormatError(
            f"criterion predicted_direction invalid: {predicted_direction!r}"
        )
    pred_statuses: list[str]
    pred_statuses = []
    has_prediction_check = False
    for c in _or_empty_tuple(checks):
        if _check_role(c) == "prediction":
            has_prediction_check = True
            try:
                st = c.status.value
            except AttributeError:
                st = c.status
            if st is None:
                raise JournalFormatError("prediction check missing status")
            pred_statuses.append(_or_empty_str(st))
    if not pred_statuses and status in ("met", "not_met", "unassessed"):
        # Never invent empty-string status for unassessed (filters to no preds).
        if status == CriterionStatus.UNASSESSED.value:
            pred_statuses = ["UNASSESSED"]
        elif status == CriterionStatus.MET.value:
            pred_statuses = ["PASSED"]
        else:
            pred_statuses = ["FAILED"]
    # Effect from CheckResult.effect only — never agent detail / claim attrs.
    effect, used_sesoi = _effect_from_checks(list(_or_empty_list(checks)), attempt_id=attempt_id)
    from coscientist.checks.kinds._util import check_is_guest_timeout

    timeout_kind: str | None = None
    refute_unresolved = False
    timeout_checks = [c for c in (_or_empty_tuple(checks)) if check_is_guest_timeout(c)]
    claim_outcome: str
    validity_s: str
    if timeout_checks:
        # Guest timeout is never a counting outcome (not k_ref / not k_sup).
        for tc in timeout_checks:
            te = tc.effect
            if te is None:
                te = {}
            if isinstance(te, dict) and optional_item(te, "timeout_kind"):
                timeout_kind = _or_empty_str(te["timeout_kind"])
            if isinstance(te, dict) and optional_item(te, "refute_unresolved"):
                refute_unresolved = True
            elif _check_role(tc) == "prediction":
                refute_unresolved = True
        claim_outcome = "indeterminate"
        if validity_ok:
            validity_s = "valid"
        else:
            validity_s = "invalid"
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
        validity_s, raw_outcome, _ = classify_outcome(
            validity_ok=validity_ok,
            prediction_statuses=pred_statuses,
            predicts_null=predicts_null,
            sesoi=_as_float_or_none(sesoi),
            effect=effect,
            r17_ok=True,  # R17 runtime metadata still deferred (PR8)
        )
        # Always stamp a concrete claim_outcome — never omit / never rely on
        # inputs.py criterion_status→outcome map. Invalid → indeterminate.
        if raw_outcome in ("consistent", "inconsistent", "indeterminate"):
            claim_outcome = raw_outcome
        else:
            claim_outcome = "indeterminate"
    if validity_s == "invalid":
        validity_ok = False
        if exclusion_reason is None:
            exclusion_reason = "validity_failed"
    elif claim_outcome == "indeterminate" and exclusion_reason is None:
        # Prefer specific no-effect reason over generic indeterminate.
        exclusion_reason = _no_effect_reason_from_checks(list(_or_empty_list(checks)))
        if exclusion_reason is None:
            exclusion_reason = 'indeterminate'
    status = outcome_to_status(
        claim_outcome,  # type: ignore[arg-type]
        validity=_valid_label(validity_ok),
    )
    exp_id = ""
    try:
        exp_id = _or_empty_str(task.task_id)
    except Exception:  # noqa: BLE001  # audit-ok[broad_except]: executor.py:660 broad_except
        exp_id = ""  # audit-ok[except_enum_default]: executor.py:661 except_enum_default
    lookup = None
    arts = artifact_store
    if arts is not None:
        try:
            lookup = arts.get_meta
        except AttributeError:
            lookup = None
    data_digests, indep_notes = root_data_digests_from_bindings(
        bindings,
        artifact_lookup=lookup,
        results=results_map,
    )
    # Plan order (deduped); shared-any structural merge uses the set.
    # Empty plan criteria → catalog.defaults["experiment"] (same as evaluate_task_checks).
    criterion_template_ids: list[str]
    criterion_template_ids = []
    seen_tmpl: set[str] = set()
    for r in criteria:
        if isinstance(r, str):
            tid = _or_empty_str(r)
        else:
            try:
                tid = _or_empty_str(r.template_id)
            except AttributeError:
                tid = _or_empty_str(r)
        if tid and tid not in seen_tmpl:
            seen_tmpl.add(tid)
            criterion_template_ids.append(tid)
    if not criterion_template_ids and catalog is not None:
        defaults = catalog.defaults
        if isinstance(defaults, dict):
            exp_defaults = optional_item(defaults, "experiment")
        else:
            exp_defaults = None
        for tid in _or_empty_list(exp_defaults):
            stid = _or_empty_str(tid)
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
            "sesoi": _as_float_or_none(sesoi),
            "uses_sesoi": uses_sesoi,
            "used_sesoi": used_sesoi,
            "predicts_null": predicts_null,
            "predicted_direction": predicted_direction,
            "effect": effect,
            "severe": None,  # never pre-trust
            "refute_capable": None,
            "intent": "refute",  # probe capability gate only
            "check_role": (
                _role_or_prediction(has_prediction_check, role)
            ),
            "criterion_frozen_at_seq": None,
            "first_cited_evidence_seq": None,
        },
    )()
    severe = claim_is_severe(probe)
    # Capable refute: recompute — never trust a stored True later on replay.
    # Countable core-evaluated evidence required (trusted is a separate tag).
    refute_capable = (
        frozen
        and has_prediction_check
        and uses_sesoi
        and isinstance(sesoi, float)
        and used_sesoi
        and effect_is_countable(effect)
        and (effect_has_interval(effect) if predicts_null else True)  # audit-ok[ternary_name]: e736
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
    der_exec = (
        ek == EVIDENCE_KIND_DERIVATION
        and executable_check_met(list(_or_empty_list(checks)), bound_key=key)
    )
    out: dict[str, Any] = {
        "criterion_status": status,
        "evidence_kind": ek,
        "frozen": frozen,
        "lit_own_criterion": _lit_own(lit, criteria),
        "accepted": validated is True,
        "validity_ok": validity_ok,
        "validity": validity_s,
        "check_role": role,
        # Always concrete — inputs must never invent from criterion_status.
        "claim_outcome": claim_outcome,
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
    if indep_notes:
        out["independence_notes"] = list(indep_notes)
    if isinstance(sesoi, float):
        out["sesoi"] = sesoi
    if isinstance(optional_item(crit, "alpha"), float):
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

class Executor:
    def __init__(
        self,
        writer: StudyWriter,
        catalog: Catalog,
        artifacts: LocalArtifactStore,
        *,
        clock: Any,
        get_attempt: Any,
        get_task: Any,
        on_events: Any | None = None,
        review_statuses: dict[str, ReqStatus] | None = None,
        get_graph: Any | None = None,
        get_results: Any | None = None,
        get_artifact_kinds: Any | None = None,
        get_proposals: Any | None = None,
        max_attempts: int,
        on_budget_debit: Any | None = None,
    ) -> None:
        self.writer = writer
        self.catalog = catalog
        self.artifacts = artifacts
        self.clock = clock
        self.get_attempt = get_attempt
        self.get_task = get_task
        self.on_events = on_events  # callback after successful append (project local)
        if review_statuses is None:
            review_statuses = {}
        self.review_statuses: dict[str, ReqStatus]
        self.review_statuses = review_statuses
        # PR13: journaled judge state (rebuilt via apply_judge_journal on reopen).
        self.judge_retry: dict[str, int]
        self.judge_retry = {}
        self.failure_kinds: dict[str, str]
        self.failure_kinds = {}
        self._pending_outputs: dict[str, AgentTaskOutput]
        self._pending_outputs = {}
        self._output_digests: dict[str, str]
        self._output_digests = {}
        self._runtime_attempts: dict[str, Any] | None = None
        self.get_graph = get_graph
        self.get_results = get_results
        # PR15: folded ArtifactRegistered(stamped) kinds (never trust meta alone).
        self.get_artifact_kinds = get_artifact_kinds
        # PR20: folded CheckProposed/Approved/Rejected → evaluate proposed_check.
        self.get_proposals = get_proposals
        self.max_attempts = max_attempts
        # PR12: async (task_id, spent_rub, request_id) → record_budget_debit
        self.on_budget_debit = on_budget_debit
        self._scheduled_reviews: list[str]
        self._scheduled_reviews = []

    def _ensure_output_digest(self, attempt_id: str, out: AgentTaskOutput) -> str:
        """CAS-store agent output; return digest used in scoped review keys."""
        dig: str | None = optional_item(self._output_digests, attempt_id)
        if dig:
            return dig
        attempt = self.get_attempt(attempt_id)
        if attempt is not None:
            dig_raw = _attempt_get(attempt, "agent_output_digest")
            if dig_raw:
                dig = _or_empty_str(dig_raw)
        if not dig:
            content = canonical_json(out.model_dump(mode="json"))
            dig = self.artifacts.put_cas(content, tmp_name=f"pending:{attempt_id}")
        self._output_digests[attempt_id] = dig
        return dig

    async def mark_route_returned(self, attempt_id: str, out: AgentTaskOutput) -> str:
        """Persist agent output in CAS and emit RouteReturned with its digest."""
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = _attempt_get(attempt, "closed")
        study_id = _attempt_req(attempt, "study_id")
        if closed:
            raise AttemptClosedError(attempt_id)
        content = canonical_json(out.model_dump(mode="json"))
        digest = self.artifacts.put_cas(content, tmp_name=f"route:{attempt_id}")
        await self._append(
            [
                UnstampedEvent(
                    study_id=study_id,
                    type="RouteReturned",
                    entity_key=attempt_id,
                    idem_key=make_idem("RouteReturned", attempt_id),
                    payload={
                        "attempt_id": attempt_id,
                        "agent_output_digest": digest,
                    },
                    ts=self.clock.now(),
                    schema_version=ROUTE_RETURNED_SCHEMA_VERSION,
                )
            ],
            require_open_attempt=attempt_id,
        )
        return digest

    async def record(self, attempt_id: str, out: AgentTaskOutput) -> Result:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = _attempt_get(attempt, "closed")
        study_id = _attempt_req(attempt, "study_id")
        task_id = _attempt_req(attempt, "task_id")

        if closed:
            await self._append(
                [
                    UnstampedEvent(
                        study_id=study_id,
                        type="LateWriteRejected",
                        entity_key=attempt_id,
                        idem_key=make_idem("LateWriteRejected", attempt_id, str(self.clock.now())),
                        payload={"attempt_id": attempt_id, "reason": "attempt_closed"},
                        ts=self.clock.now(),
                        schema_version=LATE_WRITE_REJECTED_SCHEMA_VERSION,
                    )
                ]
            )
            raise AttemptClosedError(attempt_id)

        task = self.get_task(task_id)
        # PR18: per-attempt ContextVar taint (not shared store state — foreach-safe).
        # PR19: attempt frame (private) blocks put_user mid-attempt.
        from coscientist.core.execution._attempt_frame import (
            push_attempt_active,
            reset_attempt_active,
        )
        from coscientist.core.execution.taint import (
            push_attempt_taint,
            reset_attempt_taint,
        )

        _tainted = _attempt_get(attempt, "tainted_context") is True
        _taint_token = push_attempt_taint(_tainted)
        _active_token = push_attempt_active(True)
        try:
            try:
                # MJ2: no sync Popen on the event loop — evaluate (sandbox) off-thread.
                # to_thread on 3.12+ copies ContextVar so sandbox puts see taint.
                from coscientist.core.execution.source_fetch import (
                    SourceFetchIndex,
                    load_source_fetch_index_from_writer,
                    task_needs_source_fetch,
                )

                # Skip journal fold unless quote_in_source is on this task
                # (read_all during write bursts blew the p99 SLO).
                if task_needs_source_fetch(self.catalog, task.spec):
                    fetch_idx = await load_source_fetch_index_from_writer(self.writer)
                else:
                    fetch_idx = SourceFetchIndex()
                self._pending_outputs[attempt_id] = out
                output_digest = self._ensure_output_digest(attempt_id, out)
                if self.get_proposals is not None:
                    proposals = self.get_proposals()
                else:
                    proposals = {}
                # Explicit attempts/results maps for fail-closed PR20 prereg
                # (no monkey-attr / proposals["__attempts__"] indirection).
                attempts_bag: dict[str, Any] | None = None
                host = self._runtime_attempts
                if isinstance(host, dict):
                    attempts_bag = host
                if self.get_results is not None:
                    results_bag = self.get_results()
                else:
                    results_bag = None
                checks = await asyncio.to_thread(
                    evaluate_task_checks,
                    self.catalog,
                    task.spec,
                    out,
                    self.artifacts,
                    review_statuses=self.review_statuses,
                    judge_retry=self.judge_retry,
                    failure_kinds=self.failure_kinds,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    output_digest=output_digest,
                    task_kind=task.kind,
                    task_key=task.spec.task_key,
                    fetched_digests=fetch_idx.digests_for_attempt(attempt_id),
                    proposals=(
                        (proposals if isinstance(proposals, dict) else {})  # audit-ok[ternary_name]: e981
                    ),
                    attempts=attempts_bag,
                    results_by_id=_as_dict_or_none(results_bag),
                )
            except SandboxInfraError as e:
                # Host/private-FD infra — does NOT charge the retry budget.
                # Permanent unavailability closes the task (allow_retry=False); the
                # attempt stays charged=False so a later reopen/retry budget is intact.
                # Transient attach failures may retry without consuming charged slots.
                detail = e.reason
                transient = e.transient is True
                provenance = _provenance(self.get_attempt(attempt_id))
                return await self._commit_failure(  # audit-ok[except_silent_return]: e994
                    attempt_id,
                    study_id,
                    task_id,
                    out,
                    provenance,
                    out.claim,
                    reason=TaskReason.SANDBOX_INFRA.value,
                    detail=detail,
                    event_type="SandboxInfraRaised",
                    result_class="infra",
                    charge_attempt=False,
                    allow_retry=transient,
                )
            except CoreBugError as e:
                # Own result_class (not infra); reported; never counts.
                provenance = _provenance(self.get_attempt(attempt_id))
                return await self._commit_failure(  # audit-ok[except_silent_return]: e1011
                    attempt_id,
                    study_id,
                    task_id,
                    out,
                    provenance,
                    out.claim,
                    reason=TaskReason.CORE_BUG.value,
                    detail=e.reason,
                    event_type="CatalogErrorRaised",
                    result_class="core_bug",
                    charge_attempt=False,
                    allow_retry=False,
                )
        finally:
            reset_attempt_active(_active_token)
            reset_attempt_taint(_taint_token)
        for c in checks:
            if not c.is_code and c.status == ReqStatus.UNASSESSED:
                self._scheduled_reviews.append(c.check_id)

        from coscientist.checks.kinds._util import check_is_guest_timeout, merge_detail

        timeout_checks = [c for c in checks if check_is_guest_timeout(c)]
        # Digest over content actually mounted into the jail (not artifact ids).
        jail_digests = [
            _or_empty_str(optional_item(_or_empty_dict(c.effect), 'jail_inputs_digest'))
            for c in checks
            if isinstance(c.effect, dict) and optional_item(c.effect, "jail_inputs_digest")
        ]
        if jail_digests:
            agent_digest = sha256_hex(canonical_json({"jail_inputs": sorted(set(jail_digests))}))
        else:
            # Fallback: content hashes of declared outputs (same as mount inputs).
            mounted: dict[str, str]
            mounted = {}
            for name, handle in sorted(_or_empty_dict(out.outputs).items()):
                meta = self.artifacts.get_meta(handle.artifact_id)
                if meta is None:
                    continue
                mounted[_or_empty_str(name)] = _or_empty_str(meta.sha256)
            agent_digest = sha256_hex(canonical_json({"mounted": mounted}))
        if self.get_results is not None:
            results_map = self.get_results()
        else:
            results_map = {}
        for prev_aid in _or_empty_list(task.attempts):
            if prev_aid == attempt_id:
                continue
            if isinstance(results_map, dict):
                prev = optional_item(results_map, prev_aid)
            else:
                prev = None
            if not isinstance(prev, dict):
                continue
            if _or_empty_str(optional_item(prev, 'result_class')) != "timeout":
                continue
            prev_dig = optional_item(prev, "agent_output_digest")
            if prev_dig and agent_digest and prev_dig == agent_digest:
                provenance = _provenance(self.get_attempt(attempt_id))
                return await self._commit_failure(
                    attempt_id,
                    study_id,
                    task_id,
                    out,
                    provenance,
                    out.claim,
                    reason=TaskReason.SAME_DIGEST_RETRY.value,
                    detail=f"same_digest_retry:{agent_digest}",
                    event_type="SameDigestRetryRejected",
                    result_class="timeout",
                    charge_attempt=True,
                    allow_retry=True,
                )

        if timeout_checks:
            provenance = _provenance(self.get_attempt(attempt_id))
            kinds = [
                _or_empty_str(optional_item(_or_empty_dict(c.effect), "timeout_kind"))
                for c in timeout_checks
                if isinstance(c.effect, dict)
            ]

            # Structural: ≥2 prediction/refute-role timeouts on this TASK
            # (any template_id — alternation does not evade) → untestable.
            # A single timeout (even on the last charged attempt) stays a
            # plain timeout; refute_unresolved already blocks supported.
            def _prediction_timeout_in_row(row: dict[str, Any]) -> bool:
                if _or_empty_str(optional_item(row, 'result_class')) != "timeout":
                    return False
                for cr in _or_empty_list(optional_item(row, "check_results")):
                    if not isinstance(cr, dict):
                        continue
                    if _or_empty_str(optional_item(cr, 'role')) != "prediction":
                        continue
                    raw_eff = optional_item(cr, "effect")
                    if isinstance(raw_eff, dict):
                        eff = dict(raw_eff)
                    else:
                        eff = {}
                    if any((optional_item(eff, 'timeout_kind'), optional_item(eff, 'refute_unresolved'))):
                        return True
                return False

            current_prediction_timeout = any(c.role == "prediction" for c in timeout_checks)
            prior_prediction_timeouts = 0
            for prev_aid in _or_empty_list(task.attempts):
                if prev_aid == attempt_id:
                    continue
                if isinstance(results_map, dict):
                    prev = optional_item(results_map, prev_aid)
                else:
                    prev = None
                if isinstance(prev, dict) and _prediction_timeout_in_row(prev):
                    prior_prediction_timeouts += 1
            if current_prediction_timeout:
                task_prediction_timeouts = prior_prediction_timeouts + 1
            else:
                task_prediction_timeouts = prior_prediction_timeouts
            repeat_refute_timeout = task_prediction_timeouts >= 2
            idle = any((any(k == "idle_timeout" for k in kinds), repeat_refute_timeout))
            if repeat_refute_timeout:
                elevated: list[Any]
                elevated = []
                for c in checks:
                    if (
                        check_is_guest_timeout(c)
                        and c.role == "prediction"
                        and isinstance(c.effect, dict)
                    ):
                        elevated.append(
                            c.model_copy(
                                update={
                                    "effect": {
                                        **c.effect,
                                        "timeout_kind": "idle_timeout",
                                        "refute_unresolved": True,
                                        "repeat_timeout": True,
                                    },
                                    "detail": merge_detail(
                                        c.detail,
                                        "repeat_timeout",
                                    ),
                                }
                            )
                        )
                    else:
                        elevated.append(c)
                checks = elevated
                kinds = ["idle_timeout"]
            if idle:
                timeout_reason = TaskReason.IDLE_TIMEOUT.value
            else:
                timeout_reason = TaskReason.SANDBOX_TIMEOUT.value
            return await self._commit_timeout(
                attempt_id,
                study_id,
                task_id,
                out,
                provenance,
                checks=checks,
                reason=timeout_reason,
                detail=_join_or(kinds, "timeout"),
                agent_output_digest=_or_empty_str(agent_digest),
            )

        claim = out.claim
        reason: str | None = None
        if out.downgrade is not None:
            if any((not out.downgrade.refs, not out.downgrade.reason)):
                await self._append(
                    [
                        UnstampedEvent(
                            study_id=study_id,
                            type="DowngradeRejected",
                            entity_key=attempt_id,
                            idem_key=make_idem("DowngradeRejected", attempt_id),
                            payload={
                                "attempt_id": attempt_id,
                                "reason": "downgrade requires reason and refs",
                            },
                            ts=self.clock.now(),
                            schema_version=DOWNGRADE_REJECTED_SCHEMA_VERSION,
                        )
                    ]
                )
            else:
                claim = out.downgrade.to
                reason = out.downgrade.reason

        provenance = _provenance(attempt)
        try:
            computed, downgrade = compute_outcome(checks, claim, reason, provenance)
        except CatalogError as e:
            return await self._commit_failure(  # audit-ok[except_silent_return]: executor.py:1205 except_silent_return
                attempt_id,
                study_id,
                task_id,
                out,
                provenance,
                claim,
                reason=TaskReason.CATALOG_ERROR.value,
                detail=str(e),
                event_type="CatalogErrorRaised",
                result_class="infra",
                charge_attempt=True,
                allow_retry=False,
            )

        if computed is None:
            from coscientist.core.claims.reviews import review_scheduled_events

            output_digest = self._ensure_output_digest(attempt_id, out)
            # Idem must change as judge answers arrive (clock may be frozen in tests).
            status_fp = ",".join(f"{c.check_id}={c.status.value}" for c in sorted(checks, key=lambda x: x.check_id))
            rev_fp = ",".join(f"{k}={v.value}" for k, v in sorted(self.review_statuses.items(), key=lambda kv: kv[0]))
            retry_fp = ",".join(f"{k}={v}" for k, v in sorted(self.judge_retry.items()))
            pending_events = [
                UnstampedEvent(
                    study_id=study_id,
                    type="ResultPending",
                    entity_key=attempt_id,
                    idem_key=make_idem("ResultPending", attempt_id, status_fp, rev_fp, retry_fp),
                    payload={
                        "attempt_id": attempt_id,
                        "task_id": task_id,
                        "agent_output_digest": output_digest,
                        "reason": "required_checks_pending",
                        "check_results": [c.model_dump(mode="json") for c in checks],
                    },
                    ts=self.clock.now(),
                    schema_version=RESULT_PENDING_SCHEMA_VERSION,
                )
            ]
            pending_events.extend(
                review_scheduled_events(
                    study_id=study_id,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    output_digest=output_digest,
                    checks=checks,
                    now=self.clock.now(),
                )
            )
            await self._append(pending_events)
            # Attempt stays open; caller finishes via record_review.
            return Result(
                result_id=result_id(attempt_id),
                attempt_id=attempt_id,
                outcome=Outcome.PARTIAL,
                provenance=_provenance(self.get_attempt(attempt_id)),
                computed_outcome=Outcome.PARTIAL,
                check_results=checks,
                unassessed=[c.check_id for c in checks if c.status in (ReqStatus.PENDING, ReqStatus.UNASSESSED)],
                payload_digest="",
                reason="required_checks_pending",
            )

        from coscientist.core.claims.reviews import review_scheduled_events

        sched = review_scheduled_events(
            study_id=study_id,
            attempt_id=attempt_id,
            task_id=task_id,
            output_digest=self._ensure_output_digest(attempt_id, out),
            checks=checks,
            now=self.clock.now(),
        )
        if sched:
            await self._append(sched)

        unassessed = [c.check_id for c in checks if c.status == ReqStatus.UNASSESSED]
        rid = result_id(attempt_id)
        if downgrade:
            final = downgrade.to
        else:
            final = computed
        from coscientist.core.claims.verdict import output_kinds_from_catalog

        # Code|data from catalog code_output_names (ignore planner OutputSpec.role).
        out_names = list(_or_empty_dict(out.outputs).keys())
        for ospec in task.spec.expected_outputs:
            n = _or_empty_str(ospec.name)
            if n and n not in out_names:
                out_names.append(n)
        output_kinds = output_kinds_from_catalog(output_names=out_names, catalog=self.catalog)
        payload = {
            "result_id": rid,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "outcome": final.value,
            "computed_outcome": computed.value,
            "reason": None,
            "provenance": provenance,
            "check_results": [c.model_dump(mode="json") for c in checks],
            "unassessed": unassessed,
            "artifacts": [h.artifact_id for h in out.outputs.values()],
            # name → artifact_id for InputSpec.bind / independence_key digests.
            "output_map": {name: h.artifact_id for name, h in _or_empty_dict(out.outputs).items()},
            "output_kinds": output_kinds,
            "agent_claim": _maybe_value(claim),
            "downgrade": _maybe_dump(downgrade, mode="json", by_alias=True),
            "inputs": [],
            # PR9 H-g: typed expansion request (applied via PLAN-n → critic).
            "expansion_request": (
                _maybe_dump(out.expansion_request, mode="json")
            ),
        }
        # Scientific result: criteria evaluated (SUCCESS / PARTIAL / FAILED).
        # Keep claims so not_met can refute (invariant 10); only infra drops them.
        payload["result_class"] = "scientific"
        payload["result_content_digest"] = payload_digest(payload)
        # PR15: journal kind stamps + QuestionDisposed in the same append as result.
        # Meta.kind is applied only in fold after a successful append (never pre-commit).
        from pydantic import ValidationError

        from coscientist.core.claims.dispose import (
            DisposeRejected,
            QuestionDisposal,
            question_dispose_rejected_event,
            question_disposed_event,
            validate_disposal,
        )
        from coscientist.core.claims.experiment_design import artifact_kind_from_output_spec
        from coscientist.core.events.errors import ArtifactRejected
        from coscientist.core.execution.artifact_stamp import kind_stamped_event

        out_map = dict(_or_empty_dict(out.outputs))
        extra_events: list[UnstampedEvent]
        extra_events = []
        now_stamp = self.clock.now()
        pending_design_ids: set[str] = set()
        for ospec in task.spec.expected_outputs:
            kind = artifact_kind_from_output_spec(ospec)
            if not kind:
                continue
            name = _or_empty_str(ospec.name)
            if name not in out_map:
                raise ArtifactRejected(
                    f"OutputSpec.kind={kind!r} but output {name!r} missing"
                )
            handle = out_map[name]
            meta = self.artifacts.get_meta(handle.artifact_id)
            if meta is None:
                raise ArtifactRejected(
                    f"artifact missing for kind stamp: {handle.artifact_id!r}"
                )
            if kind == "experiment_design":
                pending_design_ids.add(handle.artifact_id)
            # Journal only — fold applies meta via apply_kind_stamp after append.
            extra_events.append(
                kind_stamped_event(
                    study_id=study_id,
                    artifact_id=handle.artifact_id,
                    kind=kind,  # type: ignore[arg-type]
                    now=now_stamp,
                    name=meta.name,
                    sha256=meta.sha256,
                    role=meta.role,
                )
            )
        for raw in _or_empty_list(out.question_disposals):
            qid_hint: str | None = None
            disp_hint: str | None = None
            try:
                if isinstance(raw, dict):
                    qid_hint = _or_empty_str(optional_item(raw, 'question_id')) or None
                    disp_hint = _or_empty_str(optional_item(raw, 'disposition')) or None
                disposal = (
                    raw  # audit-ok[ternary_name]: executor.py:1380 ternary_name
                    if isinstance(raw, QuestionDisposal)
                    else QuestionDisposal.model_validate(raw)
                )
                qid_hint = disposal.question_id
                disp_hint = disposal.disposition
                if (
                    disposal.disposition == "needs_more_data"
                    and not disposal.design_artifact_id
                    and out_map
                ):
                    for ospec in task.spec.expected_outputs:
                        if artifact_kind_from_output_spec(ospec) == "experiment_design":
                            oname = _or_empty_str(ospec.name)
                            if oname in out_map:
                                disposal = disposal.model_copy(
                                    update={"design_artifact_id": out_map[oname].artifact_id}
                                )
                                break
                # Prefer live graph; else task addresses — never invent from disposal id.
                if self.get_graph is not None:
                    g = self.get_graph()
                else:
                    g = None
                known: set[str] = set()
                if g is not None:
                    for n in g.nodes_by_kind("question"):
                        if n.study_id == study_id:
                            if n.ref_id:
                                known.add(n.ref_id)
                            else:
                                known.add(n.node_id.split(":")[-1])  # audit-ok[str_split]: e1408
                if not known:
                    known = {_or_empty_str(a) for a in task.spec.addresses if a}
                kinds_fn = self.get_artifact_kinds
                if callable(kinds_fn):
                    rt_kinds = kinds_fn()
                else:
                    rt_kinds = None
                validate_disposal(
                    disposal,
                    known_question_ids=known,
                    covering_task_keys={task.spec.task_key},
                    tasks={task_id: task},
                    results=_maybe_call(self.get_results),
                    journal_kinds=_as_dict_or_none(rt_kinds),
                    pending_design_ids=pending_design_ids,
                )
                extra_events.append(
                    question_disposed_event(
                        study_id=study_id,
                        disposal=disposal,
                        now=now_stamp,
                    )
                )
            except (DisposeRejected, ValidationError) as e:
                extra_events.append(
                    question_dispose_rejected_event(
                        study_id=study_id,
                        detail=str(e),
                        now=now_stamp,
                        question_id=qid_hint,
                        disposition=disp_hint,
                    )
                )
                continue  # audit-ok[except_silent_continue]: executor.py:1442 except_silent_continue

        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome(final.value),
            None,
            claims=_or_empty_list(out.claims),
            checks=checks,
            infra_failure=False,
            charge_attempt=True,
            allow_retry=False,
            cost_rub=out.cost_rub,
            extra_events=extra_events,
        )

    async def record_review(
        self,
        attempt_id: str,
        check_id: str,
        *,
        item_id: str | None = None,
        status: ReqStatus | str | None = None,
        failure_kind: str | None = None,
    ) -> Result:
        """Apply a judge/review verdict and re-evaluate the open attempt (PR13).

        ``check_id`` may be ``parent#n`` or legacy ``parent#n:item``. Prefer
        ``item_id=`` for the item. Keys are scoped to task/attempt/output digest.

        ``failure_kind`` (timeout|invalid_json|refusal) → UNASSESSED + one retry
        (attempt stays open). Explicit FAILED = item not met (stays FAILED).
        """
        from coscientist.core.claims.reviews import resolve_review_status, review_key

        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = _attempt_get(attempt, "closed")
        study_id = _attempt_req(attempt, "study_id")
        task_id = _attempt_req(attempt, "task_id")
        if closed:
            raise AttemptClosedError(attempt_id)
        out = optional_item(self._pending_outputs, attempt_id)
        if out is None:
            raise KeyError(f"no pending agent output for attempt {attempt_id}")
        output_digest = self._ensure_output_digest(attempt_id, out)
        parent_check = check_id
        item = item_id
        if item is None and ":" in check_id and "|" not in check_id:
            # Legacy ``check_id:item_id`` argument form.
            parent_check, _, item = check_id.partition(":")  # audit-ok[str_partition]: executor.py:1495 str_partition
        item = item
        if item is None:
            item = ''
        key = review_key(
            task_id=str(task_id),
            attempt_id=attempt_id,
            check_id=parent_check,
            item_id=item,
            output_digest=output_digest,
        )
        prior = _or_zero_int(optional_item(self.judge_retry, key))
        st, retries, fk = resolve_review_status(status=status, failure_kind=failure_kind, prior_retries=prior)
        self.judge_retry[key] = retries
        # ONE status live == journal: PENDING while retry budget remains;
        # UNASSESSED only after retries exhausted (or explicit status).
        if fk and retries <= 1:
            wait_status = ReqStatus.PENDING
            self.review_statuses[key] = wait_status
            self.failure_kinds[key] = fk
        else:
            wait_status = st
            self.review_statuses[key] = wait_status
            if fk:
                self.failure_kinds[key] = fk
            else:
                if key in self.failure_kinds:
                    self.failure_kinds.pop(key)
        await self._append(
            [
                UnstampedEvent(
                    study_id=study_id,
                    type="ReviewRecorded",
                    entity_key=attempt_id,
                    idem_key=make_idem(
                        "ReviewRecorded",
                        attempt_id,
                        key,
                        _or_empty_str(wait_status.value),
                        str(retries),
                    ),
                    payload={
                        "review_key": key,
                        "attempt_id": attempt_id,
                        "task_id": task_id,
                        "check_id": parent_check,
                        "item_id": item,
                        "output_digest": output_digest,
                        "status": wait_status.value,
                        "failure_kind": fk,
                        "retry": retries,
                    },
                    ts=self.clock.now(),
                    schema_version=REVIEW_RECORDED_SCHEMA_VERSION,
                )
            ]
        )
        return await self.record(attempt_id, out)

    async def _commit_timeout(
        self,
        attempt_id: str,
        study_id: str,
        task_id: str,
        out: AgentTaskOutput,
        provenance: Provenance,
        *,
        checks: list[Any],
        reason: str,
        detail: str,
        agent_output_digest: str,
    ) -> Result:
        """Guest timeout: charged, non-countable; may retry until budget exhausted."""
        rid = result_id(attempt_id)
        payload = {
            "result_id": rid,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "outcome": Outcome.FAILED.value,
            "computed_outcome": Outcome.FAILED.value,
            "reason": reason,
            "provenance": provenance,
            "check_results": [c.model_dump(mode="json") for c in checks],
            "unassessed": [c.check_id for c in checks if c.status == ReqStatus.UNASSESSED],
            "artifacts": [h.artifact_id for h in out.outputs.values()],
            "agent_claim": _maybe_value(out.claim),
            "inputs": [],
            "detail": detail,
            "result_class": "timeout",
            "agent_output_digest": agent_output_digest,
        }
        payload["result_content_digest"] = payload_digest(payload)
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome.FAILED,
            reason,
            claims=_or_empty_list(out.claims),
            checks=checks,
            infra_failure=False,
            charge_attempt=True,
            allow_retry=True,
            cost_rub=out.cost_rub,
        )

    async def _commit_failure(
        self,
        attempt_id: str,
        study_id: str,
        task_id: str,
        out: AgentTaskOutput,
        provenance: Provenance,
        claim: Outcome | None,
        *,
        reason: str,
        detail: str,
        event_type: str,
        result_class: str,
        charge_attempt: bool,
        allow_retry: bool,
    ) -> Result:
        """Infra/core_bug/reject path — drop claims; infra never charges the *attempt*.

        Money spent is still debited when ``out.cost_rub`` is known (same batch).
        """
        rid = result_id(attempt_id)
        drop_claims = result_class in ("infra", "core_bug") or reason in (
            TaskReason.SAME_DIGEST_RETRY.value,
            TaskReason.CATALOG_ERROR.value,
            TaskReason.CORE_BUG.value,
            TaskReason.SANDBOX_INFRA.value,
        )
        payload = {
            "result_id": rid,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "outcome": Outcome.FAILED.value,
            "computed_outcome": Outcome.FAILED.value,
            "reason": reason,
            "provenance": provenance,
            "check_results": [],
            "unassessed": [],
            "artifacts": [h.artifact_id for h in out.outputs.values()],
            "agent_claim": _maybe_value(claim),
            "inputs": [],
            "detail": detail,
            "result_class": result_class,
        }
        payload["result_content_digest"] = payload_digest(payload)
        await self._append(
            [
                UnstampedEvent(
                    study_id=study_id,
                    type=cast(Any, event_type),
                    entity_key=attempt_id,
                    idem_key=make_idem(event_type, attempt_id),
                    payload={"error": detail, "reason": reason},
                    ts=self.clock.now(),
                    schema_version=writer_schema_version(event_type),
                )
            ]
        )
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome.FAILED,
            reason,
            claims=[],
            checks=[],
            infra_failure=drop_claims,
            charge_attempt=charge_attempt,
            allow_retry=allow_retry,
            cost_rub=out.cost_rub,
        )

    def _charged_count_after(self, task: Any, attempt_id: str, *, charge_attempt: bool) -> int:
        """How many charged attempts the task will have after closing ``attempt_id``.

        Reads ``charged`` from attempt rows (AttemptStarted stamps it); never
        invents True when the field is absent.
        """
        if charge_attempt:
            n = 1
        else:
            n = 0
        attempts = task.attempts
        if not isinstance(attempts, list):
            raise JournalFormatError("charged_count: task.attempts must be list")
        for aid in list(attempts):
            if aid == attempt_id:
                continue
            if self.get_attempt is None:
                raise JournalFormatError("charged_count: get_attempt required")
            att = self.get_attempt(aid)
            if att is None:
                raise JournalFormatError(
                    f"charged_count: missing attempt row for {aid!r}"
                )
            if isinstance(att, dict):
                if "charged" not in att:
                    raise JournalFormatError(
                        f"charged_count: attempt {aid!r} missing 'charged'"
                    )
                charged = att["charged"]
            else:
                try:
                    charged = att.charged
                except AttributeError as e:
                    raise JournalFormatError(
                        f"charged_count: attempt {aid!r} missing 'charged'"
                    ) from e
            if charged:
                n += 1
        return n

    async def _commit_result(
        self,
        attempt_id: str,
        study_id: str,
        task_id: str,
        payload: dict[str, Any],
        task_outcome: TaskOutcome,
        reason: str | None,
        *,
        claims: list[dict[str, Any]] | None = None,
        checks: list[Any] | None = None,
        infra_failure: bool,
        charge_attempt: bool,
        allow_retry: bool,
        cost_rub: Decimal | float | int | str | None = None,
        extra_events: list[UnstampedEvent] | None = None,
    ) -> Result:
        # Attempt-open check runs inside the write transaction (_append_many_sync).
        now = self.clock.now()
        if attempt_id in self._pending_outputs:
            self._pending_outputs.pop(attempt_id)
        if attempt_id in self._output_digests:
            self._output_digests.pop(attempt_id)
        if self.get_graph is not None:
            graph = self.get_graph()
        else:
            graph = None
        task = self.get_task(task_id)
        attempt = self.get_attempt(attempt_id)
        if attempt is not None:
            bindings = _or_empty_list(_attempt_get(attempt, "inputs"))
        else:
            bindings = []
        # Journal ResultRecorded.inputs from the attempt's P2 bindings.
        if bindings and not optional_item(payload, "inputs"):
            payload = {
                **payload,
                "inputs": [
                (
                    b.model_dump(mode="json")  # audit-ok[ternary_name]: executor.py:1753 ternary_name
                    if not isinstance(b, dict)
                    else dict(b)
                )
                for b in bindings
            ],
            }
            payload["result_content_digest"] = payload_digest(payload)
        payload = {**payload, "charged": charge_attempt is True}
        payload["result_content_digest"] = payload_digest(payload)

        # PR12 B3: remaining share limit from AttemptStarted; overrun forbids retries.
        limit_raw: Any = None
        if isinstance(attempt, dict):
            limit_raw = optional_item(attempt, "budget_limit_rub")
        elif attempt is not None:
            try:
                limit_raw = attempt.budget_limit_rub
            except AttributeError:
                limit_raw = None
        spent_this: Decimal | None = None
        if cost_rub is not None:
            spent_this = Decimal(str(cost_rub))
            if spent_this <= 0:
                spent_this = None
        overrun = False
        if spent_this is not None and limit_raw is not None and spent_this > Decimal(str(limit_raw)):
            overrun = True
            payload = {**payload, "budget_overrun": True, "budget_limit_rub": str(limit_raw)}
            payload["result_content_digest"] = payload_digest(payload)
            allow_retry = False

        # PR7/PR8: claim nodes carry producing-attempt provenance (H-b);
        # re-upsert keeps worst provenance; about edges + attr whitelist.
        # Scientific FAILED/PARTIAL keep claims (not_met → refute); infra drops.
        # Guest timeout keeps claims (refute_unresolved / timeout_kind).
        claim_events, drops = claim_nodes_from_attempt(
            study_id=study_id,
            attempt_id=attempt_id,
            provenance=payload["provenance"],
            claims=_or_empty_list(claims),
            now=now,
            graph=graph,
            attempt_outcome=task_outcome.value,
            infra_failure=infra_failure,
            derive_attrs=lambda raw, validated: _derive_claim_core_attrs(
                task,
                task_outcome,
                raw=raw,
                checks=list(_or_empty_list(checks)),
                graph=graph,
                study_id=study_id,
                validated=validated,
                catalog=self.catalog,
                bindings=bindings,
                artifact_store=self.artifacts,
                results_map=(_maybe_call(self.get_results)),
                attempt_id=attempt_id,
            ),
        )
        if drops:
            payload = {**payload, "claims_dropped": drops}
            payload["result_content_digest"] = payload_digest(payload)

        # PR15: contradicts edges for opposite-stance accepted claims (same batch).
        from coscientist.core.claims.contradicts import contradicts_events_for_new_claims

        new_claim_ids = {
            _or_empty_str(ev.entity_key)
            for ev in claim_events
            if ev.type == "GraphNodeUpserted"
        }
        contradict_events = contradicts_events_for_new_claims(
            study_id=study_id,
            graph=graph,
            now=now,
            new_claim_ids=new_claim_ids or None,
            pending_claim_events=claim_events,
        )

        charged_after = self._charged_count_after(task, attempt_id, charge_attempt=charge_attempt)
        deadline = task.deadline
        within_deadline = deadline is None or now < deadline
        schedule_retry = allow_retry and charged_after < self.max_attempts and within_deadline

        close_payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "closed": True,
            "close_reason": CloseReason.RESULT.value,
            "task_id": task_id,
            "charged": charge_attempt is True,
            "retryable": schedule_retry is True,
        }
        if schedule_retry:
            close_payload["retry_at"] = (now + timedelta(seconds=RETRY_BACKOFF_S)).isoformat()

        events = [
            *claim_events,
            *contradict_events,
            *_or_empty_tuple(extra_events),
            UnstampedEvent(
                study_id=study_id,
                type="ResultRecorded",
                entity_key=payload["result_id"],
                idem_key=make_idem("ResultRecorded", payload["result_id"]),
                payload=payload,
                ts=now,
                schema_version=RESULT_RECORDED_SCHEMA_VERSION,
            ),
            UnstampedEvent(
                study_id=study_id,
                type="AttemptClosed",
                entity_key=attempt_id,
                idem_key=make_idem("AttemptClosed", attempt_id, CloseReason.RESULT.value),
                payload=close_payload,
                ts=now,
                schema_version=ATTEMPT_CLOSED_SCHEMA_VERSION,
            ),
        ]
        if not schedule_retry:
            done_payload: dict[str, Any] = {
                "task_id": task_id,
                "outcome": task_outcome.value,
                "reason": reason,
            }
            # Keep original detail/result; overrun is a separate flag (not «не выполнено»).
            if optional_item(payload, "detail"):
                done_payload["detail"] = payload["detail"]
            if overrun:
                done_payload["budget_overrun"] = True
            events.append(
                UnstampedEvent(
                    study_id=study_id,
                    type="TaskDone",
                    entity_key=task_id,
                    idem_key=make_idem("TaskDone", task_id),
                    payload=done_payload,
                    ts=now,
                    schema_version=TASK_DONE_SCHEMA_VERSION,
                )
            )
        # PR12: debit actual cost_rub in the SAME append batch as the result
        # (scientific / timeout / same_digest / core_bug / catalog_error / infra).
        if spent_this is not None:
            from coscientist.core.plan.share_alloc import budget_debit_event

            debit_ev = budget_debit_event(
                study_id=study_id,
                spent_rub=spent_this,
                request_id=attempt_id,
                task_id=task_id,
                ts=now,
            )
            if debit_ev is not None:
                events.append(debit_ev)
        await self._append(events, require_open_attempt=attempt_id)
        if spent_this is not None and self.on_budget_debit is not None:
            await self.on_budget_debit(spent_rub=spent_this, request_id=attempt_id, task_id=task_id)
        return Result(
            result_id=payload["result_id"],
            attempt_id=attempt_id,
            outcome=Outcome(payload["outcome"]),
            provenance=payload["provenance"],
            agent_claim=_outcome_from_payload(payload),
            computed_outcome=Outcome(payload["computed_outcome"]),
            artifacts=_or_empty_list(optional_item(payload, "artifacts")),
            unassessed=_or_empty_list(optional_item(payload, "unassessed")),
            payload_digest=payload["result_content_digest"],
            reason=reason,
        )

    async def _append(
        self,
        events: list[UnstampedEvent],
        *,
        require_open_attempt: str | None = None,
    ) -> None:
        stored = await self.writer.append_many(events, require_open_attempt=require_open_attempt)
        # Project in append order (same seq order as the durable batch). Do not
        # ``asyncio.sleep(0)`` here — a yield can let another task's ``_project_local``
        # run before this batch, scrambling in-memory fold order. Dirty-key
        # upserts alone keep the fsync p99 under the §3.5a SLO.
        if self.on_events is not None:
            for ev in stored:
                self.on_events(ev)
