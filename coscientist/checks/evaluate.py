"""Check evaluation for Executor.record (CodeCheck kinds used in M0)."""

from __future__ import annotations

import json
from typing import Any

from coscientist.core.artifacts import (
    LocalArtifactStore,
    artifact_producer_trusted,
)
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
from coscientist.core.errors import (
    ArtifactCorrupt,
    CatalogError,
    CoreBugError,
    SandboxInfraError,
)


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


def artifact_belongs_to_attempt(meta: Any | None, attempt_id: str | None) -> bool:
    """True when ``Artifact.produced_by.call_id`` is bound to ``attempt_id``."""
    if meta is None or not attempt_id:
        return False
    produced = getattr(meta, "produced_by", None)
    call_id = str(getattr(produced, "call_id", "") or "")
    if not call_id:
        return False
    return call_id == attempt_id or call_id.startswith(f"{attempt_id}:")


# Stable reasons for checks with no numeric effect (indeterminate — never refute).
NO_EFFECT_REASONS = frozenset(
    {
        "foreign_artifact",
        "output_not_submitted",
        "artifact_missing",
        "pointer_empty",
    }
)


def _no_effect_result(
    *,
    check_id: str,
    template_id: str,
    role: CheckRole,
    reason: str,
) -> CheckResult:
    """FAILED prediction without effect → classify as indeterminate (safe vs main)."""
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
        role=role,
        detail=reason,
        effect=None,
        used_sesoi=False,
    )


def _effect_provenance_fields(
    artifacts: LocalArtifactStore,
    artifact_id: str | None,
    *,
    attempt_id: str | None,
) -> dict[str, Any]:
    """Core-stamped producer/trusted/countable from artifact meta (agent cannot set).

    ``trusted`` = producer core, or sandbox with both net and fs isolation hard.
    ``countable`` = core catalog evaluation on an artifact bound to this attempt.
    """
    meta = artifacts.get_meta(artifact_id) if artifact_id else None
    producer = getattr(meta, "producer", "agent") if meta is not None else "agent"
    if producer not in ("agent", "core", "sandbox"):
        producer = "agent"
    produced = getattr(meta, "produced_by", None) if meta is not None else None
    call_id = str(getattr(produced, "call_id", "") or "")
    belongs = artifact_belongs_to_attempt(meta, attempt_id)
    out: dict[str, Any] = {
        "producer": producer,
        "trusted": artifact_producer_trusted(meta),
        "core_evaluated": True,
        "produced_by_call_id": call_id,
        "artifact_id": str(artifact_id or ""),
        # Countable only when the artifact was produced for this attempt.
        "countable": bool(belongs),
    }
    if meta is not None and producer == "sandbox":
        ni = getattr(meta, "net_isolation", None)
        fs = getattr(meta, "fs_isolation", None)
        if ni in ("hard", "soft", "none"):
            out["net_isolation"] = ni
        if fs in ("hard", "soft", "none"):
            out["fs_isolation"] = fs
    return out


def _extract_ci(doc: Any, left: Any) -> dict[str, float] | None:
    """Read a real CI from the metric document (ci_low < ci_high).

    Accepts ``{est, ci_low, ci_high}`` at the document root, under ``effect``,
    or when ``left`` itself is that mapping. Point estimates (missing CI or
    ``ci_low >= ci_high``) return None — never invent CI from a point.
    """
    candidates: list[Any] = []
    if isinstance(left, dict):
        candidates.append(left)
    if isinstance(doc, dict):
        candidates.append(doc)
        if isinstance(doc.get("effect"), dict):
            candidates.append(doc["effect"])
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        if "ci_low" not in raw or "ci_high" not in raw:
            continue
        try:
            lo = float(raw["ci_low"])
            hi = float(raw["ci_high"])
            est = float(raw["est"]) if raw.get("est") is not None else (lo + hi) / 2.0
        except (TypeError, ValueError):
            continue
        if lo < hi:
            return {"est": est, "ci_low": lo, "ci_high": hi}
    return None


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
    attempt_id: str | None = None,
) -> CheckResult:
    try:
        if check.kind == "artifact_exists":
            handle = out.outputs.get(check.output or "")
            if handle is None:
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="output_not_submitted",
                )
            if artifacts.get_meta(handle.artifact_id) is None:
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="artifact_missing",
                )
            art = artifacts.get_meta(handle.artifact_id)
            assert art is not None
            if attempt_id and not artifact_belongs_to_attempt(art, attempt_id):
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="foreign_artifact",
                )
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
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="output_not_submitted",
                )
            meta = artifacts.get_meta(handle.artifact_id)
            if meta is None:
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="artifact_missing",
                )
            if attempt_id and not artifact_belongs_to_attempt(meta, attempt_id):
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="foreign_artifact",
                )
            raw = artifacts.read(handle.artifact_id)
            doc = json.loads(raw)
            pointer = check.pointer
            try:
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
                    if check.kind == "json_pointer" and (
                        pointer is None or pointer == "" or pointer == "/"
                    ):
                        return _no_effect_result(
                            check_id=check_id,
                            template_id=template_id,
                            role=role,
                            reason="pointer_empty",
                        )
                    left = _pointer_get(doc, pointer)
                    evid_digest = sha256_hex(canonical_json(left))
            except (KeyError, IndexError, TypeError, ValueError):
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="pointer_empty",
                )

            effect: dict[str, Any] | None = None
            used_sesoi = False
            prov = _effect_provenance_fields(
                artifacts, handle.artifact_id, attempt_id=attempt_id
            )
            if check.kind == "json_pointer":
                # Missing / null / empty-string at pointer → no numeric effect.
                if left is None or left == "":
                    return _no_effect_result(
                        check_id=check_id,
                        template_id=template_id,
                        role=role,
                        reason="pointer_empty",
                    )
                ok = True
            elif check.kind == "value_matches":
                # Exact equality is a point check — never TOST, never used_sesoi.
                ok = left == check.target
                if isinstance(left, (int, float)):
                    est = float(left)
                    effect = {"est": est, "kind": "point", **prov}
            else:
                # metric_threshold — point-estimate exceedance or TOST with real CI.
                op = check.op or "=="
                ci = _extract_ci(doc, left)
                if (
                    uses_sesoi
                    and sesoi is not None
                    and predicts_null
                ):
                    # TOST: require a real interval from the metric artifact.
                    if ci is not None:
                        effect = {
                            "est": ci["est"],
                            "ci_low": ci["ci_low"],
                            "ci_high": ci["ci_high"],
                            "kind": "interval",
                            **prov,
                        }
                        used_sesoi = True
                        ok = (-float(sesoi)) < ci["ci_low"] and ci["ci_high"] < float(
                            sesoi
                        )
                    else:
                        # No CI → not a TOST result; equivalence not assessable.
                        used_sesoi = False
                        effect = None
                        ok = False
                elif uses_sesoi and sesoi is not None and isinstance(left, (int, float)):
                    # Honest point-estimate check (not TOST).
                    used_sesoi = True
                    est = float(left)
                    effect = {"est": est, "kind": "point", **prov}
                    if predicted_direction == "positive":
                        ok = est > float(sesoi)
                    elif predicted_direction == "negative":
                        ok = est < -float(sesoi)
                    else:
                        ok = abs(est) > float(sesoi)
                else:
                    ok = _cmp(op, left, check.target, check.tolerance)
                    if isinstance(left, (int, float)):
                        effect = {"est": float(left), "kind": "point", **prov}

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
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="output_not_submitted",
                )
            meta = artifacts.get_meta(handle.artifact_id)
            if meta is None:
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="artifact_missing",
                )
            if attempt_id and not artifact_belongs_to_attempt(meta, attempt_id):
                return _no_effect_result(
                    check_id=check_id,
                    template_id=template_id,
                    role=role,
                    reason="foreign_artifact",
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

        if check.kind in (
            "exec_runs",
            "tests_pass",
            "metric_on_holdout",
            "reproducible_rerun",
        ):
            from coscientist.checks.kinds.executable import evaluate_executable

            # Executable kinds stamp binding/trusted themselves — do not pass
            # empty provenance (artifact_id="") that would overwrite countable.
            return evaluate_executable(
                check,
                template_id=template_id,
                check_id=check_id,
                out=out,
                artifacts=artifacts,
                role=role,
                attempt_id=attempt_id,
                prov_fields={},
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
    except SandboxInfraError:
        raise
    except CoreBugError:
        raise
    except CatalogError:
        # Catalog integrity (e.g. holdout_digest mismatch) — not a core bug.
        raise
    except OSError as e:
        # Host I/O outside Popen on executable kinds → infra (B3).
        if check.kind in (
            "exec_runs",
            "tests_pass",
            "metric_on_holdout",
            "reproducible_rerun",
        ):
            raise SandboxInfraError(
                f"sandbox_unavailable:host_io:{e.__class__.__name__}"
            ) from e
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
        # Unexpected core bugs: neither infra nor scientific (B3).
        if check.kind in (
            "exec_runs",
            "tests_pass",
            "metric_on_holdout",
            "reproducible_rerun",
        ):
            raise CoreBugError(f"core_bug:{e.__class__.__name__}:{e}") from e
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
    attempt_id: str | None = None,
    task_kind: str | None = None,
    task_key: str | None = None,
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

    # plan_expansion_* markers are valid ONLY on core-created PLAN-n containers.
    key = task_key if task_key is not None else spec.task_key
    kind = str(task_kind or "")
    is_core_plan_n = kind == "plan" and str(key).startswith("PLAN-")

    for i, ref in enumerate(refs):
        tmpl = by_id.get(ref.template_id)
        cid = f"{ref.template_id}#{i}"
        role = role_from_template(tmpl)
        if str(ref.template_id or "").startswith("plan_expansion_"):
            if is_core_plan_n:
                results.append(
                    CheckResult(
                        check_id=cid,
                        template_id=ref.template_id,
                        status=ReqStatus.MET,
                        is_code=True,
                        required=ref.required,
                        role="validity",
                        detail="m1_deterministic_critic",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        check_id=cid,
                        template_id=ref.template_id,
                        status=ReqStatus.FAILED,
                        is_code=True,
                        required=ref.required,
                        role="unknown",
                        detail="invalid_plan_expansion_marker",
                    )
                )
            continue
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
                attempt_id=attempt_id,
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
