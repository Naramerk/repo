"""Shared helpers for executable CodeCheck kinds."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from coscientist.checks.sandbox import INFRA_STATUSES, SandboxStatus
from coscientist.core.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.domain import CheckResult, CheckRole, EvidenceRef, ReqStatus
from coscientist.core.errors import SandboxInfraError

_FLOAT_RE = re.compile(rb"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def raise_if_infra(status: SandboxStatus, reason: str) -> None:
    if status in INFRA_STATUSES:
        raise SandboxInfraError(reason)


def store_log(
    artifacts: LocalArtifactStore,
    *,
    attempt_id: str | None,
    check_id: str,
    body: bytes,
    name: str = "sandbox.log",
) -> tuple[str, str]:
    """Write sandbox/check log with producer=sandbox; return (artifact_id, digest)."""
    call = f"{attempt_id or 'noattempt'}:sandbox:{check_id}"
    art = artifacts.put_sandbox(
        body,
        make_tool_ref(call, tool="sandbox"),
        name=name,
        media_type="text/plain",
        role="log",
    )
    return art.artifact_id, art.sha256


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
    root = ET.fromstring(raw)
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
        raise SandboxInfraError("sandbox_crash:no_numeric_stdout")
    return float(m.group(0))


def slot_filename(slot: str) -> str:
    return slot.replace("/", "_") or "input.bin"
