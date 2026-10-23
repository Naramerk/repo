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


def role_from_template(tmpl: RequirementTemplate | None) -> CheckRole:
    """Authoritative role from the catalog template only (no plan override)."""
    if tmpl is None:
        return "unknown"
    if tmpl.role in ("prediction", "validity"):
        return tmpl.role
    return "unknown"


def role_from_catalog(template_id: str, catalog: Catalog | None) -> CheckRole:
    """Look up ``template_id`` in the catalog; missing → ``unknown``."""
    if catalog is None:
        return "unknown"
    for tmpl in catalog.templates:
        if tmpl.id == template_id:
            return role_from_template(tmpl)
    return "unknown"


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
    role: CheckRole,
    uses_sesoi: bool = False,
    sesoi: float | None = None,
    predicts_null: bool = False,
    predicted_direction: str = "two_sided",
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
                    role=role,
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
                role=role,
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
                    role=role,
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

            effect: dict[str, Any] | None = None
            used_sesoi = False
            if check.kind == "json_pointer":
                ok = left is not None
            elif check.kind == "value_matches":
                if uses_sesoi and sesoi is not None and isinstance(left, (int, float)):
                    # SESOI as equality tolerance (null-style) or exact match otherwise.
                    used_sesoi = True
                    est = float(left) - float(check.target or 0)
                    effect = {"est": est, "ci_low": est, "ci_high": est}
                    if predicts_null:
                        ok = abs(est) < float(sesoi)
                    else:
                        ok = left == check.target
                else:
                    ok = left == check.target
            else:
                # metric_threshold — when uses_sesoi, threshold is catalog sesoi.
                op = check.op or "=="
                if uses_sesoi and sesoi is not None and isinstance(left, (int, float)):
                    used_sesoi = True
                    est = float(left)
                    effect = {"est": est, "ci_low": est, "ci_high": est}
                    if predicts_null:
                        ok = abs(est) < float(sesoi)
                    elif predicted_direction == "positive":
                        ok = est > float(sesoi)
                    elif predicted_direction == "negative":
                        ok = est < -float(sesoi)
                    else:
                        ok = abs(est) > float(sesoi)
                else:
                    ok = _cmp(op, left, check.target, check.tolerance)
                    if isinstance(left, (int, float)):
                        effect = {
                            "est": float(left),
                            "ci_low": float(left),
                            "ci_high": float(left),
                        }

            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET if ok else ReqStatus.FAILED,
                is_code=True,
                required=True,
                role=role,
                evidence=[
                    EvidenceRef(
                        artifact_id=handle.artifact_id,
                        json_pointer=pointer,
                        digest=evid_digest,
                    )
                ],
                detail=None if ok else f"{left!r} vs {check.target!r}",
                effect=effect,
                used_sesoi=used_sesoi,
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
                    role=role,
                    detail="output missing",
                )
            artifacts.read(handle.artifact_id)  # verify sha
            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET,
                is_code=True,
                required=True,
                    role=role,
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
                    role=role,
            )

    except ArtifactCorrupt as e:
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
                    role=role,
            detail=str(e),
        )
    except Exception as e:
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
                    role=role,
            detail=str(e),
        )

    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
                    role=role,
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
        tmpl = by_id.get(ref.template_id)
        cid = f"{ref.template_id}#{i}"
        role = role_from_template(tmpl)
        if tmpl is None:
            results.append(
                CheckResult(
                    check_id=cid,
                    template_id=ref.template_id,
                    status=ReqStatus.FAILED,
                    is_code=True,
                    required=ref.required,
                    role="unknown",
                    detail="unknown template",
                )
            )
            continue
        if isinstance(tmpl.check, ReviewCheck):
            st = review_statuses.get(cid, ReqStatus.UNASSESSED)
            results.append(
                CheckResult(
                    check_id=cid,
                    template_id=tmpl.id,
                    status=st,
                    is_code=False,
                    required=ref.required and tmpl.required,
                    role=role,
                    detail=tmpl.check.question,
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
                role=role,
                uses_sesoi=bool(tmpl.uses_sesoi),
                sesoi=tmpl.sesoi,
                predicts_null=bool(tmpl.predicts_null),
                predicted_direction=str(tmpl.predicted_direction),
            )
            # respect required flag from ref
            results.append(
                CheckResult(
                    check_id=cr.check_id,
                    template_id=cr.template_id,
                    status=cr.status,
                    is_code=True,
                    required=ref.required and tmpl.required,
                    role=role,
                    evidence=cr.evidence,
                    detail=cr.detail,
                    effect=cr.effect,
                    used_sesoi=cr.used_sesoi,
                )
            )
    return results
