"""Executable CodeCheck kinds (DESIGN_M1 §7).

B1 pattern: collect agent declared outputs (sha256); run catalog scorer / task
tests in a second fresh verifier jail (agent data/scorer/holdout/tests RO;
results RW only); read reward/result files — never guest stdout.
``metric_on_holdout`` never executes agent code (predictions are data).
``tests_pass`` imports agent modules → ``trusted=False``, ``countable=False``
(observation; tamper-proof tests = M2).
Fail-closed without hard isolation. Holdout labels come from the catalog only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coscientist.checks.kinds._util import (
    MALFORMED_JUNIT,
    MALFORMED_METRICS,
    MALFORMED_REWARD,
    base_result,
    cmp,
    guest_timeout_result,
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
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.errors import CatalogError, SandboxInfraError
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    CheckResult,
    CheckRole,
    CodeCheck,
    EvidenceRef,
)

EXECUTABLE_KINDS = frozenset(
    {"exec_runs", "tests_pass", "metric_on_holdout", "reproducible_rerun"}
)

REWARD_NAME = "reward.txt"
REWARD_SCHEMA = "coscientist.reward.v1"
JUNIT_NAME = "junit.xml"
DIGEST_NAME = "out.digest"

_TESTS_PASS_NOTE = "tamper-able, not counted; tamper-proof tests = M2"

# Agent-mounted filenames: reject import-hooks / stdlib shadows / pytest plugs.
_BLOCKED_AGENT_NAMES = frozenset(
    {
        "sitecustomize.py",
        "usercustomize.py",
        "conftest.py",
        "os.py",
        "sys.py",
        "builtins.py",
        "importlib.py",
        "pytest.py",
        "py.py",
        "pathlib.py",
        "json.py",
        "typing.py",
        "asyncio.py",
        "subprocess.py",
        "ctypes.py",
        "resource.py",
        "signal.py",
    }
)
_SAFE_AGENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_+-]+)?$")


def agent_filename_allowed(name: str) -> bool:
    """Defense-in-depth denylist (hooks / stdlib shadows). Prefer allowlists."""
    base = name.rsplit("/", 1)[-1]
    if not base or base.startswith("."):
        return False
    if base.endswith(".pth") or base in _BLOCKED_AGENT_NAMES:
        return False
    if ".." in name or name.startswith("/"):
        return False
    return bool(_SAFE_AGENT_NAME.match(base))


def _input_allowlist(check: CodeCheck) -> frozenset[str]:
    """Per-check allowlist of agent input basenames. Empty → mount nothing."""
    if check.input_allowlist is not None:
        return frozenset(Path(n).name for n in check.input_allowlist if n)
    if check.kind == "metric_on_holdout":
        return frozenset({"predictions.json"})
    if check.kind == "exec_runs" and check.output:
        return frozenset({slot_filename(check.output), Path(check.output).name})
    # tests_pass / reproducible_rerun: fail-closed unless catalog lists names.
    return frozenset()


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


def _ensure_python_isolated(argv: list[str]) -> list[str]:
    """Force ``python3 -I`` so /inputs is never on sys.path via site/PYTHONPATH."""
    if not argv:
        return argv
    if _path_name(argv[0]) in {"python", "python3"}:
        if "-I" in argv:
            return list(argv)
        return [argv[0], "-I", *argv[1:]]
    return list(argv)


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
    *,
    name_allowlist: frozenset[str] | None = None,
) -> tuple[dict[str, bytes], list[EvidenceRef]]:
    """Collect agent declared outputs with digests (B1 step 1).

    ``name_allowlist`` is allowlist-only (``None``/empty → collect nothing).
    Denylist still rejects import hooks even if mistakenly allowlisted.
    """
    inputs: dict[str, bytes] = {}
    evidence: list[EvidenceRef] = []
    allow = name_allowlist if name_allowlist is not None else frozenset()
    items = (
        ((s, out.outputs[s]) for s in slots if s in out.outputs)
        if slots is not None
        else out.outputs.items()
    )
    for slot, handle in items:
        fname = slot_filename(slot)
        if fname not in allow and slot not in allow:
            continue
        if not agent_filename_allowed(fname):
            continue
        meta = artifacts.get_meta(handle.artifact_id)
        if meta is None:
            continue
        data = artifacts.read(handle.artifact_id)
        inputs[fname] = data
        evidence.append(
            EvidenceRef(artifact_id=handle.artifact_id, digest=meta.sha256)
        )
    return inputs, evidence


def jail_mount_digest(inputs: dict[str, bytes]) -> str:
    """Content digest of bytes actually mounted into the jail (normalized names)."""
    return sha256_hex(
        canonical_json(
            {
                "mounted": {
                    str(k): sha256_hex(v) for k, v in sorted(inputs.items())
                }
            }
        )
    )


def _timeout_kind_for_role(result: Any, role: CheckRole) -> str:
    """Host-classified kind; refute + no cpu.stat → idle (untestable)."""
    from coscientist.checks.sandbox import classify_guest_timeout

    if getattr(result, "status", None) is not SandboxStatus.TIMEOUT:
        return "wall_timeout"
    raw = str(getattr(result, "reason", "") or "wall_timeout")
    # Re-classify with role so missing cgroup elevates refute to idle.
    return classify_guest_timeout(
        kill_cause=getattr(result, "timeout_kill", None),
        duration_s=float(getattr(result, "duration_s", 0.0) or 0.0),
        cpu_usage_s=getattr(result, "cpu_usage_s", None),
        refute_role=(role == "prediction"),
    ) or raw


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
    allow = _input_allowlist(check)
    slots = [check.output] if check.output else None
    inputs, _ev = _collect_declared_outputs(
        out, artifacts, slots, name_allowlist=allow
    )
    mount_digest = jail_mount_digest(inputs)
    # Refute/prediction: verifier jail (catalog command; agent inputs RO).
    if role == "prediction":
        holdout_b = (
            canonical_json(check.holdout) if check.holdout is not None else None
        )
        result = run_verifier(
            argv,
            inputs=inputs or {},
            limits=_limits(check),
            holdout=holdout_b,
            input_allowlist=allow,
            collect=(),
        )
    else:
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
    # Guest timeout: never countable; classified host-side (idle/cpu/wall).
    if result.status is SandboxStatus.TIMEOUT:
        kind = _timeout_kind_for_role(result, role)
        refute_unresolved = role == "prediction"
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(kind, note),
            effect=sandbox_effect(
                attempt_id=attempt_id,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                timeout_kind=kind,
                refute_unresolved=refute_unresolved,
                extra={
                    "cpu_usage_s": result.cpu_usage_s,
                    "wall_s": result.duration_s,
                    "jail_inputs_digest": mount_digest,
                },
            ),
        )
    # Guest-caused output flood — scientific not_met (still countable when bound).
    if result.status is SandboxStatus.OUTPUT_EXCEEDED:
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


def _tests_pass_launcher() -> bytes:
    """Catalog launcher: import pytest first; preload allowlisted agent modules.

    ``python -I`` ignores PYTHONPATH. Never put ``/inputs`` on ``sys.path``
    (no sitecustomize / ``.pth``). Register agent ``.py`` modules via
    ``importlib`` so catalog tests can ``import solution`` safely.
    """
    import site
    from pathlib import Path as P

    sites: list[str] = []
    try:
        sites.extend(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass
    try:
        sites.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    sites = [p for p in sites if p and P(p).is_dir()]
    return f"""
import importlib.util
import sys
from pathlib import Path
for p in {sites!r}:
    if p and p not in sys.path:
        sys.path.append(p)
import pytest
# Preload agent modules by path — /inputs stays off sys.path.
inputs = Path("/inputs")
if inputs.is_dir():
    for py in sorted(inputs.glob("*.py")):
        name = py.stem
        if not name.isidentifier() or name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(name, py)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
raise SystemExit(
    pytest.main(
        [
            "--noconftest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--junitxml=/results/{JUNIT_NAME}",
            "/tests",
        ]
    )
)
""".encode()


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
    """Catalog/task tests vs agent code; observation only (not countable)."""
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
                "countable": False,
                "observation_only": True,
                "produced_by_call_id": f"{attempt_id or 'noattempt'}:core:{check_id}",
                "note": _TESTS_PASS_NOTE,
            },
        )

    allow = _input_allowlist(check)
    # Agent code under /inputs (allowlisted names only); catalog tests under /tests.
    code_inputs, ev = _collect_declared_outputs(
        out, artifacts, name_allowlist=allow
    )
    collect = [JUNIT_NAME]
    scorer_bytes = None

    if use_catalog_pytest:
        scorer_bytes = _tests_pass_launcher()
        guest_argv = ["python3", "-I", "/scorer"]
    else:
        guest_argv = _ensure_python_isolated(list(argv))
        if guest_argv and _path_name(guest_argv[0]) in {"pytest", "py.test"}:
            scorer_bytes = _tests_pass_launcher()
            guest_argv = ["python3", "-I", "/scorer"]

    result = run_verifier(
        guest_argv,
        inputs=code_inputs,
        limits=_limits(check),
        tests=catalog_tests,
        scorer_bytes=scorer_bytes,
        collect=collect,
        input_allowlist=allow,
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    timed = guest_timeout_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        attempt_id=attempt_id,
        result=result,
        evidence=ev,
        jail_inputs_digest=jail_mount_digest(code_inputs),
    )
    if timed is not None:
        return timed
    note = merge_detail(
        result.note or soft_note(result.net_isolation, result.fs_isolation),
        _TESTS_PASS_NOTE,
    )
    raw = result.result_files.get(JUNIT_NAME)
    if raw is None:
        art_id, digest, _t = store_log(
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
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                observation_only=True,
            ),
        )
    try:
        failures, tests = parse_junit(raw)
    except ValueError:
        art_id, digest, _t = store_log(
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
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                observation_only=True,
            ),
        )
    min_tests = int(check.min_tests) if check.min_tests is not None else 1
    ok = failures == 0 and tests >= min_tests
    summary = (
        f"tests={tests} failures={failures} min_tests={min_tests} ok={ok} "
        f"net={result.net_isolation} fs={result.fs_isolation}\n"
    ).encode()
    art_id, digest, _t = store_log(
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
            trusted=False,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
            observation_only=True,
        ),
    )


def _validate_predictions(pred_bytes: bytes) -> dict[str, Any]:
    """Schema-validate agent predictions as data (never executed)."""
    try:
        doc = json.loads(pred_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"predictions_not_json:{e.__class__.__name__}") from e
    if not isinstance(doc, dict):
        raise ValueError("predictions_not_object")
    y_hat = doc.get("y_hat")
    if not isinstance(y_hat, list) or not y_hat:
        raise ValueError("predictions_missing_y_hat")
    return doc


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
    """metric = catalog scorer(predictions data, catalog holdout) — no agent code."""
    pred_slot = check.output or "predictions"
    predictions = out.outputs.get(pred_slot)
    # Agent-supplied holdout slots are ignored — labels come from the catalog only.
    if check.holdout is None:
        raise SandboxInfraError("sandbox_unavailable:holdout_missing")
    frozen = check.holdout_digest
    if not frozen:
        raise CatalogError("metric_on_holdout: holdout_digest missing")
    holdout_bytes = canonical_json(check.holdout)
    holdout_digest = sha256_hex(holdout_bytes)
    if holdout_digest != frozen:
        raise CatalogError(
            f"metric_on_holdout: holdout_digest mismatch "
            f"(catalog={frozen} computed={holdout_digest})"
        )

    if predictions is None:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[],
            detail="output_not_submitted",
        )
    try:
        p_meta = artifacts.get_meta(predictions.artifact_id)
        if p_meta is None:
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                evidence=[],
                detail="artifact_missing",
            )
        pred_bytes = artifacts.read(predictions.artifact_id)
    except OSError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:host_io:{e.__class__.__name__}"
        ) from e

    try:
        _validate_predictions(pred_bytes)
    except ValueError as e:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[
                EvidenceRef(artifact_id=predictions.artifact_id, digest=p_meta.sha256),
            ],
            detail=f"malformed_output:predictions:{e}",
        )

    argv = _argv_for(check)
    if not argv:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            evidence=[
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

    # Catalog scorer only — python -I; predictions are JSON data under /inputs.
    allow = _input_allowlist(check)
    scorer_bytes = None
    if argv == ["catalog:metric"] or argv[0] == "catalog:metric":
        scorer_bytes = _default_metric_scorer().encode()
        guest_argv = ["python3", "-I", "/scorer"]
    else:
        guest_argv = _ensure_python_isolated(list(argv))

    result = run_verifier(
        guest_argv,
        inputs={"predictions.json": pred_bytes},
        holdout=holdout_bytes,
        scorer_bytes=scorer_bytes,
        limits=_limits(check),
        collect=[REWARD_NAME],
        input_allowlist=allow,
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    timed = guest_timeout_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        attempt_id=attempt_id,
        result=result,
        evidence=[
            EvidenceRef(artifact_id=predictions.artifact_id, digest=p_meta.sha256),
        ],
        jail_inputs_digest=jail_mount_digest({"predictions.json": pred_bytes}),
    )
    if timed is not None:
        return timed
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
    """Compute accuracy(y_hat, catalog holdout.y); never exec agent code."""
    return f"""
import json, os, pathlib
results = pathlib.Path(os.environ["COSCIENTIST_RESULTS"])
inputs = pathlib.Path(os.environ["COSCIENTIST_INPUTS"])
holdout_path = os.environ["COSCIENTIST_HOLDOUT"]
pred_path = inputs / "predictions.json"
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
    """Two verifier jails; compare digests — observation only in M1 (not countable).

    Agent argv runs under ``python3 -I``; ``/inputs`` is never on ``sys.path``.
    A constant reward/digest match must not contribute ``est`` toward ``k_sup``.
    """
    _ = prov_fields  # unused: never copy agent-derived est into a countable effect
    argv = _ensure_python_isolated(_argv_for(check))
    if not argv:
        raise SandboxInfraError("sandbox_unavailable:missing_command")
    tol = float(check.tolerance) if check.tolerance is not None else float(
        check.rerun_tol if check.rerun_tol is not None else 0.0
    )
    allow = _input_allowlist(check)
    inputs, ev = _collect_declared_outputs(out, artifacts, name_allowlist=allow)
    from typing import cast

    from coscientist.checks.sandbox import FsIsolation, NetIsolation

    _RERUN_NOTE = "reproducible_rerun is observation-only in M1; not counted"
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
            input_allowlist=allow,
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
        note = merge_detail(
            result.note or soft_note(net_i, fs_i), _RERUN_NOTE
        )
        logs.append(
            b"run="
            + str(i).encode()
            + b" exit="
            + str(result.exit_code).encode()
            + b"\n"
            + result.stderr
        )
        if result.status is not SandboxStatus.OK:
            art_id, digest, _trusted = store_log(
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
                    trusted=False,
                    net_isolation=net_i,
                    fs_isolation=fs_i,
                    note=note,
                    observation_only=True,
                ),
            )
        blob = result.result_files.get(DIGEST_NAME) or result.result_files.get(
            REWARD_NAME
        )
        if blob is None:
            art_id, digest, _trusted = store_log(
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
                    trusted=False,
                    net_isolation=net_i,
                    fs_isolation=fs_i,
                    note=note,
                    observation_only=True,
                ),
            )
        blobs.append(blob)
        digests.append(sha256_hex(blob))

    ok = digests[0] == digests[1]
    delta = 0.0
    try:
        v0 = parse_reward(blobs[0])
        v1 = parse_reward(blobs[1])
        delta = abs(v0 - v1)
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
            "observation_only": True,
        }
    )
    art_id, digest, _trusted = store_log(
        artifacts,
        attempt_id=attempt_id,
        check_id=check_id,
        body=summary + b"\n" + b"\n".join(logs),
        net_isolation=net_i,
        fs_isolation=fs_i,
    )
    # No est — agent-chosen constant reward must not enter a countable effect.
    effect = sandbox_effect(
        attempt_id=attempt_id,
        check_id=check_id,
        art_id=art_id,
        trusted=False,
        net_isolation=net_i,
        fs_isolation=fs_i,
        note=note,
        observation_only=True,
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
