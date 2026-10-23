"""Executable CodeCheck kinds (DESIGN_M1 §7): exec_runs, tests_pass,
metric_on_holdout, reproducible_rerun.

Sandbox outputs go through ``put_sandbox`` with recorded ``net_isolation``.
``trusted`` only when catalog limits fully held (soft isolation → trusted=False,
still core_evaluated/countable, with a report note). Infra failures raise
``SandboxInfraError``.
"""

from __future__ import annotations

import json
from typing import Any

from coscientist.checks.kinds._util import (
    base_result,
    cmp,
    first_float,
    merge_detail,
    parse_junit,
    pointer_get,
    raise_if_infra,
    slot_filename,
    soft_note,
    store_log,
)
from coscientist.checks.sandbox import (
    NetIsolation,
    SandboxStatus,
    limits_from_check,
    run_sandbox,
)
from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import (
    AgentTaskOutput,
    CheckResult,
    CheckRole,
    CodeCheck,
    EvidenceRef,
)
from coscientist.core.errors import SandboxInfraError

EXECUTABLE_KINDS = frozenset(
    {"exec_runs", "tests_pass", "metric_on_holdout", "reproducible_rerun"}
)


def _argv_for(check: CodeCheck) -> list[str]:
    if check.argv:
        return [str(a) for a in check.argv]
    if check.command:
        return [str(check.command)]
    return []


def _limits(check: CodeCheck):
    return limits_from_check(
        timeout_s=check.timeout_s,
        memory_mb=check.memory_mb,
        max_output_bytes=check.max_output_bytes,
        allow_network=bool(check.allow_network),
    )


def evaluate_exec_runs(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    role: CheckRole,
    attempt_id: str | None,
) -> CheckResult:
    argv = _argv_for(check)
    if not argv:
        raise SandboxInfraError("sandbox_unavailable:missing_command")
    inputs: dict[str, bytes] = {}
    if check.output:
        handle = out.outputs.get(check.output)
        if handle is not None:
            inputs[slot_filename(check.output)] = artifacts.read(handle.artifact_id)
    result = run_sandbox(argv, limits=_limits(check), inputs=inputs or None)
    raise_if_infra(result.status, result.reason)
    log_body = (
        b"exit="
        + str(result.exit_code).encode()
        + b" net_isolation="
        + result.net_isolation.encode()
        + b"\n--- stdout ---\n"
        + result.stdout
        + b"\n--- stderr ---\n"
        + result.stderr
    )
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=log_body,
        net_isolation=result.net_isolation,
    )
    ok = result.status is SandboxStatus.OK and result.exit_code == 0
    note = soft_note(result.net_isolation)
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(None if ok else result.reason, note),
        effect={
            "producer": "sandbox",
            "trusted": trusted,
            "net_isolation": result.net_isolation,
            "core_evaluated": True,
            "countable": bool(attempt_id),
            "produced_by_call_id": f"{attempt_id or ''}:sandbox:{check_id}",
            "artifact_id": art_id,
            **({"note": note} if note else {}),
        },
    )


def evaluate_tests_pass(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    role: CheckRole,
    attempt_id: str | None,
) -> CheckResult:
    # In-process junit parse — no guest; net_isolation=none (limits held vacuously).
    handle = out.outputs.get(check.output or "junit")
    if handle is None:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="output_not_submitted",
        )
    meta = artifacts.get_meta(handle.artifact_id)
    if meta is None:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="artifact_missing",
        )
    raw = artifacts.read(handle.artifact_id)
    try:
        failures, tests = parse_junit(raw)
    except Exception as e:  # noqa: BLE001
        raise SandboxInfraError(f"sandbox_crash:junit_parse:{e.__class__.__name__}") from e
    min_tests = int(check.min_tests) if check.min_tests is not None else 1
    ok = failures == 0 and tests >= min_tests
    summary = (
        f"tests={tests} failures={failures} min_tests={min_tests} "
        f"ok={ok} net_isolation=none\n"
    ).encode()
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary + raw[:8000],
        name="junit_eval.log",
        net_isolation="none",
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[
            EvidenceRef(artifact_id=handle.artifact_id, digest=meta.sha256),
            EvidenceRef(artifact_id=art_id, digest=digest),
        ],
        detail=None if ok else f"failures={failures} tests={tests}",
        effect={
            "producer": "sandbox",
            "trusted": trusted,
            "net_isolation": "none",
            "core_evaluated": True,
            "countable": bool(attempt_id),
            "produced_by_call_id": f"{attempt_id or ''}:sandbox:{check_id}",
            "artifact_id": art_id,
        },
    )


def evaluate_metric_on_holdout(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    role: CheckRole,
    attempt_id: str | None,
    prov_fields: dict[str, Any],
) -> CheckResult:
    """Require frozen holdout digest + metrics.json; EvidenceRef on both."""
    holdout_slot = check.holdout_output or "holdout"
    metrics_slot = check.output or "metrics"
    holdout = out.outputs.get(holdout_slot)
    metrics = out.outputs.get(metrics_slot)
    if holdout is None or metrics is None:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="output_not_submitted",
        )
    h_meta = artifacts.get_meta(holdout.artifact_id)
    m_meta = artifacts.get_meta(metrics.artifact_id)
    if h_meta is None or m_meta is None:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="artifact_missing",
        )
    frozen = check.holdout_digest
    if not frozen:
        raise SandboxInfraError("sandbox_unavailable:holdout_digest_missing")
    if h_meta.sha256 != frozen:
        log_body = (
            f"holdout_digest_mismatch frozen={frozen} actual={h_meta.sha256}\n"
        ).encode()
        art_id, digest, _trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=log_body,
            net_isolation="none",
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[
                EvidenceRef(artifact_id=holdout.artifact_id, digest=h_meta.sha256),
                EvidenceRef(artifact_id=art_id, digest=digest),
            ],
            detail="holdout_digest_mismatch",
        )

    raw = artifacts.read(metrics.artifact_id)
    doc = json.loads(raw)
    pointer = check.pointer or (f"/{check.metric}" if check.metric else None)
    left = pointer_get(doc, pointer)
    target = check.target
    op = check.op or ">="
    ok = cmp(op, left, target, check.tolerance)
    summary = canonical_json(
        {
            "left": left,
            "op": op,
            "target": target,
            "ok": ok,
            "holdout": frozen,
            "net_isolation": "none",
        }
    )
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary,
        net_isolation="none",
    )
    effect = None
    if isinstance(left, (int, float)):
        effect = {
            "est": float(left),
            "kind": "point",
            **prov_fields,
            "producer": "sandbox",
            "trusted": trusted,
            "net_isolation": "none",
            "core_evaluated": True,
            "countable": bool(attempt_id),
            "produced_by_call_id": f"{attempt_id or ''}:sandbox:{check_id}",
            "artifact_id": art_id,
        }
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[
            EvidenceRef(artifact_id=holdout.artifact_id, digest=h_meta.sha256),
            EvidenceRef(
                artifact_id=metrics.artifact_id,
                json_pointer=pointer,
                digest=sha256_hex(canonical_json(left)),
            ),
            EvidenceRef(artifact_id=art_id, digest=digest),
        ],
        detail=None if ok else f"{left!r} vs {target!r}",
        effect=effect,
    )


def evaluate_reproducible_rerun(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    role: CheckRole,
    attempt_id: str | None,
    prov_fields: dict[str, Any],
) -> CheckResult:
    """Run argv twice in the sandbox; |Δ| on stdout float ≤ tolerance."""
    argv = _argv_for(check)
    if not argv:
        raise SandboxInfraError("sandbox_unavailable:missing_command")
    tol = float(check.tolerance) if check.tolerance is not None else float(
        check.rerun_tol if check.rerun_tol is not None else 0.0
    )
    runs: list[float] = []
    logs: list[bytes] = []
    isolation: NetIsolation = "none"
    for i in range(2):
        result = run_sandbox(argv, limits=_limits(check))
        raise_if_infra(result.status, result.reason)
        isolation = result.net_isolation
        logs.append(
            b"run="
            + str(i).encode()
            + b" exit="
            + str(result.exit_code).encode()
            + b" net_isolation="
            + result.net_isolation.encode()
            + b"\n"
            + result.stdout
            + b"\n"
            + result.stderr
        )
        if result.status is not SandboxStatus.OK:
            art_id, digest, trusted = store_log(
                artifacts,
                attempt_id=attempt_id,
                check_id=check_id,
                body=b"\n".join(logs),
                net_isolation=isolation,
            )
            note = soft_note(isolation)
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail(result.reason, note),
                effect={
                    "producer": "sandbox",
                    "trusted": trusted,
                    "net_isolation": isolation,
                    "core_evaluated": True,
                    "countable": bool(attempt_id),
                    "produced_by_call_id": f"{attempt_id or ''}:sandbox:{check_id}",
                    "artifact_id": art_id,
                    **({"note": note} if note else {}),
                },
            )
        runs.append(first_float(result.stdout))
    delta = abs(runs[0] - runs[1])
    ok = delta <= tol
    summary = canonical_json(
        {
            "runs": runs,
            "delta": delta,
            "tol": tol,
            "ok": ok,
            "net_isolation": isolation,
        }
    )
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary + b"\n" + b"\n".join(logs),
        net_isolation=isolation,
    )
    note = soft_note(isolation)
    effect = {
        "est": float(runs[0]),
        "kind": "point",
        **prov_fields,
        "producer": "sandbox",
        "trusted": trusted,
        "net_isolation": isolation,
        "core_evaluated": True,
        "countable": bool(attempt_id),
        "produced_by_call_id": f"{attempt_id or ''}:sandbox:{check_id}",
        "artifact_id": art_id,
        **({"note": note} if note else {}),
    }
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(None if ok else f"delta={delta} > tol={tol}", note),
        effect=effect,
    )


def evaluate_executable(
    check: CodeCheck,
    *,
    template_id: str,
    check_id: str,
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    role: CheckRole,
    attempt_id: str | None,
    prov_fields: dict[str, Any],
) -> CheckResult:
    if check.kind == "exec_runs":
        return evaluate_exec_runs(
            check,
            template_id=template_id,
            check_id=check_id,
            out=out,
            artifacts=artifacts,
            role=role,
            attempt_id=attempt_id,
        )
    if check.kind == "tests_pass":
        return evaluate_tests_pass(
            check,
            template_id=template_id,
            check_id=check_id,
            out=out,
            artifacts=artifacts,
            role=role,
            attempt_id=attempt_id,
        )
    if check.kind == "metric_on_holdout":
        return evaluate_metric_on_holdout(
            check,
            template_id=template_id,
            check_id=check_id,
            out=out,
            artifacts=artifacts,
            role=role,
            attempt_id=attempt_id,
            prov_fields=prov_fields,
        )
    if check.kind == "reproducible_rerun":
        return evaluate_reproducible_rerun(
            check,
            template_id=template_id,
            check_id=check_id,
            out=out,
            artifacts=artifacts,
            role=role,
            attempt_id=attempt_id,
            prov_fields=prov_fields,
        )
    raise SandboxInfraError(f"sandbox_unavailable:unhandled_kind:{check.kind}")
