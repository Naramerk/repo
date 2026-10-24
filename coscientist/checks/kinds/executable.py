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
from typing import Any

from coscientist.checks.catalog import (
    BUILTIN_METRIC_SCORER_ARGV,
    BUILTIN_TESTS_PASS_ARGV,
    GUEST_PYTHON_ARGV0,
    ISOLATED_PYTHON_ARGV0_TOKENS,
    LOG_SNIPPET_BYTES,
    VERIFIER_SCORER_GUEST_PATH,
)
from coscientist.checks.kinds._util import (
    MALFORMED_JUNIT,
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
    TIMEOUT_KINDS,
    SandboxResult,
    SandboxStatus,
    agent_input_name_allowed,
    is_plain_filename,
    limits_from_check,
    require_isolation,
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

REWARD_NAME = "reward.txt"
REWARD_SCHEMA = "coscientist.reward.v1"
JUNIT_NAME = "junit.xml"
DIGEST_NAME = "out.digest"

_TESTS_PASS_NOTE = "tamper-able, not counted; tamper-proof tests = M2"


def agent_filename_allowed(name: str, allowlist: frozenset[str]) -> bool:
    """Allowlist-only: ``name`` must be an exact member of ``allowlist``."""
    return agent_input_name_allowed(name, allowlist)


def _input_allowlist(check: CodeCheck) -> frozenset[str]:
    """Per-check allowlist of agent input file names (exact tokens).

    ``None`` (field absent) → mount nothing. Entries must already be plain
    file names — a path-like entry is a catalog error, never normalized.
    """
    if check.input_allowlist is None:
        return frozenset()
    for n in check.input_allowlist:
        if not is_plain_filename(n):
            raise CatalogError(f"input_allowlist_entry_not_plain_filename:{n!r}")
    return frozenset(check.input_allowlist)


def _argv_for(check: CodeCheck) -> list[str]:
    """Exactly one of ``argv`` / ``command``; both set is ambiguous → refuse.

    Neither set returns ``[]`` (callers raise ``missing_command``).
    """
    if check.argv is not None and check.command is not None:
        raise CatalogError("check_argv_and_command_both_set")
    if check.argv is not None:
        return list(check.argv)
    if check.command is not None:
        return [check.command]
    return []


def _require_attempt_id(attempt_id: str | None) -> str:
    if attempt_id is None:
        raise CatalogError("attempt_id_required_for_effect")
    return attempt_id


def _limits(check: CodeCheck):
    missing = [
        n
        for n, v in (
            ("timeout_s", check.timeout_s),
            ("memory_mb", check.memory_mb),
            ("max_output_bytes", check.max_output_bytes),
            ("pids_max", check.pids_max),
            ("rlimit_nproc", check.rlimit_nproc),
            ("reap_timeout_s", check.reap_timeout_s),
            ("output_drain_join_s", check.output_drain_join_s),
        )
        if v is None
    ]
    if missing:
        raise CatalogError("sandbox_limits_missing_required:" + ",".join(missing))
    timeout_s = check.timeout_s
    memory_mb = check.memory_mb
    max_output_bytes = check.max_output_bytes
    pids_max = check.pids_max
    rlimit_nproc = check.rlimit_nproc
    reap_timeout_s = check.reap_timeout_s
    output_drain_join_s = check.output_drain_join_s
    if (
        timeout_s is None
        or memory_mb is None
        or max_output_bytes is None
        or pids_max is None
        or rlimit_nproc is None
        or reap_timeout_s is None
        or output_drain_join_s is None
    ):
        raise CatalogError("sandbox_limits_missing_required:internal")
    try:
        return limits_from_check(
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            max_output_bytes=max_output_bytes,
            pids_max=pids_max,
            rlimit_nproc=rlimit_nproc,
            allow_network=check.allow_network,
            allow_cgroup_degraded=check.allow_cgroup_degraded,
            reap_timeout_s=reap_timeout_s,
            output_drain_join_s=output_drain_join_s,
        )
    except CatalogError:
        raise
    except (TypeError, ValueError) as e:
        raise CatalogError(f"sandbox_limits_invalid:{e}") from e


def _ensure_python_isolated(argv: list[str]) -> list[str]:
    """Force ``python3 -I`` so /inputs is never on sys.path via site/PYTHONPATH.

    Exact argv[0] tokens only — no Path.name / path-suffix heuristics.
    """
    if not argv:
        return argv
    if argv[0] in ISOLATED_PYTHON_ARGV0_TOKENS:
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
    name_allowlist: frozenset[str],
) -> tuple[dict[str, bytes], list[EvidenceRef]]:
    """Collect agent declared outputs with digests (B1 step 1).

    ``name_allowlist`` is required and allowlist-only (empty → collect nothing).
    """
    inputs: dict[str, bytes] = {}
    evidence: list[EvidenceRef] = []
    allow = name_allowlist
    if slots is not None:
        selected = [(s, out.outputs[s]) for s in slots if s in out.outputs]
    else:
        selected = list(out.outputs.items())
    # Allowlist filter (exact names): non-allowlisted outputs are not mounted.
    items = [
        (slot, handle)
        for slot, handle in selected
        if agent_filename_allowed(slot_filename(slot), allow)
    ]
    for slot, handle in items:
        fname = slot_filename(slot)
        meta = artifacts.get_meta(handle.artifact_id)
        if meta is None:
            raise SandboxInfraError(
                f"sandbox_unavailable:artifact_missing:{handle.artifact_id}"
            )
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


def _timeout_kind_for_role(result: SandboxResult, role: CheckRole) -> str:
    """Host-classified kind; missing CPU measurement → SandboxInfraError."""
    from coscientist.checks.sandbox import classify_guest_timeout

    if result.status is not SandboxStatus.TIMEOUT:
        raise SandboxInfraError("sandbox_unavailable:non_timeout_status")
    if not result.reason:
        raise SandboxInfraError("sandbox_unavailable:missing_timeout_reason")
    if result.idle_cpu_wall_ratio is None:
        raise SandboxInfraError("sandbox_unavailable:missing_idle_ratio")
    kind = classify_guest_timeout(
        kill_cause=result.timeout_kill,
        duration_s=float(result.duration_s),
        cpu_usage_s=result.cpu_usage_s,
        idle_ratio=float(result.idle_cpu_wall_ratio),
    )
    if kind not in TIMEOUT_KINDS:
        raise SandboxInfraError(f"sandbox_unavailable:bad_timeout_kind:{kind!r}")
    return kind


def _effect_extra(*pairs: tuple[str, Any]) -> dict[str, Any]:
    """Build extras dict; omit keys with None (sandbox_effect refuses None)."""
    out: dict[str, Any] = {}
    for k, v in pairs:
        if v == "":
            raise CatalogError(f"effect_extra_empty_string:{k}")
        if v is not None:
            out[k] = v
    return out


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
    aid = _require_attempt_id(attempt_id)
    allow = _input_allowlist(check)
    slots = [check.output] if check.output else None
    inputs, _ev = _collect_declared_outputs(
        out, artifacts, slots, name_allowlist=allow
    )
    mount_digest = jail_mount_digest(inputs)
    # Jail choice is not role-based: agent argv always runs in run_sandbox.
    # Catalog scorers use run_verifier from tests_pass / metric / rerun paths.
    result = run_sandbox(
        argv, limits=_limits(check), inputs=inputs, env={}
    )
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
        attempt_id=aid,
        check_id=check_id,
            name="sandbox.log",
        body=log_body,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        cgroup_limits_held=result.cgroup_limits_held,
    )
    note = merge_detail(
        result.note, soft_note(result.net_isolation, result.fs_isolation)
    )
    # Guest timeout: never countable; classified host-side (idle/cpu/wall).
    if result.status is SandboxStatus.TIMEOUT:
        kind = _timeout_kind_for_role(result, role)
        refute_unresolved = role == "prediction"
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(kind, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                countable=False,
                timeout_kind=kind,
                refute_unresolved=refute_unresolved,
                extra=_effect_extra(
                    ("cpu_usage_s", result.cpu_usage_s),
                    ("wall_s", result.duration_s),
                    ("jail_inputs_digest", mount_digest),
                ),
            ),
        )
    # Guest-caused output flood — scientific not_met (still countable when bound).
    if result.status is SandboxStatus.OUTPUT_EXCEEDED:
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(result.reason, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=trusted,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                countable=True,
            ),
        )
    ok = result.status is SandboxStatus.OK and result.exit_code == 0
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        required=True,
        evidence=[EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(None if ok else result.reason, note),
        effect=sandbox_effect(
            attempt_id=aid,
            check_id=check_id,
            art_id=art_id,
            trusted=trusted,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
            countable=True,
        ),
    )


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
    # Stdlib site helpers are required — AttributeError is infra, not soft-empty.
    try:
        sites.extend(site.getsitepackages())
        sites.append(site.getusersitepackages())
    except AttributeError as e:
        raise SandboxInfraError(
            f"sandbox_unavailable:site_helpers_missing:{e}"
        ) from e
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
            raise ImportError("agent module name not importable: " + str(py))
        spec = importlib.util.spec_from_file_location(name, py)
        if spec is None or spec.loader is None:
            raise ImportError("agent module spec unavailable: " + str(py))
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
    if not argv:
        # Same as exec_runs: never invent a command from catalog tests alone.
        raise SandboxInfraError("sandbox_unavailable:missing_command")
    catalog_tests = _catalog_tests_bytes(check)
    use_catalog_pytest = (
        catalog_tests is not None and tuple(argv) == BUILTIN_TESTS_PASS_ARGV
    )

    aid = _require_attempt_id(attempt_id)
    allow = _input_allowlist(check)
    # Agent code under /inputs (allowlisted names only); catalog tests under /tests.
    code_inputs, ev = _collect_declared_outputs(
        out, artifacts, name_allowlist=allow
    )
    collect = [JUNIT_NAME]
    scorer_bytes = None

    if use_catalog_pytest:
        scorer_bytes = _tests_pass_launcher()
        guest_argv = [GUEST_PYTHON_ARGV0, "-I", VERIFIER_SCORER_GUEST_PATH]
    else:
        # Non-builtin argv: no basename rewrite (pytest/py.test → launcher).
        guest_argv = _ensure_python_isolated(list(argv))

    result = run_verifier(
        guest_argv,
        inputs=code_inputs,
        limits=_limits(check),
        tests=catalog_tests,
        scorer_bytes=scorer_bytes,
        collect=collect,
        extra_ro_files={},
        input_allowlist=allow,
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    if result.status is SandboxStatus.TIMEOUT:
        return guest_timeout_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            attempt_id=aid,
            result=result,
            evidence=ev,
            jail_inputs_digest=jail_mount_digest(code_inputs),
        )
    note = merge_detail(
        merge_detail(
            result.note, soft_note(result.net_isolation, result.fs_isolation)
        ),
        _TESTS_PASS_NOTE,
    )
    # Status handling mirrors exec_runs / rerun: non-OK scientific → FAILED.
    if result.status is SandboxStatus.OUTPUT_EXCEEDED:
        art_id, digest, _t = store_log(
            artifacts,
            attempt_id=aid,
            check_id=check_id,
            body=b"output_exceeded\n" + result.stderr,
            name="tests_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            cgroup_limits_held=result.cgroup_limits_held,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(result.reason, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                countable=False,
                observation_only=True,
            ),
        )
    if result.status is not SandboxStatus.OK:
        art_id, digest, _t = store_log(
            artifacts,
            attempt_id=aid,
            check_id=check_id,
            body=(
                b"exit="
                + str(result.exit_code).encode()
                + b"\n"
                + result.stderr
            ),
            name="tests_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            cgroup_limits_held=result.cgroup_limits_held,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(result.reason, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                countable=False,
                observation_only=True,
            ),
        )
    raw = result.result_files.get(JUNIT_NAME)
    if raw is None:
        art_id, digest, _t = store_log(
            artifacts,
            attempt_id=aid,
            check_id=check_id,
            body=b"missing junit\n" + result.stderr,
            name="tests_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            cgroup_limits_held=result.cgroup_limits_held,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(MALFORMED_JUNIT, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                countable=False,
                observation_only=True,
            ),
        )
    try:
        failures, tests = parse_junit(raw)
    except ValueError:
        art_id, digest, _t = store_log(
            artifacts,
            attempt_id=aid,
            check_id=check_id,
            body=raw[:LOG_SNIPPET_BYTES],
            name="tests_eval.log",
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            cgroup_limits_held=result.cgroup_limits_held,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(MALFORMED_JUNIT, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=result.net_isolation,
                fs_isolation=result.fs_isolation,
                note=note,
                countable=False,
                observation_only=True,
            ),
        )
    if check.min_tests is None:
        raise CatalogError("tests_pass: min_tests required")
    min_tests = int(check.min_tests)
    ok = failures == 0 and tests >= min_tests
    summary = (
        f"tests={tests} failures={failures} min_tests={min_tests} ok={ok} "
        f"net={result.net_isolation} fs={result.fs_isolation}\n"
    ).encode()
    art_id, digest, _t = store_log(
        artifacts,
        attempt_id=aid,
        check_id=check_id,
        body=summary + raw[:LOG_SNIPPET_BYTES],
        name="tests_eval.log",
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        cgroup_limits_held=result.cgroup_limits_held,
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        required=True,
        evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(
            None if ok else f"failures={failures} tests={tests}", note
        ),
        effect=sandbox_effect(
            attempt_id=aid,
            check_id=check_id,
            art_id=art_id,
            trusted=False,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
            countable=False,
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
    argv = _argv_for(check)
    if not argv:
        raise CatalogError("sandbox_unavailable:missing_command")
    # output / op required by CodeCheck model_validator for this kind.
    pred_slot = check.output
    if not isinstance(pred_slot, str) or not pred_slot:
        raise CatalogError("metric_on_holdout: output required")
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
            required=True,
            evidence=[],
            detail="output_not_submitted",
        )
    try:
        p_meta = artifacts.get_meta(predictions.artifact_id)
        if p_meta is None:
            raise SandboxInfraError(
                f"sandbox_unavailable:artifact_missing:{predictions.artifact_id}"
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
            required=True,
            evidence=[
                EvidenceRef(artifact_id=predictions.artifact_id, digest=p_meta.sha256),
            ],
            detail=f"malformed_output:predictions:{e}",
        )

    aid = _require_attempt_id(attempt_id)

    # Catalog scorer only — python -I; predictions are JSON data under /inputs.
    allow = _input_allowlist(check)
    scorer_bytes = None
    if tuple(argv) == BUILTIN_METRIC_SCORER_ARGV:
        scorer_bytes = _default_metric_scorer().encode()
        guest_argv = [GUEST_PYTHON_ARGV0, "-I", VERIFIER_SCORER_GUEST_PATH]
    else:
        guest_argv = _ensure_python_isolated(list(argv))

    result = run_verifier(
        guest_argv,
        inputs={"predictions.json": pred_bytes},
        holdout=holdout_bytes,
        scorer_bytes=scorer_bytes,
        limits=_limits(check),
        collect=[REWARD_NAME],
        extra_ro_files={},
        input_allowlist=allow,
    )
    raise_if_infra(
        result.status,
        result.reason,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=result.note,
    )
    if result.status is SandboxStatus.TIMEOUT:
        return guest_timeout_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            attempt_id=aid,
            result=result,
            evidence=[
                EvidenceRef(
                    artifact_id=predictions.artifact_id, digest=p_meta.sha256
                ),
            ],
            jail_inputs_digest=jail_mount_digest({"predictions.json": pred_bytes}),
        )
    note = merge_detail(
        result.note, soft_note(result.net_isolation, result.fs_isolation)
    )

    reward = result.result_files.get(REWARD_NAME)
    if reward is None:
        raise CatalogError("sandbox_unavailable:catalog_scorer_missing_reward")
    try:
        left = parse_reward(reward)
    except ValueError as e:
        # Catalog scorer output is infrastructure, not a scientific FAILED.
        raise CatalogError(
            f"sandbox_unavailable:catalog_scorer_bad_reward:{e}"
        ) from e

    target = check.target
    # op required by CodeCheck model_validator (checked before sandbox run).
    op = check.op
    if op is None:
        raise CatalogError("metric_on_holdout: op required")
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
        attempt_id=aid,
        check_id=check_id,
        name="sandbox.log",
        body=summary,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        cgroup_limits_held=result.cgroup_limits_held,
    )
    effect = sandbox_effect(
        attempt_id=aid,
        check_id=check_id,
        art_id=art_id,
        trusted=trusted,
        net_isolation=result.net_isolation,
        fs_isolation=result.fs_isolation,
        note=note,
        countable=True,
        extra=merge_numeric_extra(prov_fields, est=float(left)),
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        required=True,
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
    if check.rerun_tol is None:
        raise CatalogError("reproducible_rerun: rerun_tol required")
    aid = _require_attempt_id(attempt_id)
    tol = float(check.rerun_tol)
    allow = _input_allowlist(check)
    inputs, ev = _collect_declared_outputs(out, artifacts, name_allowlist=allow)
    from coscientist.checks.sandbox import FsIsolation, NetIsolation

    _RERUN_NOTE = "reproducible_rerun is observation-only in M1; not counted"
    blobs: list[bytes] = []
    digests: list[str] = []
    logs: list[bytes] = []
    # Bound from run 0; run 1 must report the identical isolation or it is infra.
    net_i: NetIsolation
    fs_i: FsIsolation
    cgroup_held: bool
    note: str | None = None
    for i in range(2):
        result = run_verifier(
            argv,
            inputs=inputs,
            limits=_limits(check),
            # Fail-closed collect: only files the guest is expected to write.
            collect=[DIGEST_NAME],
            extra_ro_files={},
            input_allowlist=allow,
        )
        raise_if_infra(
            result.status,
            result.reason,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=result.note,
        )
        run_net = require_isolation(result.net_isolation)
        run_fs = require_isolation(result.fs_isolation)
        if not isinstance(result.cgroup_limits_held, bool):
            raise SandboxInfraError(
                "sandbox_unavailable:cgroup_limits_held_not_bool:"
                f"{type(result.cgroup_limits_held).__name__}"
            )
        if i == 0:
            net_i, fs_i = run_net, run_fs
            cgroup_held = result.cgroup_limits_held
        else:
            if (run_net, run_fs) != (net_i, fs_i):
                raise SandboxInfraError(
                    "sandbox_unavailable:isolation_changed_between_reruns:"
                    f"{net_i}/{fs_i}->{run_net}/{run_fs}"
                )
            cgroup_held = cgroup_held and result.cgroup_limits_held
        note = merge_detail(
            merge_detail(result.note, soft_note(net_i, fs_i)), _RERUN_NOTE
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
                attempt_id=aid,
                check_id=check_id,
                name="sandbox.log",
                body=b"\n".join(logs),
                net_isolation=net_i,
                fs_isolation=fs_i,
                cgroup_limits_held=cgroup_held,
            )
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                required=True,
                evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail(result.reason, note),
                effect=sandbox_effect(
                    attempt_id=aid,
                    check_id=check_id,
                    art_id=art_id,
                    trusted=False,
                    net_isolation=net_i,
                    fs_isolation=fs_i,
                    note=note,
                    countable=False,
                    observation_only=True,
                ),
            )
        # Expected guest output is out.digest only — no soft fallback to reward.txt.
        if DIGEST_NAME not in result.result_files:
            art_id, digest, _trusted = store_log(
                artifacts,
                attempt_id=aid,
                check_id=check_id,
                name="sandbox.log",
                body=b"\n".join(logs),
                net_isolation=net_i,
                fs_isolation=fs_i,
                cgroup_limits_held=cgroup_held,
            )
            return base_result(
                check_id=check_id,
                template_id=template_id,
                role=role,
                ok=False,
                required=True,
                evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
                detail=merge_detail(MALFORMED_REWARD, note),
                effect=sandbox_effect(
                    attempt_id=aid,
                    check_id=check_id,
                    art_id=art_id,
                    trusted=False,
                    net_isolation=net_i,
                    fs_isolation=fs_i,
                    note=note,
                    countable=False,
                    observation_only=True,
                ),
            )
        blob = result.result_files[DIGEST_NAME]
        blobs.append(blob)
        digests.append(sha256_hex(blob))

    try:
        v0 = parse_reward(blobs[0])
        v1 = parse_reward(blobs[1])
    except ValueError:
        art_id, digest, _trusted = store_log(
            artifacts,
            attempt_id=aid,
            check_id=check_id,
            name="sandbox.log",
            body=b"\n".join(logs) + b"\n" + b"\n".join(blobs),
            net_isolation=net_i,
            fs_isolation=fs_i,
            cgroup_limits_held=cgroup_held,
        )
        return base_result(
            check_id=check_id,
            template_id=template_id,
            role=role,
            ok=False,
            required=True,
            evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
            detail=merge_detail(MALFORMED_REWARD, note),
            effect=sandbox_effect(
                attempt_id=aid,
                check_id=check_id,
                art_id=art_id,
                trusted=False,
                net_isolation=net_i,
                fs_isolation=fs_i,
                note=note,
                countable=False,
                observation_only=True,
            ),
        )
    delta = abs(v0 - v1)
    ok = delta <= tol
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
        attempt_id=aid,
        check_id=check_id,
        name="sandbox.log",
        body=summary + b"\n" + b"\n".join(logs),
        net_isolation=net_i,
        fs_isolation=fs_i,
        cgroup_limits_held=cgroup_held,
    )
    # No est — agent-chosen constant reward must not enter a countable effect.
    effect = sandbox_effect(
        attempt_id=aid,
        check_id=check_id,
        art_id=art_id,
        trusted=False,
        net_isolation=net_i,
        fs_isolation=fs_i,
        note=note,
        countable=False,
        observation_only=True,
    )
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=ok,
        required=True,
        evidence=[*ev, EvidenceRef(artifact_id=art_id, digest=digest)],
        detail=merge_detail(
            None if ok else f"digest_mismatch {digests[0]} != {digests[1]}", note
        ),
        effect=effect,
    )


def _eval_exec_runs(
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
    del prov_fields
    return evaluate_exec_runs(
        check,
        template_id=template_id,
        check_id=check_id,
        out=out,
        artifacts=artifacts,
        role=role,
        attempt_id=attempt_id,
    )


def _eval_tests_pass(
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
    del prov_fields
    return evaluate_tests_pass(
        check,
        template_id=template_id,
        check_id=check_id,
        out=out,
        artifacts=artifacts,
        role=role,
        attempt_id=attempt_id,
    )


_EXECUTABLE_DISPATCH = {
    "exec_runs": _eval_exec_runs,
    "tests_pass": _eval_tests_pass,
    "metric_on_holdout": evaluate_metric_on_holdout,
    "reproducible_rerun": evaluate_reproducible_rerun,
}
EXECUTABLE_KINDS = frozenset(_EXECUTABLE_DISPATCH)


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
    handler = _EXECUTABLE_DISPATCH.get(check.kind)
    if handler is None:
        raise SandboxInfraError(f"sandbox_unavailable:unknown_kind:{check.kind}")
    return handler(
        check,
        template_id=template_id,
        check_id=check_id,
        out=out,
        artifacts=artifacts,
        role=role,
        attempt_id=attempt_id,
        prov_fields=prov_fields,
    )
