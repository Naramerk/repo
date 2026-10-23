"""Executor.record — sole result-write path (DESIGN §4)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any

from coscientist.checks.evaluate import evaluate_task_checks, role_from_catalog
from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.attempts import RETRY_BACKOFF_S
from coscientist.core.digest import canonical_json, payload_digest, sha256_hex
from coscientist.core.domain import (
    VALID_PROVENANCE,
    AgentTaskOutput,
    Catalog,
    ExperimentMethod,
    Outcome,
    Provenance,
    ReqStatus,
    Result,
    TaskOutcome,
)
from coscientist.core.errors import (
    AttemptClosedError,
    CatalogError,
    CoreBugError,
    SandboxInfraError,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.ids import criterion_node_id, result_id
from coscientist.core.inputs import claim_nodes_from_attempt
from coscientist.core.outcome import compute_outcome
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.writer import StudyWriter

log = logging.getLogger(__name__)


def _provenance(attempt: Any) -> Provenance:
    p = getattr(attempt, "provenance", None)
    if p is None and isinstance(attempt, dict):
        p = attempt.get("provenance")
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
    from coscientist.core.verdict import recompute_effect_provenance

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
    # Required validity failure invalidates this experiment's claims only.
    # Rubric (even role=validity) never flips validity_ok — see _is_rubric_check.
    if _required_validity_failed(checks):
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
    from coscientist.core.verdict import (
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
    from coscientist.core.derivation import (
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
        max_attempts: int = 3,
        on_budget_debit: Any | None = None,
    ) -> None:
        self.writer = writer
        self.catalog = catalog
        self.artifacts = artifacts
        self.clock = clock
        self.get_attempt = get_attempt
        self.get_task = get_task
        self.on_events = on_events  # callback after successful append (project local)
        self.review_statuses = review_statuses or {}
        # PR13: journaled judge state (rebuilt via apply_judge_journal on reopen).
        self.judge_retry: dict[str, int] = {}
        self.failure_kinds: dict[str, str] = {}
        self._pending_outputs: dict[str, AgentTaskOutput] = {}
        self._output_digests: dict[str, str] = {}
        self.get_graph = get_graph
        self.get_results = get_results
        # PR15: folded ArtifactRegistered(stamped) kinds (never trust meta alone).
        self.get_artifact_kinds = get_artifact_kinds
        self.max_attempts = int(max_attempts)
        # PR12: async (task_id, spent_rub, request_id) → record_budget_debit
        self.on_budget_debit = on_budget_debit
        self._scheduled_reviews: list[str] = []

    def _ensure_output_digest(self, attempt_id: str, out: AgentTaskOutput) -> str:
        """CAS-store agent output; return digest used in scoped review keys."""
        dig: str | None = self._output_digests.get(attempt_id)
        if dig:
            return dig
        attempt = self.get_attempt(attempt_id)
        if attempt is not None:
            if hasattr(attempt, "agent_output_digest") and attempt.agent_output_digest:
                dig = str(attempt.agent_output_digest)
            elif isinstance(attempt, dict) and attempt.get("agent_output_digest"):
                dig = str(attempt["agent_output_digest"])
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
        closed = attempt.closed if hasattr(attempt, "closed") else attempt.get("closed")
        study_id = attempt.study_id if hasattr(attempt, "study_id") else attempt["study_id"]
        if closed:
            raise AttemptClosedError(attempt_id)
        content = canonical_json(out.model_dump(mode="json"))
        digest = self.artifacts.put_cas(content, tmp_name=f"route:{attempt_id}")
        await self._append(
            [
                EventEnvelope(
                    study_id=study_id,
                    type="RouteReturned",
                    entity_key=attempt_id,
                    idem_key=make_idem("RouteReturned", attempt_id),
                    payload={
                        "attempt_id": attempt_id,
                        "agent_output_digest": digest,
                    },
                    ts=self.clock.now(),
                )
            ],
            require_open_attempt=attempt_id,
        )
        return digest

    async def record(self, attempt_id: str, out: AgentTaskOutput) -> Result:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = attempt.closed if hasattr(attempt, "closed") else attempt.get("closed")
        study_id = attempt.study_id if hasattr(attempt, "study_id") else attempt["study_id"]
        task_id = attempt.task_id if hasattr(attempt, "task_id") else attempt["task_id"]

        if closed:
            await self._append(
                [
                    EventEnvelope(
                        study_id=study_id,
                        type="LateWriteRejected",
                        entity_key=attempt_id,
                        idem_key=make_idem("LateWriteRejected", attempt_id, str(self.clock.now())),
                        payload={"attempt_id": attempt_id, "reason": "attempt_closed"},
                        ts=self.clock.now(),
                    )
                ]
            )
            raise AttemptClosedError(attempt_id)

        task = self.get_task(task_id)
        # PR18: per-attempt ContextVar taint (not shared store state — foreach-safe).
        from coscientist.core.taint import push_attempt_taint, reset_attempt_taint

        _tainted = False
        if hasattr(attempt, "tainted_context"):
            _tainted = bool(attempt.tainted_context)
        elif isinstance(attempt, dict):
            _tainted = bool(attempt.get("tainted_context"))
        _taint_token = push_attempt_taint(_tainted)
        try:
            try:
                # MJ2: no sync Popen on the event loop — evaluate (sandbox) off-thread.
                # to_thread on 3.12+ copies ContextVar so sandbox puts see taint.
                from coscientist.core.source_fetch import (
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
                    task_kind=str(getattr(task.kind, "value", task.kind)),
                    task_key=task.spec.task_key,
                    fetched_digests=fetch_idx.digests_for_attempt(attempt_id),
                )
            except SandboxInfraError as e:
                # Host/private-FD infra — does NOT charge the retry budget.
                # Permanent unavailability closes the task (allow_retry=False); the
                # attempt stays charged=False so a later reopen/retry budget is intact.
                # Transient attach failures may retry without consuming charged slots.
                detail = e.reason
                transient = "cgroup_attach" in detail or detail.endswith(":attach")
                provenance = _provenance(self.get_attempt(attempt_id))
                return await self._commit_failure(
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
                return await self._commit_failure(
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
                )
        finally:
            reset_attempt_taint(_taint_token)
        for c in checks:
            if not c.is_code and c.status == ReqStatus.UNASSESSED:
                self._scheduled_reviews.append(c.check_id)

        from coscientist.checks.kinds._util import check_is_guest_timeout, merge_detail

        timeout_checks = [c for c in checks if check_is_guest_timeout(c)]
        # Digest over content actually mounted into the jail (not artifact ids).
        jail_digests = [
            str((c.effect or {}).get("jail_inputs_digest") or "")
            for c in checks
            if isinstance(getattr(c, "effect", None), dict) and (c.effect or {}).get("jail_inputs_digest")
        ]
        if jail_digests:
            agent_digest = sha256_hex(canonical_json({"jail_inputs": sorted(set(jail_digests))}))
        else:
            # Fallback: content hashes of declared outputs (same as mount inputs).
            mounted: dict[str, str] = {}
            for name, handle in sorted((out.outputs or {}).items()):
                meta = self.artifacts.get_meta(handle.artifact_id)
                if meta is None:
                    continue
                mounted[str(name)] = str(meta.sha256)
            agent_digest = sha256_hex(canonical_json({"mounted": mounted}))
        results_map = self.get_results() if self.get_results is not None else {}
        for prev_aid in list(getattr(task, "attempts", []) or []):
            if prev_aid == attempt_id:
                continue
            prev = results_map.get(prev_aid) if isinstance(results_map, dict) else None
            if not isinstance(prev, dict):
                continue
            if str(prev.get("result_class") or "") != "timeout":
                continue
            prev_dig = prev.get("agent_output_digest")
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
                str((c.effect or {}).get("timeout_kind") or "") for c in timeout_checks if isinstance(c.effect, dict)
            ]

            # Structural: ≥2 prediction/refute-role timeouts on this TASK
            # (any template_id — alternation does not evade) → untestable.
            # A single timeout (even on the last charged attempt) stays a
            # plain timeout; refute_unresolved already blocks supported.
            def _prediction_timeout_in_row(row: dict[str, Any]) -> bool:
                if str(row.get("result_class") or "") != "timeout":
                    return False
                for cr in row.get("check_results") or []:
                    if not isinstance(cr, dict):
                        continue
                    if str(cr.get("role") or "") != "prediction":
                        continue
                    raw_eff = cr.get("effect")
                    eff: dict[str, Any] = dict(raw_eff) if isinstance(raw_eff, dict) else {}
                    if eff.get("timeout_kind") or eff.get("refute_unresolved"):
                        return True
                return False

            current_prediction_timeout = any(getattr(c, "role", None) == "prediction" for c in timeout_checks)
            prior_prediction_timeouts = 0
            for prev_aid in list(getattr(task, "attempts", []) or []):
                if prev_aid == attempt_id:
                    continue
                prev = results_map.get(prev_aid) if isinstance(results_map, dict) else None
                if isinstance(prev, dict) and _prediction_timeout_in_row(prev):
                    prior_prediction_timeouts += 1
            task_prediction_timeouts = prior_prediction_timeouts + (1 if current_prediction_timeout else 0)
            repeat_refute_timeout = task_prediction_timeouts >= 2
            idle = any(k == "idle_timeout" for k in kinds) or repeat_refute_timeout
            if repeat_refute_timeout:
                elevated: list[Any] = []
                for c in checks:
                    if (
                        check_is_guest_timeout(c)
                        and getattr(c, "role", None) == "prediction"
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
                                        getattr(c, "detail", None),
                                        "repeat_timeout",
                                    ),
                                }
                            )
                        )
                    else:
                        elevated.append(c)
                checks = elevated
                kinds = ["idle_timeout"]
            timeout_reason = TaskReason.IDLE_TIMEOUT.value if idle else TaskReason.SANDBOX_TIMEOUT.value
            return await self._commit_timeout(
                attempt_id,
                study_id,
                task_id,
                out,
                provenance,
                checks=checks,
                reason=timeout_reason,
                detail=";".join(kinds) or "timeout",
                agent_output_digest=str(agent_digest or ""),
            )

        claim = out.claim
        reason: str | None = None
        if out.downgrade is not None:
            if not out.downgrade.refs or not out.downgrade.reason:
                await self._append(
                    [
                        EventEnvelope(
                            study_id=study_id,
                            type="DowngradeRejected",
                            entity_key=attempt_id,
                            idem_key=make_idem("DowngradeRejected", attempt_id),
                            payload={
                                "attempt_id": attempt_id,
                                "reason": "downgrade requires reason and refs",
                            },
                            ts=self.clock.now(),
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
            return await self._commit_failure(
                attempt_id,
                study_id,
                task_id,
                out,
                provenance,
                claim,
                reason=TaskReason.CATALOG_ERROR.value,
                detail=str(e),
            )

        if computed is None:
            from coscientist.core.reviews import review_scheduled_events

            output_digest = self._ensure_output_digest(attempt_id, out)
            # Idem must change as judge answers arrive (clock may be frozen in tests).
            status_fp = ",".join(f"{c.check_id}={c.status.value}" for c in sorted(checks, key=lambda x: x.check_id))
            rev_fp = ",".join(f"{k}={v.value}" for k, v in sorted(self.review_statuses.items(), key=lambda kv: kv[0]))
            retry_fp = ",".join(f"{k}={v}" for k, v in sorted(self.judge_retry.items()))
            pending_events = [
                EventEnvelope(
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

        from coscientist.core.reviews import review_scheduled_events

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
        final = downgrade.to if downgrade else computed
        from coscientist.core.verdict import output_kinds_from_catalog

        # Code|data from catalog code_output_names (ignore planner OutputSpec.role).
        out_names = list((out.outputs or {}).keys())
        for ospec in getattr(getattr(task, "spec", None), "expected_outputs", None) or ():
            n = str(getattr(ospec, "name", "") or "")
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
            "output_map": {name: h.artifact_id for name, h in (out.outputs or {}).items()},
            "output_kinds": output_kinds,
            "agent_claim": claim.value if claim else None,
            "downgrade": (downgrade.model_dump(mode="json", by_alias=True) if downgrade else None),
            "inputs": [],
            # PR9 H-g: typed expansion request (applied via PLAN-n → critic).
            "expansion_request": (
                out.expansion_request.model_dump(mode="json") if out.expansion_request is not None else None
            ),
        }
        # Scientific result: criteria evaluated (SUCCESS / PARTIAL / FAILED).
        # Keep claims so not_met can refute (invariant 10); only infra drops them.
        payload["result_class"] = "scientific"
        payload["payload_digest"] = payload_digest(payload)
        # PR15: journal kind stamps + QuestionDisposed in the same append as result.
        # Meta.kind is applied only in fold after a successful append (never pre-commit).
        from pydantic import ValidationError

        from coscientist.core.artifact_stamp import kind_stamped_event
        from coscientist.core.dispose import (
            DisposeRejected,
            QuestionDisposal,
            question_dispose_rejected_event,
            question_disposed_event,
            validate_disposal,
        )
        from coscientist.core.errors import ArtifactRejected
        from coscientist.core.experiment_design import artifact_kind_from_output_spec

        out_map = dict(out.outputs or {})
        extra_events: list[EventEnvelope] = []
        now_stamp = self.clock.now()
        pending_design_ids: set[str] = set()
        for ospec in getattr(task.spec, "expected_outputs", None) or ():
            kind = artifact_kind_from_output_spec(ospec)
            if not kind:
                continue
            name = str(getattr(ospec, "name", "") or "")
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
        for raw in list(out.question_disposals or []):
            qid_hint: str | None = None
            disp_hint: str | None = None
            try:
                if isinstance(raw, dict):
                    qid_hint = str(raw.get("question_id") or "") or None
                    disp_hint = str(raw.get("disposition") or "") or None
                disposal = (
                    raw
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
                    for ospec in getattr(task.spec, "expected_outputs", None) or ():
                        if artifact_kind_from_output_spec(ospec) == "experiment_design":
                            oname = str(ospec.name)
                            if oname in out_map:
                                disposal = disposal.model_copy(
                                    update={"design_artifact_id": out_map[oname].artifact_id}
                                )
                                break
                # Prefer live graph; else task addresses — never invent from disposal id.
                g = self.get_graph() if self.get_graph is not None else None
                known: set[str] = set()
                if g is not None:
                    for n in g.nodes_by_kind("question"):
                        if n.study_id == study_id:
                            known.add(n.ref_id or n.node_id.split(":")[-1])
                if not known:
                    known = {str(a) for a in (task.spec.addresses or []) if a}
                kinds_fn = getattr(self, "get_artifact_kinds", None)
                rt_kinds = kinds_fn() if callable(kinds_fn) else None
                validate_disposal(
                    disposal,
                    known_question_ids=known,
                    covering_task_keys={task.spec.task_key},
                    tasks={task_id: task},
                    results=self.get_results() if self.get_results is not None else None,
                    journal_kinds=rt_kinds if isinstance(rt_kinds, dict) else None,
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
                continue

        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome(final.value),
            None,
            claims=list(out.claims or []),
            checks=checks,
            infra_failure=False,
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
        from coscientist.core.reviews import resolve_review_status, review_key

        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        closed = attempt.closed if hasattr(attempt, "closed") else attempt.get("closed")
        study_id = attempt.study_id if hasattr(attempt, "study_id") else attempt["study_id"]
        task_id = attempt.task_id if hasattr(attempt, "task_id") else attempt["task_id"]
        if closed:
            raise AttemptClosedError(attempt_id)
        out = self._pending_outputs.get(attempt_id)
        if out is None:
            raise KeyError(f"no pending agent output for attempt {attempt_id}")
        output_digest = self._ensure_output_digest(attempt_id, out)
        parent_check = check_id
        item = item_id
        if item is None and ":" in check_id and "|" not in check_id:
            # Legacy ``check_id:item_id`` argument form.
            parent_check, _, item = check_id.partition(":")
        item = item or ""
        key = review_key(
            task_id=str(task_id),
            attempt_id=attempt_id,
            check_id=parent_check,
            item_id=item,
            output_digest=output_digest,
        )
        prior = int(self.judge_retry.get(key, 0))
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
                self.failure_kinds.pop(key, None)
        await self._append(
            [
                EventEnvelope(
                    study_id=study_id,
                    type="ReviewRecorded",
                    entity_key=attempt_id,
                    idem_key=make_idem(
                        "ReviewRecorded",
                        attempt_id,
                        key,
                        str(wait_status.value),
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
            "agent_claim": out.claim.value if out.claim else None,
            "inputs": [],
            "detail": detail,
            "result_class": "timeout",
            "agent_output_digest": agent_output_digest,
        }
        payload["payload_digest"] = payload_digest(payload)
        return await self._commit_result(
            attempt_id,
            study_id,
            task_id,
            payload,
            TaskOutcome.FAILED,
            reason,
            claims=list(out.claims or []),
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
        event_type: str = "CatalogErrorRaised",
        result_class: str = "infra",
        charge_attempt: bool = True,
        allow_retry: bool = False,
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
            "agent_claim": claim.value if claim else None,
            "inputs": [],
            "detail": detail,
            "result_class": result_class,
        }
        payload["payload_digest"] = payload_digest(payload)
        await self._append(
            [
                EventEnvelope(
                    study_id=study_id,
                    type=event_type,
                    entity_key=attempt_id,
                    idem_key=make_idem(event_type, attempt_id),
                    payload={"error": detail, "reason": reason},
                    ts=self.clock.now(),
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
        """How many charged attempts the task will have after closing ``attempt_id``."""
        results = self.get_results() if self.get_results is not None else {}
        n = 1 if charge_attempt else 0
        for aid in list(getattr(task, "attempts", []) or []):
            if aid == attempt_id:
                continue
            row = results.get(aid) if isinstance(results, dict) else None
            if isinstance(row, dict):
                if row.get("charged", True):
                    n += 1
            else:
                # Prior closed attempt without a row — assume charged.
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
        infra_failure: bool = False,
        charge_attempt: bool = True,
        allow_retry: bool = False,
        cost_rub: Decimal | float | int | str | None = None,
        extra_events: list[EventEnvelope] | None = None,
    ) -> Result:
        # Attempt-open check runs inside the write transaction (_append_many_sync).
        now = self.clock.now()
        self._pending_outputs.pop(attempt_id, None)
        self._output_digests.pop(attempt_id, None)
        graph = self.get_graph() if self.get_graph is not None else None
        task = self.get_task(task_id)
        attempt = self.get_attempt(attempt_id)
        if attempt is not None and hasattr(attempt, "inputs"):
            bindings = list(attempt.inputs or [])
        elif isinstance(attempt, dict):
            bindings = list(attempt.get("inputs") or [])
        else:
            bindings = []
        # Journal ResultRecorded.inputs from the attempt's P2 bindings.
        if bindings and not payload.get("inputs"):
            payload = {
                **payload,
                "inputs": [b.model_dump(mode="json") if hasattr(b, "model_dump") else dict(b) for b in bindings],
            }
            payload["payload_digest"] = payload_digest(payload)
        payload = {**payload, "charged": bool(charge_attempt)}
        payload["payload_digest"] = payload_digest(payload)

        # PR12 B3: remaining share limit from AttemptStarted; overrun forbids retries.
        limit_raw: Any = None
        if isinstance(attempt, dict):
            limit_raw = attempt.get("budget_limit_rub")
        elif attempt is not None:
            limit_raw = getattr(attempt, "budget_limit_rub", None)
        spent_this: Decimal | None = None
        if cost_rub is not None:
            spent_this = Decimal(str(cost_rub))
            if spent_this <= 0:
                spent_this = None
        overrun = False
        if spent_this is not None and limit_raw is not None and spent_this > Decimal(str(limit_raw)):
            overrun = True
            payload = {**payload, "budget_overrun": True, "budget_limit_rub": str(limit_raw)}
            payload["payload_digest"] = payload_digest(payload)
            allow_retry = False

        # PR7/PR8: claim nodes carry producing-attempt provenance (H-b);
        # re-upsert keeps worst provenance; about edges + attr whitelist.
        # Scientific FAILED/PARTIAL keep claims (not_met → refute); infra drops.
        # Guest timeout keeps claims (refute_unresolved / timeout_kind).
        claim_events, drops = claim_nodes_from_attempt(
            study_id=study_id,
            attempt_id=attempt_id,
            provenance=payload["provenance"],
            claims=list(claims or []),
            now=now,
            graph=graph,
            attempt_outcome=task_outcome.value,
            infra_failure=infra_failure,
            derive_attrs=lambda raw, validated: _derive_claim_core_attrs(
                task,
                task_outcome,
                raw=raw,
                checks=list(checks or []),
                graph=graph,
                study_id=study_id,
                validated=validated,
                catalog=self.catalog,
                bindings=bindings,
                artifact_store=self.artifacts,
                results_map=(self.get_results() if self.get_results is not None else None),
                attempt_id=attempt_id,
            ),
        )
        if drops:
            payload = {**payload, "claims_dropped": drops}
            payload["payload_digest"] = payload_digest(payload)

        # PR15: contradicts edges for opposite-stance accepted claims (same batch).
        from coscientist.core.contradicts import contradicts_events_for_new_claims

        new_claim_ids = {
            str(ev.entity_key)
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
        deadline = getattr(task, "deadline", None)
        within_deadline = deadline is None or now < deadline
        schedule_retry = allow_retry and charged_after < self.max_attempts and within_deadline

        close_payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "closed": True,
            "close_reason": CloseReason.RESULT.value,
            "task_id": task_id,
            "charged": bool(charge_attempt),
            "retryable": bool(schedule_retry),
        }
        if schedule_retry:
            close_payload["retry_at"] = (now + timedelta(seconds=RETRY_BACKOFF_S)).isoformat()

        events = [
            *claim_events,
            *contradict_events,
            *(extra_events or ()),
            EventEnvelope(
                study_id=study_id,
                type="ResultRecorded",
                entity_key=payload["result_id"],
                idem_key=make_idem("ResultRecorded", payload["result_id"]),
                payload=payload,
                ts=now,
                schema_version=3,
            ),
            EventEnvelope(
                study_id=study_id,
                type="AttemptClosed",
                entity_key=attempt_id,
                idem_key=make_idem("AttemptClosed", attempt_id, CloseReason.RESULT.value),
                payload=close_payload,
                ts=now,
                schema_version=2,
            ),
        ]
        if not schedule_retry:
            done_payload: dict[str, Any] = {
                "task_id": task_id,
                "outcome": task_outcome.value,
                "reason": reason,
            }
            # Keep original detail/result; overrun is a separate flag (not «не выполнено»).
            if payload.get("detail"):
                done_payload["detail"] = payload["detail"]
            if overrun:
                done_payload["budget_overrun"] = True
            events.append(
                EventEnvelope(
                    study_id=study_id,
                    type="TaskDone",
                    entity_key=task_id,
                    idem_key=make_idem("TaskDone", task_id),
                    payload=done_payload,
                    ts=now,
                    schema_version=2,
                )
            )
        # PR12: debit actual cost_rub in the SAME append batch as the result
        # (scientific / timeout / same_digest / core_bug / catalog_error / infra).
        if spent_this is not None:
            from coscientist.core.share_alloc import budget_debit_event

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
            agent_claim=(Outcome(payload["agent_claim"]) if payload.get("agent_claim") else None),
            computed_outcome=Outcome(payload["computed_outcome"]),
            artifacts=list(payload.get("artifacts", [])),
            unassessed=list(payload.get("unassessed", [])),
            payload_digest=payload["payload_digest"],
            reason=reason,
        )

    async def _append(
        self,
        events: list[EventEnvelope],
        *,
        require_open_attempt: str | None = None,
    ) -> None:
        stored = await self.writer.append_many(events, require_open_attempt=require_open_attempt)
        # Project in append order (same seq order as the durable batch). Do not
        # ``asyncio.sleep(0)`` here — a yield can let another task's ``_project_local``
        # run before this batch, scrambling in-memory fold order. Dirty-key
        # upserts alone keep the fsync p99 under the §3.5a SLO.
        if self.on_events is not None:
            for ev in events:
                self.on_events(ev)
        _ = stored
