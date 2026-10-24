"""Shared helpers for executable CodeCheck kinds."""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as ET

from coscientist.checks.sandbox import (
    INFRA_STATUSES,
    FsIsolation,
    NetIsolation,
    SandboxResult,
    SandboxStatus,
    limits_fully_held,
    soft_isolation_note,
)
from coscientist.core.events.errors import JournalFormatError, SandboxInfraError
from coscientist.core.events.upcasters import require_exact_int
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import CheckResult, CheckRole, EvidenceRef, ReqStatus

MALFORMED_JUNIT = "malformed_output:junit"
MALFORMED_METRICS = "malformed_output:metrics"
MALFORMED_REWARD = "malformed_output:reward"

# Platform sentinel when a sandbox call is not bound to an attempt yet.
_UNBOUND_ATTEMPT = "noattempt"
_DEFAULT_SANDBOX_LOG = "sandbox.log"


def _as_float(value: object, *, what: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise JournalFormatError(f"{what} must be numeric, got {type(value).__name__}")
    return float(value)  # audit-ok[coerce_float]: coerce after int|float type gate (bool rejected)


def _as_int_attr(raw: object, *, what: str) -> int:
    """Parse a JUnit attribute that must be an int-shaped value."""
    if type(raw) is int:
        return raw
    if type(raw) is str and raw.isdigit():
        return require_exact_int(int(raw), what=what)  # audit-ok[coerce_int]: coerce after digit allowlist
    raise JournalFormatError(f"{what} must be int-shaped, got {type(raw).__name__}: {raw!r}")


def _junit_count(attrib: dict[str, str], key: str) -> int:
    if key not in attrib:
        return 0
    return _as_int_attr(attrib[key], what=f"junit.@{key}")


def _tag_local(tag: str, suffix: str) -> bool:
    """True when XML tag local-name equals ``suffix`` (namespace-tolerant)."""
    if tag == suffix:
        return True
    # Clark notation / suffixed names: "{ns}testsuites" or "ns:testsuites"
    return tag.endswith("}" + suffix) or tag.endswith(":" + suffix)  # audit-ok[or_call]: XML local-name suffix match # audit-ok[str_endswith]: XML local-name suffix match  # audit-ok[str_endswith]: allowlist suffix check (not name heuristic)  # audit-ok[str_endswith]: allowlist suffix check (not name heuristic)  # noqa: E501


def _attempt_call_id(attempt_id: str | None, check_id: str, *, channel: str) -> str:
    if attempt_id is None:
        aid = _UNBOUND_ATTEMPT
    else:
        aid = attempt_id
    return f"{aid}:{channel}:{check_id}"


def raise_if_infra(
    status: SandboxStatus,
    reason: str,
    *,
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
    note: str | None = None,
) -> None:
    if status in INFRA_STATUSES:
        n = note if note is not None else soft_isolation_note(net_isolation, fs_isolation)  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
        full = f"{reason}; {n}" if n else reason  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
        raise SandboxInfraError(full, transient=False)


def store_log(
    artifacts: LocalArtifactStore,
    *,
    attempt_id: str | None,
    check_id: str,
    body: bytes,
    name: str,
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
) -> tuple[str, str, bool]:
    call = _attempt_call_id(attempt_id, check_id, channel="sandbox")
    art = artifacts.put_sandbox(
        body,
        make_tool_ref(call, tool="sandbox", attempt_id=call),
        name=name,
        media_type="text/plain",
        role="log",
        net_isolation=net_isolation,
        fs_isolation=fs_isolation,
    )
    return art.artifact_id, art.sha256, limits_fully_held(net_isolation, fs_isolation)


def soft_note(
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
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
        status=ReqStatus.MET if ok else ReqStatus.FAILED,  # audit-ok[ternary_name]: branch selects existing name/call; no invented literal  # noqa: E501
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
    except Exception as e:  # audit-ok[broad_except]: XML parse → typed ValueError refuse (never invent counts)
        raise ValueError(f"junit_parse:{e.__class__.__name__}") from e
    nodes = [root]
    if _tag_local(root.tag, "testsuites"):
        nodes = list(root)
    failures = 0
    tests = 0
    for node in nodes:
        if not _tag_local(node.tag, "testsuite") and node is not root:
            continue
        failures += _junit_count(node.attrib, "failures")
        failures += _junit_count(node.attrib, "errors")
        tests += _junit_count(node.attrib, "tests")
    if tests == 0 and _tag_local(root.tag, "testsuite"):
        failures = _junit_count(root.attrib, "failures") + _junit_count(root.attrib, "errors")
        tests = _junit_count(root.attrib, "tests")
    return failures, tests


def pointer_get(doc: Any, pointer: str | None) -> Any:
    if not pointer:
        return doc
    if not pointer.startswith("/"):  # audit-ok[str_startswith]: RFC6901 pointer must be absolute
        raise ValueError(f"invalid json pointer: {pointer}")
    cur = doc
    if pointer == "/":
        return cur
    for part in pointer[1:].split("/"):  # audit-ok[str_split]: RFC6901 path segment walk
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            cur = cur[part]
        elif isinstance(cur, list):
            if not part.isdigit():
                raise KeyError(pointer)
            cur = cur[require_exact_int(int(part), what="json_pointer.index")]  # audit-ok[coerce_int]: coerce after digit allowlist  # noqa: E501
        else:
            raise KeyError(pointer)
    return cur


def cmp(op: str, left: Any, right: Any, tolerance: float | None) -> bool:
    if op == "in":
        return left in right
    if tolerance is not None and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if op == "==":
            return abs(_as_float(left, what="cmp.left") - _as_float(right, what="cmp.right")) <= tolerance
    ops = {
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
    }
    return ops[op](left, right)


def slot_filename(slot: str) -> str:
    name = slot.replace("/", "_")
    if not name:
        raise ValueError("empty slot filename")
    return name


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
    observation_only: bool,
    timeout_kind: str | None = None,
    refute_unresolved: bool,
) -> dict[str, Any]:
    """Stamp sandbox provenance.

    ``observation_only`` / guest ``timeout_kind`` → never countable.
    ``refute_unresolved`` marks a refute-role check closed by timeout.
    """
    call = _attempt_call_id(attempt_id, check_id, channel="sandbox")
    if observation_only or timeout_kind:  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        countable_flag = False
    elif countable is None:
        countable_flag = attempt_id is not None
    else:
        if type(countable) is not bool:
            raise JournalFormatError(f"sandbox_effect.countable must be bool, got {type(countable).__name__}")
        countable_flag = countable
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
    if observation_only or timeout_kind:  # audit-ok[or_name]: coalesce existing name/attr; no invented literal
        effect["observation_only"] = True
    if timeout_kind:
        if not isinstance(timeout_kind, str):
            raise JournalFormatError(f"timeout_kind must be str, got {type(timeout_kind).__name__}")
        effect["timeout_kind"] = timeout_kind
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

    try:
        effect = check.effect
    except AttributeError:
        return False  # audit-ok[except_silent_return]: typed refuse/None on expected parse/IO class
    if not isinstance(effect, dict):
        return False
    kind = effect.get("timeout_kind")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
    if kind is None:
        kind = effect.get("reason")  # audit-ok[dict_get_bare]: absent key → None (structural; not invented domain value)  # noqa: E501
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

    if not isinstance(result, SandboxResult):
        raise TypeError(f"guest_timeout_result expects SandboxResult, got {type(result).__name__}")
    if result.status is not SandboxStatus.TIMEOUT:
        return None
    kind = classify_guest_timeout(
        kill_cause=result.timeout_kill,
        duration_s=_as_float(result.duration_s, what="SandboxResult.duration_s"),
        cpu_usage_s=result.cpu_usage_s,
        refute_role=(role == "prediction"),
    )
    note = merge_detail(
        soft_note(result.net_isolation, result.fs_isolation),
        result.note,
    )
    art_id = f"timeout:{check_id}"
    extra: dict[str, Any] = {
        "cpu_usage_s": result.cpu_usage_s,
        "wall_s": result.duration_s,
    }
    if jail_inputs_digest:
        extra["jail_inputs_digest"] = jail_inputs_digest
    if evidence is None:
        ev: list[EvidenceRef] = []  # audit-ok[field_default]: structural empty = no prior evidence
    else:
        ev = list(evidence)
    # Caller may have already stored a log; keep evidence only.
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=False,
        evidence=ev,
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
            refute_unresolved=(role == "prediction"),
            observation_only=False,
            extra=extra,
        ),
    )


def merge_numeric_extra(
    prov_fields: dict[str, Any] | None,
    *,
    est: float,
) -> dict[str, Any]:
    """Safe numeric extras from prov_fields (no binding overwrites)."""
    out: dict[str, Any] = {
        "est": _as_float(est, what="merge_numeric_extra.est"),
        "kind": "point",
    }
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


# Re-export for callers that previously relied on store_log default name.
SANDBOX_LOG_NAME = _DEFAULT_SANDBOX_LOG
