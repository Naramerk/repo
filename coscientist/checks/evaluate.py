"""Check evaluation for Executor.record (CodeCheck kinds used in M0)."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import Any

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import (
    ArtifactCorrupt,
    CatalogError,
    CoreBugError,
    JournalFormatError,
    SandboxInfraError,
)
from coscientist.core.execution.artifacts import (
    LocalArtifactStore,
    artifact_producer_trusted,
)
from coscientist.core.execution.domain import (
    PROPOSED_CHECK_TEMPLATE_ID,
    AgentTaskOutput,
    Catalog,
    CheckResult,
    CheckRole,
    CodeCheck,
    EvidenceRef,
    NodeRequirement,
    PlanRole,
    ReqStatus,
    RequirementRef,
    RequirementTemplate,
    ReviewCheck,
    TaskSpec,
    TemplateKind,
)


def _as_float(value: object, *, what: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise JournalFormatError(f"{what} must be numeric, got {type(value).__name__}")
    return float(value)  # audit-ok[coerce_float]: coerce after explicit numeric type gate


def _as_bool(value: object, *, what: str) -> bool:
    if type(value) is not bool:
        raise JournalFormatError(f"{what} must be bool, got {type(value).__name__}")
    return value


def _as_str(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise JournalFormatError(f"{what} must be str, got {type(value).__name__}")
    return value


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
    if not pointer.startswith("/"):  # audit-ok[str_startswith]: RFC/allowlist layout prefix (not name heuristic)
        raise ValueError(f"invalid json pointer: {pointer}")
    cur = doc
    if pointer == "/":
        return cur
    for part in pointer[1:].split("/"):  # audit-ok[str_split]: structural id/key parse after typed gate
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            cur = cur[part]
        elif isinstance(cur, list):
            if not part.isdigit():
                raise KeyError(pointer)
            cur = cur[int(part)]  # audit-ok[coerce_int]: coerce after explicit numeric type gate
        else:
            raise KeyError(pointer)
    return cur


def _cmp(op: str, left: Any, right: Any, tolerance: float | None) -> bool:
    if op == "in":
        return left in right
    if tolerance is not None and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if op in ("==",):
            return abs(_as_float(left, what="cmp.left") - _as_float(right, what="cmp.right")) <= tolerance
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
    try:
        produced = meta.produced_by
    except AttributeError as e:
        raise JournalFormatError("artifact meta missing produced_by") from e
    if produced is None:
        return False
    try:
        raw_call = produced.call_id
    except AttributeError as e:
        raise JournalFormatError("artifact produced_by missing call_id") from e
    if raw_call is None:
        return False
    call_id = _as_str(raw_call, what="produced_by.call_id")
    if not call_id:
        return False
    try:
        raw_attempt = produced.attempt_id
    except AttributeError as e:
        raise JournalFormatError("artifact produced_by missing attempt_id") from e
    produced_attempt = _as_str(raw_attempt, what="produced_by.attempt_id")
    if not produced_attempt:
        raise JournalFormatError("artifact produced_by.attempt_id must be non-empty")
    return produced_attempt == attempt_id


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
    meta = artifacts.get_meta(artifact_id) if artifact_id else None  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
    if meta is None:
        producer = "agent"
        call_id = ""
    else:
        try:
            producer = meta.producer
        except AttributeError as e:
            raise JournalFormatError("artifact meta missing producer") from e
        if producer is None:
            raise JournalFormatError("artifact meta.producer must not be null")
        producer = _as_str(producer, what="artifact.producer")
        # Fail-closed: unknown / future producers collapse to agent (untrusted).
        if producer == "user":
            pass
        elif producer not in ("agent", "core", "sandbox", "tool"):
            producer = "agent"
        try:
            produced = meta.produced_by
        except AttributeError as e:
            raise JournalFormatError("artifact meta missing produced_by") from e
        if produced is None:
            call_id = ""
        else:
            try:
                raw_call = produced.call_id
            except AttributeError as e:
                raise JournalFormatError(
                    "artifact produced_by missing call_id"
                ) from e
            if raw_call is None:
                call_id = ""
            else:
                call_id = _as_str(raw_call, what="produced_by.call_id")
    belongs = artifact_belongs_to_attempt(meta, attempt_id)
    if artifact_id is None:
        art_id_s = ""
    else:
        art_id_s = _as_str(artifact_id, what="artifact_id")
    out: dict[str, Any] = {
        "producer": producer,
        "trusted": artifact_producer_trusted(meta),
        "core_evaluated": True,
        "produced_by_call_id": call_id,
        "artifact_id": art_id_s,
        "countable": belongs,
    }
    if meta is not None and producer == "sandbox":
        try:
            ni = meta.net_isolation
        except AttributeError:
            ni = None
        try:
            fs = meta.fs_isolation
        except AttributeError:
            fs = None
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
    candidates = []
    if isinstance(left, dict):
        candidates.append(left)
    if isinstance(doc, dict):
        candidates.append(doc)
        if isinstance(doc.get("effect"), dict):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            candidates.append(doc["effect"])
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        if "ci_low" not in raw or "ci_high" not in raw:
            continue
        try:
            lo = _as_float(raw["ci_low"], what="ci_low")
            hi = _as_float(raw["ci_high"], what="ci_high")
            if raw.get("est") is not None:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                est = _as_float(raw["est"], what="est")
            else:
                est = (lo + hi) / 2.0
        except (TypeError, ValueError, JournalFormatError):
            continue  # audit-ok[except_silent_continue]: skip malformed row; fail-closed on required paths elsewhere
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
    uses_sesoi: bool,
    sesoi: float | None = None,
    predicts_null: bool,
    predicted_direction: str,
    attempt_id: str | None = None,
    fetched_digests: frozenset[str] | Collection[str] | None = None,
) -> CheckResult:
    try:
        if check.kind == "artifact_exists":
            out_name = check.output
            if not out_name:
                raise JournalFormatError("artifact_exists requires non-empty output")
            handle = out.outputs.get(out_name)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
            if art is None:
                raise JournalFormatError("artifact_exists meta vanished after probe")
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
        used_sesoi=False,
    )

        if check.kind in ("value_matches", "metric_threshold", "json_pointer"):
            if not check.output:
                raise JournalFormatError(
                    f"{check.kind} requires non-empty output"
                )
            handle = out.outputs.get(check.output)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
                )  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class

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
                    est = _as_float(left, what="value_matches.left")
                    effect = {"est": est, "kind": "point", **prov}
            else:
                # metric_threshold — point-estimate exceedance or TOST with real CI.
                if check.op is None:
                    raise JournalFormatError("metric_threshold requires op")
                op = check.op
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
                        s = _as_float(sesoi, what="sesoi")
                        ok = (-s) < ci["ci_low"] and ci["ci_high"] < s
                    else:
                        # No CI → not a TOST result; equivalence not assessable.
                        used_sesoi = False
                        effect = None
                        ok = False
                elif uses_sesoi and sesoi is not None and isinstance(left, (int, float)):
                    # Honest point-estimate check (not TOST).
                    used_sesoi = True
                    est = _as_float(left, what="metric.left")
                    effect = {"est": est, "kind": "point", **prov}
                    s = _as_float(sesoi, what="sesoi")
                    if predicted_direction == "positive":
                        ok = est > s
                    elif predicted_direction == "negative":
                        ok = est < -s
                    else:
                        ok = abs(est) > s
                else:
                    ok = _cmp(op, left, check.target, check.tolerance)
                    if isinstance(left, (int, float)):
                        effect = {
                            "est": _as_float(left, what="metric.left"),
                            "kind": "point",
                            **prov,
                        }

            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET if ok else ReqStatus.FAILED,  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
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
            if not check.output:
                raise JournalFormatError("schema_valid requires non-empty output")
            handle = out.outputs.get(check.output)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
        used_sesoi=False,
    )

        if check.kind == "source_link":
            return CheckResult(
                check_id=check_id,
                template_id=template_id,
                status=ReqStatus.MET,
                is_code=True,
                required=True,
                role=role,
        used_sesoi=False,
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
        used_sesoi=False,
    )
    except SandboxInfraError:
        raise
    except CoreBugError:
        raise
    except CatalogError:
        # Catalog integrity (e.g. holdout_digest mismatch) — not a core bug.
        raise
    except JournalFormatError:
        # Fail-closed required-field errors — never wrap as CoreBug/FAILED.
        raise
    except OSError as e:
        # Host I/O outside Popen on executable kinds → infra (B3).
        if check.kind in (
            "exec_runs",
            "tests_pass",
            "metric_on_holdout",
            "reproducible_rerun",
        ):
            raise SandboxInfraError(f"sandbox_unavailable:host_io:{e.__class__.__name__}", transient=False) from e
        return CheckResult(
            check_id=check_id,
            template_id=template_id,
            status=ReqStatus.FAILED,
            is_code=True,
            required=True,
            role=role,
            detail=str(e),
        used_sesoi=False,
    )
    except Exception as e:
        # Unexpected core bugs: neither infra nor scientific (B3).
        # Never convert unexpected exceptions into FAILED CheckResult.
        raise CoreBugError(f"core_bug:{e.__class__.__name__}:{e}") from e  # audit-ok[broad_except]: cleanup then re-raise; does not swallow

    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.FAILED,
        is_code=True,
        required=True,
        role=role,
        detail=f"unhandled kind {check.kind}",
        used_sesoi=False,
    )


def agent_facing_template_view(tmpl: RequirementTemplate) -> dict[str, Any]:
    """Fields safe to show an agent — never the full CodeCheck (holdout/scorer/tests).

    This is the **only** agent-facing serialization of a catalog template.
    Prompt renderers (arriving later) must call this — never
    ``RequirementTemplate.model_dump`` / full ``CodeCheck``.
    """
    check = tmpl.check
    if isinstance(check, CodeCheck):
        check_kind: str | None = check.kind
    elif isinstance(check, ReviewCheck):
        check_kind = check.reviewer_role
    else:
        raise JournalFormatError(
            f"agent_facing_template_view: unknown check type {type(check).__name__}"
        )
    return {
        "template_id": tmpl.id,
        "text_ru": tmpl.text_ru,
        "version": tmpl.version,
        "required": tmpl.required,
        "role": tmpl.role,
        "is_code": isinstance(check, CodeCheck),
        "check_kind": check_kind,
    }


def agent_facing_catalog_view(catalog: Catalog) -> dict[str, Any]:
    """Agent-safe catalog projection — templates via ``agent_facing_template_view`` only."""
    return {
        "catalog_version": catalog.catalog_version,
        "edition": catalog.edition,
        "templates": [agent_facing_template_view(t) for t in catalog.templates],
        "defaults": {k: list(v) for k, v in catalog.defaults.items()},
        "code_output_names": list(catalog.code_output_names),
    }


def template_to_requirement(
    tmpl: RequirementTemplate,
    node_id: str,
    *,
    status: ReqStatus = ReqStatus.PENDING,  # audit-ok[named_default]: named constant / enum member default; not silent invent
    req_id: str | None = None,
) -> NodeRequirement:
    """Build a NodeRequirement from the agent-facing template view (no check body)."""
    view = agent_facing_template_view(tmpl)
    text_ru = view["text_ru"]
    if text_ru is None:
        text_ru = ""
    return NodeRequirement(
        id=req_id or f"{node_id}:{view['template_id']}",
        template_id=_as_str(view["template_id"], what="template_id"),
        node_id=node_id,
        text_ru=_as_str(text_ru, what="text_ru"),
        required=_as_bool(view["required"], what="required"),
        status=status,
        check_version=_as_str(view["version"], what="version"),
        is_code=_as_bool(view["is_code"], what="is_code"),
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
    from coscientist.core.plan.proposed_check import (
        assert_proposal_preregistered_for_eval,
        code_check_from_proposal,
        rubric_from_proposal,
    )

    if not isinstance(ref.params, dict):
        raise JournalFormatError("proposed_check RequirementRef.params must be dict")
    params = ref.params
    pid_raw = params.get("proposal_id")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if pid_raw is None:
        pid = ""
    else:
        pid = _as_str(pid_raw, what="proposal_id")
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
        used_sesoi=False,
    )
    row = proposals.get(pid)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
        used_sesoi=False,
    )
    if "status" not in row or not isinstance(row["status"], str) or not row["status"]:
        raise CatalogError("proposed_check row missing status")
    status = row["status"]
    role_raw = row.get("role")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if role_raw not in ("prediction", "validity"):
        raise CatalogError(f"proposed_check row bad role: {role_raw!r}")
    role: CheckRole = role_raw  # type: ignore[assignment]
    base_effect["proposal_digest"] = row.get("proposal_digest")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    if "delegated" in row:
        base_effect["delegated"] = _as_bool(row["delegated"], what="delegated")
    else:
        base_effect["delegated"] = False
    base_effect["bound_task_id"] = row["bound_task_id"]
    base_effect["bound_target"] = row["bound_target"]
    # Target must be on the RequirementRef (params); no fallback to bound_target.
    eval_target = params.get("target")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    prereg_err = assert_proposal_preregistered_for_eval(
        row,
        attempt_id=attempt_id,
        task_id=task_id,
        target=str(eval_target) if eval_target is not None else None,  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal
        attempts=attempts,
        results=results,
    )
    if prereg_err:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=row.get("check") is not None,  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            required=ref.required,
            role=role,
            detail=prereg_err,
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        used_sesoi=False,
    )
    # Cheap bind: when the attempt recorded claims, params.target must name one.
    claims = list(out.claims)
    if claims and eval_target is not None and str(eval_target).strip():
        import unicodedata

        want = unicodedata.normalize("NFKC", _as_str(eval_target, what="eval_target")).strip()
        claim_targets = set()
        for c in claims:
            if not isinstance(c, dict):
                continue
            t = c.get("target")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            if t is None:
                continue
            claim_targets.add(
                unicodedata.normalize("NFKC", _as_str(t, what="claim.target")).strip()
            )
        claim_targets.discard("")
        if claim_targets and want not in claim_targets:
            return CheckResult(
                check_id=check_id,
                template_id=PROPOSED_CHECK_TEMPLATE_ID,
                status=ReqStatus.FAILED,
                is_code=row.get("check") is not None,  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
                required=ref.required,
                role=role,
                detail=(
                    f"unapproved_check:claim_target_mismatch:{want!r}"
                    f" not in {sorted(claim_targets)!r}"
                ),
                effect={**base_effect, "exclusion_reason": "unapproved_check"},
        used_sesoi=False,
    )
    if status == "rejected":
        if "reject_reason" in row and row["reject_reason"] is not None:
            reject_detail = _as_str(row["reject_reason"], what="reject_reason")
        else:
            reject_detail = "rejected"
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=row.get("check") is not None,  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            required=ref.required,
            role=role,
            detail=f"unapproved_check:rejected:{reject_detail}",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        used_sesoi=False,
    )
    if status != "approved":
        # Awaiting HITL / reviewer — required checks keep the attempt open.
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.PENDING,
            is_code=row.get("check") is not None,  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            required=ref.required,
            role=role,
            detail="unapproved_check:pending_approval",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        used_sesoi=False,
    )

    # Approved: evaluate using the preregistered body (never mutate after propose).
    # Bound approval: one task_id + target only (no multi-claim k_sup reuse).
    bound_task = _as_str(row["bound_task_id"], what="bound_task_id")
    if task_id and bound_task and _as_str(task_id, what="task_id") != bound_task:
        return CheckResult(
            check_id=check_id,
            template_id=PROPOSED_CHECK_TEMPLATE_ID,
            status=ReqStatus.FAILED,
            is_code=row.get("check") is not None,  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            required=ref.required,
            role=role,
            detail=f"unapproved_check:task_mismatch:{bound_task}!={task_id}",
            effect={**base_effect, "exclusion_reason": "unapproved_check"},
        used_sesoi=False,
    )
    if "delegated" in row:
        delegated = _as_bool(row["delegated"], what="delegated")
    else:
        delegated = False
    if row.get("rubric") is not None:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        from coscientist.checks.kinds.rubric import evaluate_rubric
        from coscientist.core.execution.domain import CodeCheck as CC

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
        if cr.effect is None:
            effect = {}
        else:
            effect = dict(cr.effect)
        effect.update(
            {
                "proposed_check": True,
                "proposal_id": pid,
                "proposal_digest": row.get("proposal_digest"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
        used_sesoi=False,
    )
    from coscientist.checks.sandbox_limits import apply_frozen_limits

    check = apply_frozen_limits(check, spec, template_id=PROPOSED_CHECK_TEMPLATE_ID)
    if "predicted_direction" not in row or row["predicted_direction"] is None:
        raise JournalFormatError(
            "proposed_check missing required predicted_direction"
        )
    if "uses_sesoi" in row:
        uses_sesoi_v = _as_bool(row["uses_sesoi"], what="uses_sesoi")
    else:
        uses_sesoi_v = False
    if "predicts_null" in row:
        predicts_null_v = _as_bool(row["predicts_null"], what="predicts_null")
    else:
        predicts_null_v = False
    if row.get("sesoi") is not None:  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        sesoi_v = row.get("sesoi")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    else:
        sesoi_v = row.get("threshold")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
    cr = evaluate_code_check(
        check,
        template_id=PROPOSED_CHECK_TEMPLATE_ID,
        check_id=check_id,
        out=out,
        artifacts=artifacts,
        values=out.values,
        role=role,
        uses_sesoi=uses_sesoi_v,
        sesoi=sesoi_v,
        predicts_null=predicts_null_v,
        predicted_direction=_as_str(row["predicted_direction"], what="predicted_direction"),
        attempt_id=attempt_id,
        fetched_digests=fetched_digests,
    )
    if cr.effect is None:
        effect = {}
    else:
        effect = dict(cr.effect)
    effect.update(
        {
            "proposed_check": True,
            "proposal_id": pid,
            "proposal_digest": row.get("proposal_digest"),  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
            "delegated": delegated,
        }
    )
    # Severe only when trusted (sandbox hard isolation) — DESIGN_M1 §16.
    if not effect.get("trusted"):  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
    plan_role: PlanRole | str | None = None,
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

    def _optional_dict(value: Any, *, name: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise JournalFormatError(
                f"evaluate_task_checks: {name} must be dict|None, "
                f"got {type(value).__name__}"
            )
        return value

    review_statuses = _optional_dict(review_statuses, name="review_statuses")
    judge_retry = _optional_dict(judge_retry, name="judge_retry")
    failure_kinds = _optional_dict(failure_kinds, name="failure_kinds")
    proposals = _optional_dict(proposals, name="proposals")
    results = []
    refs = list(spec.criteria)
    if not refs:
        # apply defaults for experiment kind (schema NodeKind)
        if "experiment" not in catalog.defaults:
            raise JournalFormatError(
                "catalog.defaults missing required key 'experiment'"
            )
        for tid in catalog.defaults["experiment"]:
            refs.append(RequirementRef(template_id=tid, required=True))

    # Sole PLAN-n signal is Task.plan_role (passed as plan_role=...).
    if plan_role is None:
        raise JournalFormatError("evaluate_task_checks: plan_role required")
    if isinstance(plan_role, PlanRole):
        role_tok = plan_role
    elif isinstance(plan_role, str):
        try:
            role_tok = PlanRole(plan_role)
        except ValueError as e:
            raise JournalFormatError(
                f"evaluate_task_checks: invalid plan_role {plan_role!r}"
            ) from e
    else:
        raise JournalFormatError(
            "evaluate_task_checks: plan_role must be PlanRole|str, "
            f"got {type(plan_role).__name__}"
        )
    is_core_plan_n = role_tok == PlanRole.PLAN_N

    for i, ref in enumerate(refs):
        tmpl = by_id.get(ref.template_id)  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
        cid = f"{ref.template_id}#{i}"
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
        used_sesoi=False,
    )
            )
            continue
        role = role_from_template(tmpl)
        is_proposed_kind = tmpl.kind == TemplateKind.PROPOSED
        is_proposed_id = ref.template_id == PROPOSED_CHECK_TEMPLATE_ID
        if is_proposed_kind != is_proposed_id:
            raise JournalFormatError(
                "proposed_check kind/template_id mismatch: "
                f"kind={tmpl.kind!r} template_id={ref.template_id!r}"
            )
        if is_proposed_kind and is_proposed_id:
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
        if tmpl.kind == TemplateKind.PLAN_N_CRITIC:
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
        used_sesoi=False,
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
        used_sesoi=False,
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
            from coscientist.core.claims.reviews import review_key

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
                if rkey in review_statuses:
                    st = review_statuses[rkey]
                else:
                    st = ReqStatus.UNASSESSED
            results.append(
                CheckResult(
                    check_id=cid,
                    template_id=tmpl.id,
                    status=st,
                    is_code=False,
                    required=ref.required and tmpl.required,
                    role=role,
                    detail=check.question,
        used_sesoi=False,
    )
            )
        elif isinstance(check, CodeCheck) and check.kind == "rubric":
            from coscientist.checks.kinds.rubric import evaluate_rubric
            from coscientist.core.claims.rubric import freeze_rubric

            # Prefer OutputSpec.rubric for the named output (plan-frozen).
            if not check.output:
                raise JournalFormatError("rubric requires non-empty output")
            out_rubric = None
            for o in spec.expected_outputs:
                if o.name == check.output and o.rubric is not None:
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
                uses_sesoi=tmpl.uses_sesoi,
                sesoi=tmpl.sesoi,
                predicts_null=tmpl.predicts_null,
                predicted_direction=tmpl.predicted_direction,
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
        if isinstance(r.effect, dict):
            effect = r.effect
        else:
            effect = {}
        dig = effect.get("rubric_digest")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)
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
