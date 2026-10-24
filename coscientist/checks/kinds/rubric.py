"""Evaluate CodeCheck kind ``rubric`` (M1 PR13 / DESIGN_M1 §6.2).

Items are atomic and binary. ``check=code`` delegates to a nested CodeCheck
kind (never ``rubric``). ``check=judge`` uses ``review_statuses``:

- missing status → PENDING (waiting for judge; blocks outcome)
- MET / FAILED (not-met verdict) → kept as-is
- judge *failure* (timeout / invalid_json / refusal) → UNASSESSED + 1 retry
  via ``judge_retry`` / ``failure_kinds`` (never silently remap FAILED→UNASSESSED)

Aggregate status never feeds claim stance (role must be validity).
"""

from __future__ import annotations

from typing import Any

from coscientist.core.claims.reviews import review_key
from coscientist.core.claims.rubric import aggregate_rubric_status, freeze_rubric
from coscientist.core.claims.rubric_types import Rubric
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.domain import AgentTaskOutput, CheckResult, CodeCheck, ReqStatus

# Infra failures of the judge call itself (DESIGN §6.2) — not "item not met".
JUDGE_FAILURE_KINDS = frozenset({"timeout", "invalid_json", "refusal"})


def _as_rubric(raw: Any) -> Rubric:
    if isinstance(raw, Rubric):
        return freeze_rubric(raw)
    return freeze_rubric(Rubric.model_validate(raw))


def judge_item_key(
    check_id: str,
    item_id: str,
    *,
    task_id: str,
    attempt_id: str,
    output_digest: str,
) -> str:
    """Scoped review key — fail-closed when scope fields are empty (no bare fallback)."""
    return review_key(
        task_id=task_id,
        attempt_id=attempt_id,
        check_id=check_id,
        item_id=item_id,
        output_digest=output_digest,
    )


def evaluate_rubric(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: Any,
    role: str,
    review_statuses: dict[str, ReqStatus] | None = None,
    judge_retry: dict[str, int] | None = None,
    failure_kinds: dict[str, str] | None = None,
    output_rubric: Any | None = None,
    attempt_id: str | None = None,
    task_id: str | None = None,
    output_digest: str | None = None,
    evaluate_code_check: Any | None = None,
) -> CheckResult:
    """Evaluate a rubric check. Bugs propagate (no swallow → UNASSESSED)."""
    if review_statuses is None:
        review_statuses = {}
    if judge_retry is None:
        judge_retry = {}
    if failure_kinds is None:
        failure_kinds = {}
    return _evaluate(
        check,
        template_id=template_id,
        check_id=check_id,
        out=out,
        artifacts=artifacts,
        role=role,
        review_statuses=review_statuses,
        judge_retry=judge_retry,
        failure_kinds=failure_kinds,
        output_rubric=output_rubric,
        attempt_id=attempt_id,
        task_id=task_id,
        output_digest=output_digest,
        evaluate_code_check=evaluate_code_check,
    )


def _evaluate(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: Any,
    role: str,
    review_statuses: dict[str, ReqStatus],
    judge_retry: dict[str, int],
    failure_kinds: dict[str, str],
    output_rubric: Any | None,
    attempt_id: str | None,
    task_id: str | None,
    output_digest: str | None,
    evaluate_code_check: Any | None,
) -> CheckResult:
    # Plan-frozen OutputSpec.rubric wins over catalog placeholder rubric.
    raw = output_rubric if output_rubric is not None else check.rubric  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    if raw is None:
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            role="validity",
            detail="rubric_missing",
        used_sesoi=False,
    )
    rubric = _as_rubric(raw)
    item_statuses: dict[str, ReqStatus] = {}
    details: list[str] = []
    pending_items: list[str] = []
    has_judge = False
    has_code = False

    if not task_id or not attempt_id or not output_digest:
        raise ValueError("evaluate_rubric requires non-empty task_id, attempt_id, output_digest")
    for item in rubric.items:
        item_key = judge_item_key(
            check_id,
            item.id,
            task_id=task_id,
            attempt_id=attempt_id,
            output_digest=output_digest,
        )
        if item.check == "judge":
            has_judge = True
            # Scoped lookup only — never fall back to bare item.id.
            st = review_statuses.get(item_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            fk = failure_kinds.get(item_key)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if item_key in judge_retry:
                retries = require_exact_int(judge_retry[item_key], what=f"judge_retry[{item_key}]")
            else:
                retries = 0
            if st is None and fk is None:
                # Waiting for judge — PENDING blocks compute_outcome.
                st = ReqStatus.PENDING
                pending_items.append(item.id)
                details.append(f"{item.id}:judge_pending")
            elif st == ReqStatus.FAILED:
                # Not-met verdict — stay FAILED (never remap to UNASSESSED).
                details.append(f"{item.id}:judge:FAILED")
            elif fk in JUDGE_FAILURE_KINDS:
                # Infra failure (timeout/invalid_json/refusal): one retry (R23).
                # While retry budget remains, stay PENDING so the attempt stays open.
                if retries <= 1 and st in (
                    None,
                    ReqStatus.PENDING,
                    ReqStatus.UNASSESSED,
                ):
                    st = ReqStatus.PENDING
                    pending_items.append(item.id)
                    details.append(f"{item.id}:judge_failure:{fk}:retry={retries}:waiting")
                else:
                    st = ReqStatus.UNASSESSED
                    details.append(f"{item.id}:judge_failure:{fk}:retry={retries}")
            elif st is None:
                st = ReqStatus.UNASSESSED
                details.append(f"{item.id}:judge_unassessed")
            elif st == ReqStatus.PENDING:
                pending_items.append(item.id)
                details.append(f"{item.id}:judge_pending")
            else:
                details.append(f"{item.id}:judge:{st.value}")
            item_statuses[item.id] = st
            continue

        has_code = True
        if item.code_kind is None:
            kind = "artifact_exists"
        else:
            kind = item.code_kind
        if kind == "rubric":
            item_statuses[item.id] = ReqStatus.FAILED
            details.append(f"{item.id}:nested_rubric_forbidden")
            continue
        if check.output is None:
            nested_output = "main"
        else:
            nested_output = check.output
        nested = CodeCheck(kind=kind, output=nested_output, allow_network=False)  # type: ignore[arg-type]
        if evaluate_code_check is None:
            from coscientist.checks.evaluate import evaluate_code_check as _ecc

            evaluate_code_check = _ecc
        cr = evaluate_code_check(
            nested,
            template_id=f"{template_id}:{item.id}",
            check_id=item_key,
            out=out,
            artifacts=artifacts,
            values=out.values,
            role="validity",
            attempt_id=attempt_id,
            uses_sesoi=False,
            predicts_null=False,
            predicted_direction="two_sided",
        )
        item_statuses[item.id] = cr.status
        if cr.detail is None:
            detail_part = ""
        else:
            detail_part = cr.detail
        details.append(f"{item.id}:code:{cr.status.value}:{detail_part}")

    overall = aggregate_rubric_status(item_statuses, rubric)
    is_code = has_code and not has_judge
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=overall,
        is_code=is_code,
        required=True,
        role="validity",
        detail=f"rubric:{rubric.digest};" + ";".join(details),
        effect={
            "rubric_digest": rubric.digest,
            "rubric_kind": "rubric",
            "item_statuses": {k: v.value for k, v in item_statuses.items()},
            "pending_items": pending_items,
            "affects_stance": False,
            "countable": False,
        },
        used_sesoi=False,
    )


def evaluate_output_spec_rubrics(
    spec: Any,
    *,
    out: AgentTaskOutput,
    artifacts: Any,
    review_statuses: dict[str, ReqStatus],
    attempt_id: str | None,
    already: set[str],
    evaluate_code_check: Any | None = None,
    judge_retry: dict[str, int] | None = None,
    failure_kinds: dict[str, str] | None = None,
    task_id: str | None = None,
    output_digest: str | None = None,
) -> list[CheckResult]:
    """Evaluate each OutputSpec.rubric not already covered by a criterion.

    Freeze/eval errors propagate — never swallowed as UNASSESSED.
    """
    extra: list[CheckResult] = []
    try:
        expected_outputs = spec.expected_outputs
    except AttributeError:
        return extra
    if expected_outputs is None:
        return extra
    for o in expected_outputs:
        try:
            rub_raw = o.rubric
        except AttributeError:
            continue
        if rub_raw is None:
            continue
        rub = freeze_rubric(rub_raw)
        tid = f"rubric:{rub.digest}"
        if tid in already:
            continue
        check = CodeCheck(kind="rubric", output=o.name, rubric=rub, allow_network=False)
        cr = evaluate_rubric(
            check,
            template_id=tid,
            check_id=f"{tid}#out",
            out=out,
            artifacts=artifacts,
            role="validity",
            review_statuses=review_statuses,
            judge_retry=judge_retry,
            failure_kinds=failure_kinds,
            output_rubric=rub,
            attempt_id=attempt_id,
            task_id=task_id,
            output_digest=output_digest,
            evaluate_code_check=evaluate_code_check,
        )
        try:
            required_raw = o.required
        except AttributeError:
            required_v = True
        else:
            if type(required_raw) is not bool:
                raise JournalFormatError(f"OutputSpec.required must be bool, got {type(required_raw).__name__}")
            required_v = required_raw
        extra.append(
            CheckResult(
                check_id=cr.check_id,
                template_id=tid,
                status=cr.status,
                is_code=cr.is_code,
                required=required_v,
                role="validity",
                evidence=cr.evidence,
                detail=cr.detail,
                effect=cr.effect,
        used_sesoi=False,
    )
        )
    return extra
