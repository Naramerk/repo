"""Executable CodeCheck kinds (DESIGN_M1 §7).

B1 pattern: collect agent declared outputs (sha256); run catalog scorer / task
tests in a second fresh verifier jail (agent outputs/scorer/holdout/tests RO;
results RW only); read reward/result files — never guest stdout. No agent code
during verify. Fail-closed without hard isolation.
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
REWARD_SCHEMA = "coscientist.reward.v1"
JUNIT_NAME = "junit.xml"
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


def parse_reward(raw: bytes) -> float:
    """Strict reward.txt: JSON ``{schema, value}`` only — no pass/fail strings."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"reward_not_json:{e.__class__.__name__}") from e
    if not isinstance(doc, dict):
        raise ValueError("reward_not_object")
    if doc.get("schema") != REWARD_SCHEMA:
        raise ValueError("reward_bad_schema")
    val = doc.get("value")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError("reward_bad_value")
    return float(val)


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
    note = result.note or soft_note(result.net_isolation, result.fs_isolation)
    # OUTPUT_EXCEEDED is scientific (guest fault); not infra.
    if result.status is SandboxStatus.OUTPUT_EXCEEDED:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail("output_exceeded", note),
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
    ok = result.status is SandboxStatus.OK and result.exit_code == 0
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


def _path_name(cmd: str) -> str:
    from pathlib import Path as P

    return P(cmd).name


def _catalog_tests_bytes(check: CodeCheck) -> dict[str, bytes] | None:
    if not check.tests:
        return None
    return {rel: src.encode("utf-8") for rel, src in check.tests.items()}


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
    """Verifier runs catalog/task tests (RO) against agent code; junit in results."""
    argv = _argv_for(check)
    catalog_tests = _catalog_tests_bytes(check)
    use_catalog_pytest = (
        (not argv or argv == ["catalog:pytest"] or argv[0] == "catalog:pytest")
        and catalog_tests is not None
    )
    if not argv and catalog_tests is None:
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
                "note": "tests_pass requires catalog tests or scorer argv",
            },
        )

    # Agent supplies code under test as declared outputs (RO at /inputs).
    # Catalog/task tests are separate (RO at /tests) — never agent tests.
    code_inputs, ev = _collect_declared_outputs(out, artifacts)
    collect = [JUNIT_NAME, REWARD_NAME]

    if use_catalog_pytest:
        # Catalog-controlled pytest: task tests only; agent conftest/plugins off.
        guest_argv = [
            "python3",
            "-m",
            "pytest",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            f"--junitxml=/results/{JUNIT_NAME}",
            "/tests",
        ]
    else:
        guest_argv = list(argv)
        if guest_argv and _path_name(guest_argv[0]) in {"pytest", "py.test"}:
            if guest_argv[0] in {"pytest", "py.test"}:
                guest_argv = ["python3", "-m", "pytest", *guest_argv[1:]]
            guest_argv = [
                *guest_argv,
                "--noconftest",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                f"--junitxml=/results/{JUNIT_NAME}",
            ]
            if catalog_tests is not None and "/tests" not in guest_argv:
                guest_argv.append("/tests")

    result = run_verifier(
        guest_argv,
        inputs=code_inputs,
        limits=_limits(check),
        tests=catalog_tests,
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
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=b"missing junit\n" + result.stderr,
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
    """metric = catalog scorer(agent predictions, holdout labels) in verifier."""
    holdout_slot = check.holdout_output or "holdout"
    pred_slot = check.output or "predictions"
    holdout = out.outputs.get(holdout_slot)
    predictions = out.outputs.get(pred_slot)
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

    scorer_bytes = None
    if argv == ["catalog:metric"] or argv[0] == "catalog:metric":
        scorer_bytes = _default_metric_scorer().encode()
        guest_argv = ["python3", "/scorer"]
    else:
        guest_argv = argv

    result = run_verifier(
        guest_argv,
        inputs={"predictions": pred_bytes},
        holdout=holdout_bytes,
        scorer_bytes=scorer_bytes,
        limits=_limits(check),
        collect=[REWARD_NAME],
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
    if reward is not None:
        try:
            left = parse_reward(reward)
        except ValueError:
            left = None
    if left is None:
        art_id, digest, trusted = store_log(
            artifacts,
            attempt_id=attempt_id,
            check_id=check_id,
            body=b"missing/malformed reward\n" + result.stderr,
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


def _default_metric_scorer() -> str:
    """Compute accuracy(y_hat, holdout.y); ignore any agent-claimed score."""
    return f"""
import json, os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
inputs = pathlib.Path(os.environ["COSCIENTIST_INPUTS"])
holdout_path = os.environ["COSCIENTIST_HOLDOUT"]
pred_path = inputs / "predictions.json"
if not pred_path.exists():
    pred_path = inputs / "predictions"
pred = json.loads(pred_path.read_bytes())
hold = json.loads(pathlib.Path(holdout_path).read_bytes())
y = hold["y"]
y_hat = pred["y_hat"]
if len(y) != len(y_hat) or not y:
    raise SystemExit("length_mismatch")
correct = sum(int(a == b) for a, b in zip(y_hat, y))
value = correct / len(y)
# Never copy pred.get("score") — agent claims are ignored.
(results / "reward.txt").write_text(
    json.dumps({{"schema": {REWARD_SCHEMA!r}, "value": float(value)}})
)
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
    """Two verifier runs; compare digests / reward values — not stdout."""
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

    ok = digests[0] == digests[1]
    delta = 0.0
    est = 0.0
    try:
        v0 = parse_reward(blobs[0])
        v1 = parse_reward(blobs[1])
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
    raise SandboxInfraError(f"sandbox_unavailable:unknown_kind:{check.kind}")
