"""Check evaluation for Executor.record (CodeCheck kinds used in M0)."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import Any

from coscientist.core.artifacts import (
    LocalArtifactStore,
    artifact_producer_trusted,
)
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
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
        # quote_in_source indeterminate (missing / unreadable payload)
        "quote_missing",
        "quote_payload_invalid",
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
    # Fail-closed: unknown / future producers collapse to agent (untrusted).
    # ``user`` is explicit — never trusted, never countable via this helper alone.
    if producer == "user":
        pass
    elif producer not in ("agent", "core", "sandbox", "tool"):
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
    fetched_digests: frozenset[str] | Collection[str] | None = None,
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
                    if check.kind == "json_pointer" and (pointer is None or pointer == "" or pointer == "/"):
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
            prov = _effect_provenance_fields(artifacts, handle.artifact_id, attempt_id=attempt_id)
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
                if uses_sesoi and sesoi is not None and predicts_null:
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
                        ok = (-float(sesoi)) < ci["ci_low"] and ci["ci_high"] < float(sesoi)
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
                evidence=[EvidenceRef(artifact_id=handle.artifact_id, digest=sha256_hex(b"ok"))],
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

        if check.kind == "quote_in_source":
            from coscientist.checks.kinds.quote import evaluate_quote_in_source

            return evaluate_quote_in_source(
                check,
                template_id=template_id,
                check_id=check_id,
                out=out,
                artifacts=artifacts,
                role=role,
                attempt_id=attempt_id,
                fetched_digests=fetched_digests,
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

        if check.kind == "rubric":
            from coscientist.checks.kinds.rubric import evaluate_rubric

            # Role forced to validity inside evaluate_rubric (stance invariant).
            return evaluate_rubric(
                check,
                template_id=template_id,
                check_id=check_id,
                out=out,
                artifacts=artifacts,
                role=role,
                attempt_id=attempt_id,
                evaluate_code_check=evaluate_code_check,
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
            raise SandboxInfraError(f"sandbox_unavailable:host_io:{e.__class__.__name__}") from e
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


def agent_facing_template_view(tmpl: RequirementTemplate) -> dict[str, Any]:
    """Fields safe to show an agent — never the full CodeCheck (holdout/scorer/tests).

    This is the **only** agent-facing serialization of a catalog template.
    Prompt renderers (arriving later) must call this — never
    ``RequirementTemplate.model_dump`` / full ``CodeCheck``.
    """
    return {
        "template_id": tmpl.id,
        "text_ru": tmpl.text_ru,
        "version": tmpl.version,
        "required": tmpl.required,
        "role": tmpl.role,
        "is_code": isinstance(tmpl.check, CodeCheck),
        "check_kind": getattr(tmpl.check, "kind", None) or getattr(tmpl.check, "reviewer_role", None),
    }


def agent_facing_catalog_view(catalog: Catalog) -> dict[str, Any]:
    """Agent-safe catalog projection — templates via ``agent_facing_template_view`` only."""
    return {
        "catalog_version": catalog.catalog_version,
        "edition": catalog.edition,
        "templates": [agent_facing_template_view(t) for t in catalog.templates],
        "defaults": {k: list(v) for k, v in (catalog.defaults or {}).items()},
        "code_output_names": list(catalog.code_output_names or []),
    }


def template_to_requirement(
    tmpl: RequirementTemplate,
    node_id: str,
    *,
    status: ReqStatus = ReqStatus.PENDING,
    req_id: str | None = None,
) -> NodeRequirement:
    """Build a NodeRequirement from the agent-facing template view (no check body)."""
    view = agent_facing_template_view(tmpl)
    return NodeRequirement(
        id=req_id or f"{node_id}:{view['template_id']}",
        template_id=str(view["template_id"]),
        node_id=node_id,
        text_ru=str(view["text_ru"] or ""),
        required=bool(view["required"]),
        status=status,
        check_version=str(view["version"]),
        is_code=bool(view["is_code"]),
    )


def _evaluate_proposed_check(
    *,
    ref: RequirementRef,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    proposals: dict[str, dict[str, Any]],
    attempt_id: str | None,
    task_id: str | None,
    output_digest: str | None,
    review_statuses: dict[str, ReqStatus],
    judge_retry: dict[str, int],
    failure_kinds: dict[str, str],
    fetched_digests: frozenset[str] | Collection[str] | None,
    spec: TaskSpec,
    attempts: Mapping[str, Mapping[str, Any]] | None,
    results: Mapping[str, Mapping[str, Any]] | None,
) -> CheckResult:
    """Evaluate the ``proposed_check`` meta-template against a folded proposal.

    Unapproved / missing → PENDING (await HITL) or FAILED after reject.
    Claims never count until ``CheckApproved`` (exclusion ``unapproved_check``).
    """
    from coscientist.core.proposed_check import (
        assert_proposal_preregistered_for_eval,
        code_check_from_proposal,
        rubric_from_proposal,
    )

    params = ref.params if isinstance(ref.params, dict) else {}
    pid = str(params.get("proposal_id") or "")
    base_effect: dict[str, Any] = {
        "proposed_check": True,
        "proposal_id": pid,
        "core_evaluated": True,
        "countable": False,
        "trusted": False,
    }
    if not pid:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=True,
            required=ref.required,
            role="unknown",
            detail="unapproved_check:missing_proposal_id",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )
    row = proposals.get(pid)
    if row is None:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=True,
            required=ref.required,
            role="unknown",
            detail="unapproved_check:unknown_proposal",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )
    status = str(row.get("status") or "proposed")
    role: CheckRole = row.get("role") if row.get("role") in ("prediction", "validity") else "unknown"  # type: ignore[assignment]
    base_effect["proposal_digest"] = row.get("proposal_digest")
    base_effect["delegated"] = bool(row.get("delegated"))
    base_effect["bound_task_id"] = row.get("bound_task_id") or row.get("task_id")
    base_effect["bound_target"] = row.get("bound_target") or row.get("target")
    eval_target = params.get("target")
    if eval_target is None:
        eval_target = row.get("bound_target") or row.get("target")
    prereg_err = assert_proposal_preregistered_for_eval(
        row,
        attempt_id=attempt_id,
        task_id=task_id,
        target=str(eval_target) if eval_target is not None else None,
        attempts=attempts,
        results=results,
    )
    if prereg_err:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=row.get("check") is not None,
            required=ref.required,
            role=role,
            detail=prereg_err,
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )
    if status == "rejected":
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=row.get("check") is not None,
            required=ref.required,
            role=role,
            detail=f"unapproved_check:rejected:{row.get('reject_reason') or 'rejected'}",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )
    if status != "approved":
        # Awaiting HITL / reviewer — required checks keep the attempt open.
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.PENDING,
            is_code=row.get("check") is not None,
            required=ref.required,
            role=role,
            detail="unapproved_check:pending_approval",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )

    # Approved: evaluate using the preregistered body (never mutate after propose).
    # Bound approval: one task_id + target only (no multi-claim k_sup reuse).
    bound_task = str(row.get("bound_task_id") or row.get("task_id") or "")
    if task_id and bound_task and str(task_id) != bound_task:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=row.get("check") is not None,
            required=ref.required,
            role=role,
            detail=f"unapproved_check:task_mismatch:{bound_task}!={task_id}",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )
    delegated = bool(row.get("delegated"))
    if row.get("rubric") is not None:
        from coscientist.checks.kinds.rubric import evaluate_rubric
        from coscientist.core.domain import CodeCheck as CC

        rub = rubric_from_proposal(row)
        # Qualitative proposed rubric — never severe (DESIGN_M1 §16).
        sentinel = CC(kind="rubric", output="main", rubric=rub)
        cr = evaluate_rubric(
            sentinel,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            check_id=check_id,
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
        effect = dict(cr.effect or {})
        effect.update(
            {
                "proposed_check": True,
                "proposal_id": pid,
                "proposal_digest": row.get("proposal_digest"),
                "delegated": delegated,
                "qualitative": True,
                "never_severe": True,
            }
        )
        # Rubric proposed checks are never severe / never tip trusted severity.
        effect["trusted"] = False
        return CheckResult(
            check_id=cr.check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=cr.status,
            is_code=False,
            required=ref.required,
            role="validity",
            evidence=cr.evidence,
            detail=cr.detail,
            effect=effect,
            used_sesoi=False,
        )

    check = code_check_from_proposal(row)
    if check is None:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=True,
            required=ref.required,
            role=role,
            detail="unapproved_check:empty_body",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        )
    from coscientist.checks.sandbox_limits import apply_frozen_limits

    check = apply_frozen_limits(check, spec, template_id=PROPOSED_CHECK_TEMPLATE_ID)
    cr = evaluate_code_check(
        check,
        template_id=PROPOSED_CHECK_TEMPLATE_ID,
        check_id=check_id,
        out=out,
        artifacts=artifacts,
        values=out.values,
        role=role,
        uses_sesoi=bool(row.get("uses_sesoi")),
        sesoi=row.get("sesoi") if row.get("sesoi") is not None else row.get("threshold"),
        predicts_null=bool(row.get("predicts_null")),
        predicted_direction=str(row.get("predicted_direction") or "two_sided"),
        attempt_id=attempt_id,
        fetched_digests=fetched_digests,
    )
    effect = dict(cr.effect or {})
    effect.update(
        {
            "proposed_check": True,
            "proposal_id": pid,
            "proposal_digest": row.get("proposal_digest"),
            "delegated": delegated,
        }
    )
    # Severe only when trusted (sandbox hard isolation) — DESIGN_M1 §16.
    if not effect.get("trusted"):
        effect["severe_eligible"] = False
    return CheckResult(
        check_id=cr.check_id,
        template_id=PROPOSED_CHECK_TEMPLATE_ID,
        status=cr.status,
        is_code=True,
        required=ref.required,
        role=role,
        evidence=cr.evidence,
        detail=cr.detail,
        effect=effect,
        used_sesoi=cr.used_sesoi,
    )


def evaluate_task_checks(
    catalog: Catalog,
    spec: TaskSpec,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    *,
    review_statuses: dict[str, ReqStatus] | None = None,
    judge_retry: dict[str, int] | None = None,
    failure_kinds: dict[str, str] | None = None,
    attempt_id: str | None = None,
    task_id: str | None = None,
    output_digest: str | None = None,
    task_kind: str | None = None,
    task_key: str | None = None,
    fetched_digests: frozenset[str] | Collection[str] | None = None,
    proposals: dict[str, dict[str, Any]] | None = None,
    attempts: Mapping[str, Mapping[str, Any]] | None = None,
    results_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[CheckResult]:
    """Evaluate criteria on the task; review checks become UNASSESSED unless overridden.

    Joint with PR14: ``fetched_digests`` forwarded to executable kinds.
    PR13: ``judge_retry`` / ``failure_kinds`` / ``task_id`` / ``output_digest``.
    PR20: ``proposals`` — folded CheckProposed/Approved/Rejected rows for
    ``proposed_check`` meta-template evaluation (core decides countability).
    ``attempts`` / ``results_by_id`` are required for fail-closed prereg checks
    (CheckProposed.seq < AttemptStarted._seq and < ResultRecorded._seq).
    """
    by_id = {t.id: t for t in catalog.templates}
    review_statuses = review_statuses or {}
    judge_retry = judge_retry or {}
    failure_kinds = failure_kinds or {}
    proposals = proposals or {}
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
        if str(ref.template_id) == PROPOSED_CHECK_TEMPLATE_ID:
            results.append(
                _evaluate_proposed_check(
                    ref=ref,
                    check_id=cid,
                    out=out,
                    artifacts=artifacts,
                    proposals=proposals,
                    attempt_id=attempt_id,
                    task_id=task_id,
                    output_digest=output_digest,
                    review_statuses=review_statuses,
                    judge_retry=judge_retry,
                    failure_kinds=failure_kinds,
                    fetched_digests=fetched_digests,
                    spec=spec,
                    attempts=attempts,
                    results=results_by_id,
                )
            )
            continue
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
        check: CodeCheck | ReviewCheck
        if isinstance(tmpl.check, CodeCheck):
            from coscientist.checks.sandbox_limits import apply_frozen_limits

            check = apply_frozen_limits(tmpl.check, spec, template_id=tmpl.id)
        else:
            check = tmpl.check
        if isinstance(check, ReviewCheck):
            from coscientist.core.reviews import review_key

            # Scoped key only — never bare check_id / empty-field substitution.
            if not task_id or not attempt_id or not output_digest:
                st = ReqStatus.UNASSESSED
            else:
                rkey = review_key(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    check_id=cid,
                    item_id="",
                    output_digest=output_digest,
                )
                st = review_statuses.get(rkey, ReqStatus.UNASSESSED)
            results.append(
                CheckResult(
                    check_id=cid,
                    template_id=tmpl.id,
                    status=st,
                    is_code=False,
                    required=ref.required and tmpl.required,
                    role=role,
                    detail=check.question,
                )
            )
        elif isinstance(check, CodeCheck) and check.kind == "rubric":
            from coscientist.checks.kinds.rubric import evaluate_rubric
            from coscientist.core.rubric import freeze_rubric

            # Prefer OutputSpec.rubric for the named output (plan-frozen).
            out_rubric = None
            for o in spec.expected_outputs or []:
                if o.name == (check.output or "main") and o.rubric is not None:
                    out_rubric = freeze_rubric(o.rubric)
                    break
            cr = evaluate_rubric(
                check,
                template_id=tmpl.id,
                check_id=cid,
                out=out,
                artifacts=artifacts,
                role="validity",
                review_statuses=review_statuses,
                judge_retry=judge_retry,
                failure_kinds=failure_kinds,
                output_rubric=out_rubric,
                attempt_id=attempt_id,
                task_id=task_id,
                output_digest=output_digest,
                evaluate_code_check=evaluate_code_check,
            )
            results.append(
                CheckResult(
                    check_id=cr.check_id,
                    template_id=cr.template_id,
                    status=cr.status,
                    is_code=cr.is_code,
                    required=ref.required and tmpl.required,
                    role="validity",
                    evidence=cr.evidence,
                    detail=cr.detail,
                    effect=cr.effect,
                    used_sesoi=False,
                )
            )
        else:
            cr = evaluate_code_check(
                check,  # type: ignore[arg-type]  # CodeCheck after ReviewCheck branch
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
                fetched_digests=fetched_digests,
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

    # Plan-frozen OutputSpec.rubric without an explicit rubric criterion still runs
    # (artifact deliverables: literature review, design doc, …).
    from coscientist.checks.kinds.rubric import evaluate_output_spec_rubrics

    already: set[str] = {r.template_id for r in results}
    for r in results:
        effect = r.effect if isinstance(r.effect, dict) else {}
        dig = effect.get("rubric_digest")
        if dig:
            already.add(f"rubric:{dig}")
    results.extend(
        evaluate_output_spec_rubrics(
            spec,
            out=out,
            artifacts=artifacts,
            review_statuses=review_statuses,
            attempt_id=attempt_id,
            already=already,
            evaluate_code_check=evaluate_code_check,
            judge_retry=judge_retry,
            failure_kinds=failure_kinds,
            task_id=task_id,
            output_digest=output_digest,
        )
    )
    return results
