"""Shared helpers for executable CodeCheck kinds."""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as ET

from coscientist.checks.sandbox import (
    INFRA_STATUSES,
    FsIsolation,
    NetIsolation,
    SandboxStatus,
    limits_fully_held,
    soft_isolation_note,
)
from coscientist.core.events.errors import SandboxInfraError
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import CheckResult, CheckRole, EvidenceRef, ReqStatus

MALFORMED_JUNIT = "malformed_output:junit"
MALFORMED_METRICS = "malformed_output:metrics"
MALFORMED_REWARD = "malformed_output:reward"


def raise_if_infra(
    status: SandboxStatus,
    reason: str,
    *,
    net_isolation: NetIsolation = "none",
    fs_isolation: FsIsolation = "none",
    note: str | None = None,
) -> None:
    if status in INFRA_STATUSES:
        n = note or soft_isolation_note(net_isolation, fs_isolation)
        full = f"{reason}; {n}" if n else reason
        raise SandboxInfraError(full)


def store_log(
    artifacts: LocalArtifactStore,
    *,
    attempt_id: str | None,
    check_id: str,
    body: bytes,
    name: str = "sandbox.log",
    net_isolation: NetIsolation = "none",
    fs_isolation: FsIsolation = "none",
) -> tuple[str, str, bool]:
    call = f"{attempt_id or 'noattempt'}:sandbox:{check_id}"
    art = artifacts.put_sandbox(
        body,
        make_tool_ref(call, tool="sandbox"),
        name=name,
        media_type="text/plain",
        role="log",
        net_isolation=net_isolation,
        fs_isolation=fs_isolation,
    )
    return art.artifact_id, art.sha256, limits_fully_held(net_isolation, fs_isolation)


def soft_note(
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation = "none",
) -> str | None:
    return soft_isolation_note(net_isolation, fs_isolation)


def merge_detail(detail: str | None, note: str | None) -> str | None:
    if note is None:
        return detail
    if detail is None or detail == "":
        return note
    return f"{detail}; {note}"


def base_result(
    *,
    check_id: str,
    template_id: str,
    role: CheckRole,
    ok: bool,
    evidence: list[EvidenceRef],
    detail: str | None,
    effect: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.MET if ok else ReqStatus.FAILED,
        is_code=True,
        required=True,
        role=role,
        evidence=evidence,
        detail=detail,
        effect=effect,
        used_sesoi=False,
    )


def parse_junit(raw: bytes) -> tuple[int, int]:
    try:
        root = ET.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"junit_parse:{e.__class__.__name__}") from e
    nodes = [root]
    if root.tag.endswith("testsuites"):
        nodes = list(root)
    failures = 0
    tests = 0
    for node in nodes:
        if not node.tag.endswith("testsuite") and node is not root:
            continue
        failures += int(node.attrib.get("failures", 0) or 0)
        failures += int(node.attrib.get("errors", 0) or 0)
        tests += int(node.attrib.get("tests", 0) or 0)
    if tests == 0 and root.tag.endswith("testsuite"):
        failures = int(root.attrib.get("failures", 0) or 0) + int(
            root.attrib.get("errors", 0) or 0
        )
        tests = int(root.attrib.get("tests", 0) or 0)
    return failures, tests


def pointer_get(doc: Any, pointer: str | None) -> Any:
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


def cmp(op: str, left: Any, right: Any, tolerance: float | None) -> bool:
    if op == "in":
        return left in right
    if tolerance is not None and isinstance(left, (int, float)) and isinstance(
        right, (int, float)
    ):
        if op == "==":
            return abs(float(left) - float(right)) <= tolerance
    ops = {
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
    }
    return ops[op](left, right)


def slot_filename(slot: str) -> str:
    return slot.replace("/", "_") or "input.bin"


def sandbox_effect(
    *,
    attempt_id: str | None,
    check_id: str,
    art_id: str,
    trusted: bool,
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
    note: str | None,
    extra: dict[str, Any] | None = None,
    countable: bool | None = None,
    observation_only: bool = False,
    timeout_kind: str | None = None,
    refute_unresolved: bool = False,
) -> dict[str, Any]:
    """Stamp sandbox provenance.

    ``observation_only`` / guest ``timeout_kind`` → never countable.
    ``refute_unresolved`` marks a refute-role check closed by timeout.
    """
    call = f"{attempt_id or 'noattempt'}:sandbox:{check_id}"
    if observation_only or timeout_kind:
        countable_flag = False
    elif countable is None:
        countable_flag = bool(attempt_id)
    else:
        countable_flag = bool(countable)
    effect: dict[str, Any] = {
        "producer": "sandbox",
        "trusted": trusted,
        "net_isolation": net_isolation,
        "fs_isolation": fs_isolation,
        "core_evaluated": True,
        "countable": countable_flag,
        "produced_by_call_id": call,
        "artifact_id": art_id,
    }
    if observation_only or timeout_kind:
        effect["observation_only"] = True
    if timeout_kind:
        effect["timeout_kind"] = str(timeout_kind)
    if refute_unresolved:
        effect["refute_unresolved"] = True
    if note:
        effect["note"] = note
    if extra:
        # Never let empty prov_fields overwrite binding keys.
        blocked = {
            "countable",
            "produced_by_call_id",
            "artifact_id",
            "trusted",
            "producer",
            "core_evaluated",
            "net_isolation",
            "fs_isolation",
            "observation_only",
            "timeout_kind",
            "refute_unresolved",
        }
        for k, v in extra.items():
            if k in blocked:
                continue
            if v is None or v == "":
                continue
            effect[k] = v
    return effect


def check_is_guest_timeout(check: Any) -> bool:
    """True when CheckResult.effect carries a host-classified guest timeout."""
    from coscientist.checks.sandbox import TIMEOUT_KINDS

    effect = getattr(check, "effect", None)
    if not isinstance(effect, dict):
        return False
    kind = effect.get("timeout_kind") or effect.get("reason")
    return kind in TIMEOUT_KINDS


def guest_timeout_result(
    *,
    check_id: str,
    template_id: str,
    role: CheckRole,
    attempt_id: str | None,
    result: Any,
    evidence: list[EvidenceRef] | None = None,
    jail_inputs_digest: str | None = None,
) -> CheckResult | None:
    """If ``result`` is a guest timeout, return a non-countable CheckResult."""
    from coscientist.checks.sandbox import SandboxStatus, classify_guest_timeout

    if getattr(result, "status", None) is not SandboxStatus.TIMEOUT:
        return None
    kind = classify_guest_timeout(
        kill_cause=getattr(result, "timeout_kill", None),
        duration_s=float(getattr(result, "duration_s", 0.0) or 0.0),
        cpu_usage_s=getattr(result, "cpu_usage_s", None),
        refute_role=(role == "prediction"),
    )
    note = merge_detail(
        soft_note(
            getattr(result, "net_isolation", "none"),
            getattr(result, "fs_isolation", "none"),
        ),
        getattr(result, "note", None),
    )
    art_id = ""
    extra: dict[str, Any] = {
        "cpu_usage_s": getattr(result, "cpu_usage_s", None),
        "wall_s": getattr(result, "duration_s", None),
    }
    if jail_inputs_digest:
        extra["jail_inputs_digest"] = jail_inputs_digest
    # Caller may have already stored a log; keep evidence only.
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=False,
        evidence=list(evidence or []),
        detail=merge_detail(kind, note),
        effect=sandbox_effect(
            attempt_id=attempt_id,
            check_id=check_id,
            art_id=art_id or f"timeout:{check_id}",
            trusted=False,
            net_isolation=getattr(result, "net_isolation", "none"),
            fs_isolation=getattr(result, "fs_isolation", "none"),
            note=note,
            timeout_kind=kind,
            refute_unresolved=(role == "prediction"),
            extra=extra,
        ),
    )


def merge_numeric_extra(
    prov_fields: dict[str, Any] | None,
    *,
    est: float,
) -> dict[str, Any]:
    """Safe numeric extras from prov_fields (no binding overwrites)."""
    out: dict[str, Any] = {"est": float(est), "kind": "point"}
    if not prov_fields:
        return out
    for k, v in prov_fields.items():
        if k in (
            "countable",
            "produced_by_call_id",
            "artifact_id",
            "trusted",
            "producer",
            "core_evaluated",
            "net_isolation",
            "fs_isolation",
            "est",
            "kind",
        ):
            continue
        if v is None or v == "":
            continue
        out[k] = v
    return out
