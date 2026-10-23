"""Shared helpers for executable CodeCheck kinds."""

from __future__ import annotations

import re
from typing import Any

from coscientist.checks.sandbox import (
    INFRA_STATUSES,
    SOFT_ISOLATION_NOTE,
    FsIsolation,
    NetIsolation,
    SandboxStatus,
    limits_fully_held,
)
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import CheckResult, CheckRole, EvidenceRef, ReqStatus
from coscientist.core.errors import SandboxInfraError

_FLOAT_RE = re.compile(rb"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

# Unified scientific failure for malformed guest/agent structured output (B3).
MALFORMED_JUNIT = "malformed_output:junit"
MALFORMED_METRICS = "malformed_output:metrics"


def raise_if_infra(
    status: SandboxStatus,
    reason: str,
    *,
    net_isolation: NetIsolation = "none",
    fs_isolation: FsIsolation = "none",
) -> None:
    if status in INFRA_STATUSES:
        note = soft_note(net_isolation, fs_isolation)
        full = f"{reason}; {note}" if note else reason
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
    """Write via put_sandbox; return (artifact_id, digest, trusted).

    ``trusted`` only when both net and fs isolation are hard.
    """
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
    if limits_fully_held(net_isolation, fs_isolation):
        return None
    return SOFT_ISOLATION_NOTE


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
    """Parse junit XML; raises ValueError on malformed input."""
    try:
        from defusedxml import ElementTree as ET
    except ImportError:  # pragma: no cover — defusedxml is a hard dep
        import xml.etree.ElementTree as ET  # noqa: S405
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


def first_float(data: bytes) -> float:
    m = _FLOAT_RE.search(data)
    if m is None:
        raise ValueError("malformed_output:numeric_stdout")
    return float(m.group(0))


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
) -> dict[str, Any]:
    effect: dict[str, Any] = {
        "producer": "sandbox",
        "trusted": trusted,
        "net_isolation": net_isolation,
        "fs_isolation": fs_isolation,
        "core_evaluated": True,
        "countable": bool(attempt_id),
        "produced_by_call_id": f"{attempt_id or 'noattempt'}:sandbox:{check_id}",
        "artifact_id": art_id,
    }
    if note:
        effect["note"] = note
    if extra:
        effect.update(extra)
    return effect


def protect_paths_for(artifacts: LocalArtifactStore) -> list[str]:
    """Host CAS/meta paths the sandbox must not be able to rewrite."""
    return [str(artifacts.meta), str(artifacts.cas), str(artifacts.root)]
