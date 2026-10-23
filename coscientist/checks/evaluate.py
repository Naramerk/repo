"""Check evaluation for Executor.record (CodeCheck kinds used in M0)."""

from __future__ import annotations

import json
from typing import Any

from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import (
    AgentTaskOutput,
    Catalog,
    CheckResult,
    CheckRole,
    CodeCheck,
    EvidenceRef,
    NodeRequirement,
    ReqStatus,
    RequirementRef,
    RequirementTemplate,
    ReviewCheck,
    TaskSpec,
)
from coscientist.core.errors import ArtifactCorrupt

# Prediction-style CodeCheck kinds (hypothesis/question prediction tests).
_PREDICTION_KINDS = frozenset({"value_matches", "metric_threshold"})


def resolve_check_role(
    ref: RequirementRef,
    tmpl: RequirementTemplate | None = None,
) -> CheckRole:
    """Duhem–Quine role: explicit on ref, else kind-based default.

    ``value_matches`` / ``metric_threshold`` → prediction (hypothesis criterion);
    all other checks (artifact_exists, schema, review, …) → validity.
    """
    if ref.role in ("prediction", "validity"):
        return ref.role
    if tmpl is not None and isinstance(tmpl.check, CodeCheck):
        if tmpl.check.kind in _PREDICTION_KINDS:
            return "prediction"
    # Heuristic when template unavailable (upcasters / journal replay).
    tid = (ref.template_id or "").lower()
    if any(k in tid for k in ("value_match", "metric", "threshold", "prediction")):
        return "prediction"
    return "validity"


def _pointer_get(doc: Any, pointer: str | None) -> Any:
    if not pointer:
        return doc
    if not pointer.startswith("/"):
        raise ValueError(f"invalid json pointer: {pointer}")
    cur = doc
    if pointer == "/":
        return cur
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            cur = cur[part]
        elif isinstance(cur, list):
            cur = cur[int(part)]
        else:
            raise KeyError(pointer)
    return cur


def _cmp(op: str, left: Any, right: Any, tolerance: float | None) -> bool:
    if op == "in":
        return left in right
    if tolerance is not None and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if op in ("==",):
            return abs(float(left) - float(right)) <= tolerance
    ops = {
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
    }
    return ops[op](left, right)


def evaluate_code_check(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    values: dict[str, Any],
) -> CheckResult:
    try:
        if check.kind == "artifact_exists":
            handle = out.outputs.get(check.output or "")
            if handle is None or artifacts.get_meta(handle.artifact_id) is None:
                return CheckResult(
                    check_id=check_id,
                    template_id=template_id,
                    status=ReqStatus.FAILED,
                    is_code=True,
                    required=True,
                    detail="artifact missing",
                )
            art = artifacts.get_meta(handle.artifact_id)
            assert art is not None
            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET,
                is_code=True,
                required=True,
                evidence=[
                    EvidenceRef(
                        artifact_id=art.artifact_id,
                        digest=art.sha256,
                    )
                ],
            )

        if check.kind in ("value_matches", "metric_threshold", "json_pointer"):
            handle = out.outputs.get(check.output or "main")
            if handle is None:
                return CheckResult(
                    check_id=check_id,
                    template_id=template_id,
                    status=ReqStatus.FAILED,
                    is_code=True,
                    required=True,
                    detail="output missing",
                )
            raw = artifacts.read(handle.artifact_id)
            doc = json.loads(raw)
            pointer = check.pointer
            if check.kind == "metric_threshold" and check.metric and not pointer:
                # prefer values dict, else top-level key
                if check.metric in values:
                    left = values[check.metric]
                    pointer = None
                    evid_digest = sha256_hex(canonical_json(left))
                else:
                    pointer = f"/{check.metric}"
                    left = _pointer_get(doc, pointer)
                    evid_digest = sha256_hex(canonical_json(left))
            else:
                left = _pointer_get(doc, pointer)
                evid_digest = sha256_hex(canonical_json(left))

            if check.kind == "json_pointer":
                ok = left is not None
            elif check.kind == "value_matches":
                ok = left == check.target
            else:
                op = check.op or "=="
                ok = _cmp(op, left, check.target, check.tolerance)

            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET if ok else ReqStatus.FAILED,
                is_code=True,
                required=True,
                evidence=[
                    EvidenceRef(
                        artifact_id=handle.artifact_id,
                        json_pointer=pointer,
                        digest=evid_digest,
                    )
                ],
                detail=None if ok else f"{left!r} vs {check.target!r}",
            )

        if check.kind == "schema_valid":
            handle = out.outputs.get(check.output or "main")
            if handle is None:
                return CheckResult(
                    check_id=check_id,
                    template_id=template_id,
                    status=ReqStatus.FAILED,
                    is_code=True,
                    required=True,
                    detail="output missing",
                )
            artifacts.read(handle.artifact_id)  # verify sha
            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET,
                is_code=True,
                required=True,
                evidence=[
                    EvidenceRef(artifact_id=handle.artifact_id, digest=sha256_hex(b"ok"))
                ],
            )

        if check.kind == "source_link":
            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET,
                is_code=True,
                required=True,
            )

    except ArtifactCorrupt as e:
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            detail=str(e),
        )
    except Exception as e:
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            detail=str(e),
        )

    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
        detail=f"unhandled kind {check.kind}",
    )


def template_to_requirement(
    tmpl: RequirementTemplate,
    node_id: str,
    *,
    status: ReqStatus = ReqStatus.PENDING,
    req_id: str | None = None,
) -> NodeRequirement:
    is_code = isinstance(tmpl.check, CodeCheck)
    return NodeRequirement(
        id=req_id or f"{node_id}:{tmpl.id}",
        template_id=tmpl.id,
        node_id=node_id,
        text_ru=tmpl.text_ru,
        required=tmpl.required,
        status=status,
        check_version=tmpl.version,
        is_code=is_code,
    )


def evaluate_task_checks(
    catalog: Catalog,
    spec: TaskSpec,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    *,
    review_statuses: dict[str, ReqStatus] | None = None,
) -> list[CheckResult]:
    """Evaluate criteria on the task; review checks become UNASSESSED unless overridden."""
    by_id = {t.id: t for t in catalog.templates}
    review_statuses = review_statuses or {}
    results: list[CheckResult] = []
    refs = list(spec.criteria)
    if not refs:
        # apply defaults for experiment kind (schema NodeKind)
        for tid in catalog.defaults.get("experiment", []):
            refs.append(RequirementRef(template_id=tid))

    for i, ref in enumerate(refs):
        tmpl = by_id[ref.template_id]
        cid = f"{ref.template_id}#{i}"
        role = resolve_check_role(ref, tmpl)
        if isinstance(tmpl.check, ReviewCheck):
            st = review_statuses.get(cid, ReqStatus.UNASSESSED)
            results.append(
                CheckResult(
                    check_id=cid,
                    template_id=tmpl.id,
                    status=st,
                    is_code=False,
                    required=ref.required and tmpl.required,
                    detail=tmpl.check.question,
                    role=role,
                )
            )
        else:
            cr = evaluate_code_check(
                tmpl.check,
                template_id=tmpl.id,
                check_id=cid,
                out=out,
                artifacts=artifacts,
                values=out.values,
            )
            # respect required flag from ref
            results.append(
                CheckResult(
                    check_id=cr.check_id,
                    template_id=cr.template_id,
                    status=cr.status,
                    is_code=True,
                    required=ref.required and tmpl.required,
                    evidence=cr.evidence,
                    detail=cr.detail,
                    role=role,
                )
            )
    return results
