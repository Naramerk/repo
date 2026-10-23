"""Executable CodeCheck kinds (DESIGN_M1 §7): exec_runs, tests_pass,
metric_on_holdout, reproducible_rerun.

Core runs catalog argv inside the sandbox and reads only core-owned outputs.
``trusted`` only when both net and fs isolation are hard. Infra failures raise
``SandboxInfraError``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from coscientist.checks.kinds._util import (
    MALFORMED_JUNIT,
    MALFORMED_METRICS,
    base_result,
    cmp,
    first_float,
    merge_detail,
    parse_junit,
    pointer_get,
    protect_paths_for,
    raise_if_infra,
    sandbox_effect,
    slot_filename,
    soft_note,
    store_log,
)
from coscientist.checks.sandbox import (
    FsIsolation,
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

JUNIT_CORE_NAME = "junit.xml"
METRICS_CORE_NAME = "metrics.json"


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
    try:
        if check.output:
            handle = out.outputs.get(check.output)
            if handle is not None:
                inputs[slot_filename(check.output)] = artifacts.read(handle.artifact_id)
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:host_io:{e.__class__.__name__}"
        ) from e
    result = run_sandbox(
        argv,
        limits=_limits(check),
        inputs=inputs or None,
        protect_paths=protect_paths_for(artifacts),
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
    )
    log_body = (
        b"exit="
        + str(result.exit_code).encode()
        + b" net_isolation="
        + result.net_isolation.encode()
        + b" fs_isolation="
        + result.fs_isolation.encode()
        + b"\n--- stdout ---\n"
        + result.stdout
        + b"\n--- stderr ---\n"
        + result.stderr
    )
    try:
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=log_body,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:host_io:{e.__class__.__name__}"
        ) from e
    ok = result.status is SandboxStatus.OK and result.exit_code == 0
    note = soft_note(result.net_isolation, result.fs_isolation)
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(None if ok else result.reason, note),
        effect=sandbox_effect(
            attempt_id=attempt_id,
            check_id=check_id,
            art_id=art_id,
            trusted=trusted,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
        ),
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
    """Run catalog argv in sandbox; parse junit only from core-owned output (B1)."""
    argv = _argv_for(check)
    if not argv:
        # M1 without catalog argv: do not trust agent-submitted junit.
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="sandbox_unavailable:missing_command",
            effect={
                "producer": "core",
                "trusted": False,
                "core_evaluated": True,
                "countable": bool(attempt_id),
                "produced_by_call_id": f"{attempt_id or 'noattempt'}:core:{check_id}",
                "note": "tests_pass requires catalog argv; agent junit not trusted",
            },
        )

    inputs: dict[str, bytes] = {}
    try:
        # Optional agent code/data as RO inputs (never trusted as the junit source).
        for slot, handle in out.outputs.items():
            inputs[slot_filename(slot)] = artifacts.read(handle.artifact_id)
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:host_io:{e.__class__.__name__}"
        ) from e

    result = run_sandbox(
        argv,
        limits=_limits(check),
        inputs=inputs or None,
        collect_core=[JUNIT_CORE_NAME],
        protect_paths=protect_paths_for(artifacts),
        env={
            "COSCIENTIST_JUNIT": JUNIT_CORE_NAME,
        },
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
    )
    raw = result.core_outputs.get(JUNIT_CORE_NAME)
    note = soft_note(result.net_isolation, result.fs_isolation)
    if raw is None:
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=b"missing core junit\n" + result.stdout + b"\n" + result.stderr,
            name="junit_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(MALFORMED_JUNIT, note),
            effect=sandbox_effect(
                attempt_id=attempt_id,
                check_id=check_id,
                art_id=art_id,
                trusted=trusted,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
            ),
        )
    try:
        failures, tests = parse_junit(raw)
    except ValueError:
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=raw[:8000],
            name="junit_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(MALFORMED_JUNIT, note),
            effect=sandbox_effect(
                attempt_id=attempt_id,
                check_id=check_id,
                art_id=art_id,
                trusted=trusted,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
            ),
        )
    min_tests = int(check.min_tests) if check.min_tests is not None else 1
    ok = failures == 0 and tests >= min_tests
    summary = (
        f"tests={tests} failures={failures} min_tests={min_tests} "
        f"ok={ok} net_isolation={result.net_isolation} "
        f"fs_isolation={result.fs_isolation}\n"
    ).encode()
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary + raw[:8000],
        name="junit_eval.log",
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(
            None if ok else f"failures={failures} tests={tests}", note
        ),
        effect=sandbox_effect(
            attempt_id=attempt_id,
            check_id=check_id,
            art_id=art_id,
            trusted=trusted,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
        ),
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
    """Run metric argv on frozen holdout inside sandbox; ignore agent metrics (B1)."""
    holdout_slot = check.holdout_output or "holdout"
    holdout = out.outputs.get(holdout_slot)
    if holdout is None:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="output_not_submitted",
        )
    try:
        h_meta = artifacts.get_meta(holdout.artifact_id)
        if h_meta is None:
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[],
                detail="artifact_missing",
            )
        holdout_bytes = artifacts.read(holdout.artifact_id)
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:host_io:{e.__class__.__name__}"
        ) from e

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
            fs_isolation="none",
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

    argv = _argv_for(check)
    if not argv:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[
                EvidenceRef(artifact_id=holdout.artifact_id, digest=h_meta.sha256),
            ],
            detail="sandbox_unavailable:missing_command",
            effect={
                "producer": "core",
                "trusted": False,
                "core_evaluated": True,
                "countable": bool(attempt_id),
                "produced_by_call_id": f"{attempt_id or 'noattempt'}:core:{check_id}",
                "note": "metric_on_holdout requires catalog argv; "
                "agent metrics.json not trusted",
            },
        )

    holdout_name = slot_filename(holdout_slot)
    result = run_sandbox(
        argv,
        limits=_limits(check),
        inputs={holdout_name: holdout_bytes},
        collect_core=[METRICS_CORE_NAME],
        protect_paths=protect_paths_for(artifacts),
        env={
            "COSCIENTIST_HOLDOUT": holdout_name,
            "COSCIENTIST_METRICS": METRICS_CORE_NAME,
        },
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
    )
    note = soft_note(result.net_isolation, result.fs_isolation)
    raw = result.core_outputs.get(METRICS_CORE_NAME)
    if raw is None:
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=b"missing core metrics\n" + result.stdout + b"\n" + result.stderr,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
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
            detail=merge_detail(MALFORMED_METRICS, note),
            effect=sandbox_effect(
                attempt_id=attempt_id,
                check_id=check_id,
                art_id=art_id,
                trusted=trusted,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
            ),
        )
    try:
        doc = json.loads(raw)
        pointer = check.pointer or (f"/{check.metric}" if check.metric else None)
        left = pointer_get(doc, pointer)
    except Exception:  # noqa: BLE001 — unified malformed class (B3)
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=raw[:8000],
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
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
            detail=merge_detail(MALFORMED_METRICS, note),
            effect=sandbox_effect(
                attempt_id=attempt_id,
                check_id=check_id,
                art_id=art_id,
                trusted=trusted,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
            ),
        )

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
            "net_isolation": result.net_isolation,
            "fs_isolation": result.fs_isolation,
            "source": "core_owned",
        }
    )
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
    )
    effect = None
    if isinstance(left, (int, float)):
        effect = sandbox_effect(
            attempt_id=attempt_id,
            check_id=check_id,
            art_id=art_id,
            trusted=trusted,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
            extra={
                "est": float(left),
                "kind": "point",
                **{k: v for k, v in prov_fields.items() if k not in ("trusted", "producer")},
            },
        )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[
            EvidenceRef(artifact_id=holdout.artifact_id, digest=h_meta.sha256),
            EvidenceRef(
                artifact_id=art_id,
                json_pointer=pointer,
                digest=sha256_hex(canonical_json(left)),
            ),
        ],
        detail=merge_detail(None if ok else f"{left!r} vs {target!r}", note),
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
    net_i: NetIsolation = "none"
    fs_i: FsIsolation = "none"
    for i in range(2):
        result = run_sandbox(
            argv,
            limits=_limits(check),
            protect_paths=protect_paths_for(artifacts),
        )
        raise_if_infra(
            result.status,
            result.reason,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
        net_i = cast(NetIsolation, str(result.net_isolation))
        fs_i = cast(FsIsolation, str(result.fs_isolation))
        logs.append(
            b"run="
            + str(i).encode()
            + b" exit="
            + str(result.exit_code).encode()
            + b" net_isolation="
            + result.net_isolation.encode()
            + b" fs_isolation="
            + result.fs_isolation.encode()
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
                net_isolation=net_i,
                fs_isolation=fs_i,
            )
            note = soft_note(net_i, fs_i)
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail(result.reason, note),
                effect=sandbox_effect(
                    attempt_id=attempt_id,
                    check_id=check_id,
                    art_id=art_id,
                    trusted=trusted,
                    net_isolation=net_i,
                    fs_isolation=fs_i,
                    note=note,
                ),
            )
        try:
            runs.append(first_float(result.stdout))
        except ValueError:
            art_id, digest, trusted = store_log(
                artifacts,
                attempt_id=attempt_id,
                check_id=check_id,
                body=b"\n".join(logs),
                net_isolation=net_i,
                fs_isolation=fs_i,
            )
            note = soft_note(net_i, fs_i)
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail("malformed_output:numeric_stdout", note),
                effect=sandbox_effect(
                    attempt_id=attempt_id,
                    check_id=check_id,
                    art_id=art_id,
                    trusted=trusted,
                    net_isolation=net_i,
                    fs_isolation=fs_i,
                    note=note,
                ),
            )
    delta = abs(runs[0] - runs[1])
    ok = delta <= tol
    note = soft_note(net_i, fs_i)
    summary = canonical_json(
        {
            "runs": runs,
            "delta": delta,
            "tol": tol,
            "ok": ok,
            "net_isolation": net_i,
            "fs_isolation": fs_i,
        }
    )
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary + b"\n" + b"\n".join(logs),
        net_isolation=net_i,
        fs_isolation=fs_i,
    )
    effect = sandbox_effect(
        attempt_id=attempt_id,
        check_id=check_id,
        art_id=art_id,
        trusted=trusted,
        net_isolation=net_i,
        fs_isolation=fs_i,
        note=note,
        extra={
            "est": float(runs[0]),
            "kind": "point",
            **{k: v for k, v in prov_fields.items() if k not in ("trusted", "producer")},
        },
    )
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
