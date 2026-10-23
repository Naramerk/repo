"""Evaluate CodeCheck kind ``rubric`` (M1 PR13 / DESIGN_M1 §6.2).

Items are atomic and binary. ``check=code`` delegates to a nested CodeCheck
kind (never ``rubric``). ``check=judge`` uses review_statuses; judge failure
→ UNASSESSED (R23) + caller may retry once — never FAILED for the judge item.

Aggregate status never feeds claim stance (role must be validity).
"""

from __future__ import annotations

from typing import Any

from coscientist.core.domain import AgentTaskOutput, CheckResult, CodeCheck, ReqStatus
from coscientist.core.rubric import aggregate_rubric_status, freeze_rubric
from coscientist.core.rubric_types import Rubric


def _as_rubric(raw: Any) -> Rubric:
    if isinstance(raw, Rubric):
        return freeze_rubric(raw)
    return freeze_rubric(Rubric.model_validate(raw))


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
    output_rubric: Any | None = None,
    attempt_id: str | None = None,
    evaluate_code_check: Any | None = None,
) -> CheckResult:
    """Evaluate a rubric check; never raises into the executor."""
    try:
        return _evaluate(
            check,
            template_id=template_id,
            check_id=check_id,
            out=out,
            artifacts=artifacts,
            role=role,
            review_statuses=review_statuses or {},
            judge_retry=judge_retry or {},
            output_rubric=output_rubric,
            attempt_id=attempt_id,
            evaluate_code_check=evaluate_code_check,
        )
    except Exception as e:  # noqa: BLE001 — totality
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.UNASSESSED,
            is_code=False,
            required=True,
            role="validity",  # never prediction — stance invariant
            detail=f"rubric_eval_error:{type(e).__name__}:{e}",
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
    output_rubric: Any | None,
    attempt_id: str | None,
    evaluate_code_check: Any | None,
) -> CheckResult:
    raw = check.rubric if check.rubric is not None else output_rubric
    if raw is None:
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            role="validity",
            detail="rubric_missing",
        )
    rubric = _as_rubric(raw)
    item_statuses: dict[str, ReqStatus] = {}
    details: list[str] = []
    has_judge = False
    has_code = False

    for item in rubric.items:
        item_key = f"{check_id}:{item.id}"
        if item.check == "judge":
            has_judge = True
            st = review_statuses.get(item_key)
            if st is None:
                st = review_statuses.get(item.id)
            if st is None:
                # Judge not yet answered → UNASSESSED (R23); retry counter.
                retries = int(judge_retry.get(item_key, 0))
                st = ReqStatus.UNASSESSED
                details.append(f"{item.id}:judge_unassessed:retry={retries}")
            elif st == ReqStatus.FAILED:
                # Judge failure → UNASSESSED + one retry, never FAILED (design §6.2).
                retries = int(judge_retry.get(item_key, 0))
                if retries < 1:
                    st = ReqStatus.UNASSESSED
                    details.append(f"{item.id}:judge_failure_unassessed:retry=1")
                else:
                    st = ReqStatus.UNASSESSED
                    details.append(f"{item.id}:judge_failure_exhausted")
            else:
                details.append(f"{item.id}:judge:{st.value}")
            item_statuses[item.id] = st
            continue

        # code item
        has_code = True
        kind = item.code_kind or "artifact_exists"
        if kind == "rubric":
            item_statuses[item.id] = ReqStatus.FAILED
            details.append(f"{item.id}:nested_rubric_forbidden")
            continue
        nested = CodeCheck(kind=kind, output=check.output or "main")  # type: ignore[arg-type]
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
        )
        item_statuses[item.id] = cr.status
        details.append(f"{item.id}:code:{cr.status.value}:{cr.detail or ''}")

    overall = aggregate_rubric_status(item_statuses, rubric)
    # Mixed judge+code: overall UNASSESSED is not "code" for outcome B2b.
    is_code = has_code and not has_judge
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=overall,
        is_code=is_code,
        required=True,
        role="validity",  # force validity — never feeds claim stance
        detail=f"rubric:{rubric.digest};" + ";".join(details),
        effect={
            "rubric_digest": rubric.digest,
            "item_statuses": {k: v.value for k, v in item_statuses.items()},
            # Explicit: rubric must not count toward claim stance / k.
            "affects_stance": False,
            "countable": False,
        },
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
) -> list[CheckResult]:
    """Evaluate each OutputSpec.rubric not already covered by a criterion."""
    extra: list[CheckResult] = []
    for o in getattr(spec, "expected_outputs", None) or []:
        if getattr(o, "rubric", None) is None:
            continue
        try:
            rub = freeze_rubric(o.rubric)
        except Exception as e:  # noqa: BLE001
            extra.append(
                CheckResult(
                    check_id=f"rubric:{o.name}",
                    template_id=f"rubric:{o.name}",
                    status=ReqStatus.UNASSESSED,
                    is_code=False,
                    required=bool(getattr(o, "required", True)),
                    role="validity",
                    detail=f"rubric_freeze_error:{type(e).__name__}",
                )
            )
            continue
        tid = f"rubric:{rub.digest}"
        if any(tid == a or rub.digest in a for a in already):
            continue
        check = CodeCheck(kind="rubric", output=o.name, rubric=rub)
        cr = evaluate_rubric(
            check,
            template_id=tid,
            check_id=f"{tid}#out",
            out=out,
            artifacts=artifacts,
            role="validity",
            review_statuses=review_statuses,
            output_rubric=rub,
            attempt_id=attempt_id,
            evaluate_code_check=evaluate_code_check,
        )
        extra.append(
            CheckResult(
                check_id=cr.check_id,
                template_id=tid,
                status=cr.status,
                is_code=cr.is_code,
                required=bool(getattr(o, "required", True)),
                role="validity",
                evidence=cr.evidence,
                detail=cr.detail,
                effect=cr.effect,
            )
        )
    return extra
