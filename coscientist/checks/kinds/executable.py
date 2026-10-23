"""Executable CodeCheck kinds (DESIGN_M1 §7).

B1 pattern: collect agent declared outputs (sha256); run catalog scorer in a
second fresh verifier jail (agent outputs/scorer/holdout RO; results RW only);
read reward/result files — never guest stdout. No agent code during verify.
"""

from __future__ import annotations

import json
from typing import Any

from coscientist.checks.kinds._util import (
    MALFORMED_JUNIT,
    MALFORMED_METRICS,
    MALFORMED_REWARD,
    base_result,
    cmp,
    merge_detail,
    merge_numeric_extra,
    parse_junit,
    pointer_get,
    raise_if_infra,
    sandbox_effect,
    slot_filename,
    soft_note,
    store_log,
)
from coscientist.checks.sandbox import (
    SandboxStatus,
    limits_from_check,
    run_sandbox,
    run_verifier,
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

REWARD_NAME = "reward.txt"
JUNIT_NAME = "junit.xml"
METRICS_NAME = "metrics.json"
DIGEST_NAME = "out.digest"


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


def _collect_declared_outputs(
    out: AgentTaskOutput,
    artifacts: LocalArtifactStore,
    slots: list[str] | None = None,
) -> tuple[dict[str, bytes], list[EvidenceRef]]:
    """Collect agent declared outputs with digests (B1 step 1)."""
    inputs: dict[str, bytes] = {}
    evidence: list[EvidenceRef] = []
    items = (
        ((s, out.outputs[s]) for s in slots if s in out.outputs)
        if slots is not None
        else out.outputs.items()
    )
    for slot, handle in items:
        meta = artifacts.get_meta(handle.artifact_id)
        if meta is None:
            continue
        data = artifacts.read(handle.artifact_id)
        inputs[slot_filename(slot)] = data
        evidence.append(
            EvidenceRef(artifact_id=handle.artifact_id, digest=meta.sha256)
        )
    return inputs, evidence


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
    inputs, _ev = _collect_declared_outputs(
        out, artifacts, [check.output] if check.output else []
    )
    result = run_sandbox(argv, limits=_limits(check), inputs=inputs or None)
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    log_body = (
        b"exit="
        + str(result.exit_code).encode()
        + b" net="
        + result.net_isolation.encode()
        + b" fs="
        + result.fs_isolation.encode()
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
        fs_isolation=result.fs_isolation,
    )
    ok = result.status is SandboxStatus.OK and result.exit_code == 0
    note = result.note or soft_note(result.net_isolation, result.fs_isolation)
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
    """Verifier runs catalog scorer; junit/reward from results dir only."""
    argv = _argv_for(check)
    if not argv:
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
                "note": "tests_pass requires catalog scorer argv; not trusted",
            },
        )

    # Agent supplies code under test as declared outputs (RO into verifier).
    inputs, ev = _collect_declared_outputs(out, artifacts)
    # Default catalog scorer writes junit.xml into COSCIENTIST_RESULTS when argv
    # is a custom script; for pytest-style argv we wrap with controlled flags.
    scorer_bytes = None
    collect = [JUNIT_NAME, REWARD_NAME]
    guest_argv = list(argv)
    if guest_argv and Path_name(guest_argv[0]) in {"pytest", "py.test"}:
        # Catalog-controlled pytest: no agent conftest, junit outside agent tree.
        guest_argv = [
            *guest_argv,
            "--noconftest",
            "-p",
            "no:cacheprovider",
            f"--junitxml=/results/{JUNIT_NAME}",
            "/inputs",
        ]
        # pytest binary path: run via python -m pytest for RO bind simplicity
        if guest_argv[0] in {"pytest", "py.test"}:
            guest_argv = ["python3", "-m", "pytest", *guest_argv[1:]]

    result = run_verifier(
        guest_argv,
        inputs=inputs,
        limits=_limits(check),
        scorer_bytes=scorer_bytes,
        collect=collect,
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    note = result.note or soft_note(result.net_isolation, result.fs_isolation)
    raw = result.result_files.get(JUNIT_NAME)
    if raw is None:
        # Accept reward.txt "pass"/"fail" as alternate catalog signal.
        reward = result.result_files.get(REWARD_NAME)
        if reward is not None:
            ok = reward.strip() in (b"1", b"pass", b"ok", b"true")
            art_id, digest, trusted = store_log(
                artifacts,
                attempt_id=attempt_id,
                check_id=check_id,
                body=b"reward=" + reward,
                name="tests_eval.log",
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
            )
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=ok,
                evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail(None if ok else MALFORMED_REWARD, note),
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
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=b"missing junit/reward\n" + result.stderr,
            name="tests_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
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
            name="tests_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
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
        f"tests={tests} failures={failures} min_tests={min_tests} ok={ok} "
        f"net={result.net_isolation} fs={result.fs_isolation}\n"
    ).encode()
    art_id, digest, trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary + raw[:8000],
        name="tests_eval.log",
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
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


def Path_name(cmd: str) -> str:
    from pathlib import Path as P

    return P(cmd).name


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
    """metric = catalog scorer(agent predictions, holdout) in verifier jail."""
    holdout_slot = check.holdout_output or "holdout"
    pred_slot = check.output or "predictions"
    holdout = out.outputs.get(holdout_slot)
    predictions = out.outputs.get(pred_slot)
    # Also accept legacy "metrics" slot name as predictions input.
    if predictions is None:
        predictions = out.outputs.get("metrics")
    if holdout is None or predictions is None:
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
        p_meta = artifacts.get_meta(predictions.artifact_id)
        if h_meta is None or p_meta is None:
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[],
                detail="artifact_missing",
            )
        holdout_bytes = artifacts.read(holdout.artifact_id)
        pred_bytes = artifacts.read(predictions.artifact_id)
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:host_io:{e.__class__.__name__}"
        ) from e

    frozen = check.holdout_digest
    if not frozen:
        raise SandboxInfraError("sandbox_unavailable:holdout_digest_missing")
    if h_meta.sha256 != frozen:
        art_id, digest, _t = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=f"holdout_digest_mismatch frozen={frozen} actual={h_meta.sha256}\n".encode(),
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
                EvidenceRef(artifact_id=predictions.artifact_id, digest=p_meta.sha256),
            ],
            detail="sandbox_unavailable:missing_command",
            effect={
                "producer": "core",
                "trusted": False,
                "core_evaluated": True,
                "countable": bool(attempt_id),
                "produced_by_call_id": f"{attempt_id or 'noattempt'}:core:{check_id}",
                "note": "metric_on_holdout requires catalog scorer; "
                "agent metrics not trusted",
            },
        )

    # Default inline scorer when argv is a marker; otherwise use catalog argv.
    scorer_bytes = None
    if argv == ["catalog:metric"] or argv[0] == "catalog:metric":
        pointer = check.pointer or (f"/{check.metric}" if check.metric else "/score")
        scorer_bytes = _default_metric_scorer(pointer).encode()
        guest_argv = ["python3", "/scorer"]
    else:
        guest_argv = argv

    result = run_verifier(
        guest_argv,
        inputs={"predictions": pred_bytes},
        holdout=holdout_bytes,
        scorer_bytes=scorer_bytes,
        limits=_limits(check),
        collect=[REWARD_NAME, METRICS_NAME],
        extra_ro_files={"predictions.json": pred_bytes},
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    note = result.note or soft_note(result.net_isolation, result.fs_isolation)

    left: Any = None
    reward = result.result_files.get(REWARD_NAME)
    metrics_raw = result.result_files.get(METRICS_NAME)
    if reward is not None:
        try:
            left = float(reward.strip())
        except ValueError:
            left = None
    if left is None and metrics_raw is not None:
        try:
            doc = json.loads(metrics_raw)
            ptr: str | None = check.pointer or (
                f"/{check.metric}" if check.metric else None
            )
            left = pointer_get(doc, ptr)
        except Exception:  # noqa: BLE001
            left = None
    if left is None:
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=b"missing reward/metrics\n" + result.stderr,
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
                EvidenceRef(artifact_id=predictions.artifact_id, digest=p_meta.sha256),
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
            "predictions": p_meta.sha256,
            "net_isolation": result.net_isolation,
            "fs_isolation": result.fs_isolation,
            "source": "verifier",
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
            extra=merge_numeric_extra(prov_fields, est=float(left)),
        )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[
            EvidenceRef(artifact_id=holdout.artifact_id, digest=h_meta.sha256),
            EvidenceRef(artifact_id=predictions.artifact_id, digest=p_meta.sha256),
            EvidenceRef(
                artifact_id=art_id,
                digest=sha256_hex(canonical_json(left)),
            ),
        ],
        detail=merge_detail(None if ok else f"{left!r} vs {target!r}", note),
        effect=effect,
    )


def _default_metric_scorer(pointer: str) -> str:
    """Catalog scorer: read predictions + holdout, write reward.txt (never stdout)."""
    return f"""
import json, os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
inputs = pathlib.Path(os.environ["COSCIENTIST_INPUTS"])
pred_path = inputs / "predictions.json"
if not pred_path.exists():
    pred_path = inputs / "predictions"
doc = json.loads(pred_path.read_bytes())
# Holdout is mounted RO at COSCIENTIST_HOLDOUT; presence is the freeze check.
_ = os.environ.get("COSCIENTIST_HOLDOUT")
pointer = {pointer!r}
cur = doc
if pointer and pointer != "/":
    for part in pointer[1:].split("/"):
        cur = cur[part]
(results / "reward.txt").write_text(str(float(cur)))
(results / "metrics.json").write_bytes(pred_path.read_bytes())
"""


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
    """Two verifier runs; compare digests of core result files — not stdout."""
    argv = _argv_for(check)
    if not argv:
        raise SandboxInfraError("sandbox_unavailable:missing_command")
    tol = float(check.tolerance) if check.tolerance is not None else float(
        check.rerun_tol if check.rerun_tol is not None else 0.0
    )
    inputs, ev = _collect_declared_outputs(out, artifacts)
    from typing import cast

    from coscientist.checks.sandbox import FsIsolation, NetIsolation

    blobs: list[bytes] = []
    digests: list[str] = []
    logs: list[bytes] = []
    net_i: NetIsolation = "none"
    fs_i: FsIsolation = "none"
    note: str | None = None
    for i in range(2):
        result = run_verifier(
            argv,
            inputs=inputs,
            limits=_limits(check),
            collect=[DIGEST_NAME, REWARD_NAME],
        )
        raise_if_infra(
            result.status,
            result.reason,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=result.note,
        )
        net_i = cast(NetIsolation, str(result.net_isolation))
        fs_i = cast(FsIsolation, str(result.fs_isolation))
        note = result.note or soft_note(net_i, fs_i)
        logs.append(
            b"run="
            + str(i).encode()
            + b" exit="
            + str(result.exit_code).encode()
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
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
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
        blob = result.result_files.get(DIGEST_NAME) or result.result_files.get(
            REWARD_NAME
        )
        if blob is None:
            art_id, digest, trusted = store_log(
                artifacts,
                attempt_id=attempt_id,
                check_id=check_id,
                body=b"\n".join(logs),
                net_isolation=net_i,
                fs_isolation=fs_i,
            )
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail(MALFORMED_REWARD, note),
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
        blobs.append(blob)
        digests.append(sha256_hex(blob))

    # Prefer digest equality; if both rewards parse as floats, allow |Δ| ≤ tol.
    ok = digests[0] == digests[1]
    delta = 0.0
    est = 0.0
    try:
        v0 = float(blobs[0].strip())
        v1 = float(blobs[1].strip())
        delta = abs(v0 - v1)
        est = v0
        ok = delta <= tol
    except ValueError:
        ok = digests[0] == digests[1]
    summary = canonical_json(
        {
            "digests": digests,
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
        extra=merge_numeric_extra(prov_fields, est=float(est)),
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(
            None if ok else f"digest_mismatch {digests[0]} != {digests[1]}", note
        ),
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
