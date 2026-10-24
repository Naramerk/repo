"""Shared helpers for executable CodeCheck kinds."""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as ET

from coscientist.checks.catalog import EFFECT_EST_KIND_POINT
from coscientist.checks.sandbox import (
    INFRA_STATUSES,
    TIMEOUT_KINDS,
    FsIsolation,
    NetIsolation,
    SandboxResult,
    SandboxStatus,
    limits_fully_held,
    soft_isolation_note,
)
from coscientist.core.events.errors import CatalogError, SandboxInfraError
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import CheckResult, CheckRole, EvidenceRef, ReqStatus

MALFORMED_JUNIT = "malformed_output:junit"
MALFORMED_METRICS = "malformed_output:metrics"
MALFORMED_REWARD = "malformed_output:reward"

# Binding keys that extras must never overwrite (single source).
_EFFECT_BLOCKED_KEYS: frozenset[str] = frozenset(
    {
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
)
# merge_numeric_extra also blocks overwriting its own est/kind seeds.
_PROV_BLOCKED_KEYS: frozenset[str] = _EFFECT_BLOCKED_KEYS | frozenset({"est", "kind"})


def raise_if_infra(
    status: SandboxStatus,
    reason: str,
    *,
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
    note: str | None,
) -> None:
    """Raise ``SandboxInfraError`` for infra statuses.

    ``note`` is the result's own note (already carries the soft-isolation
    note); it is required so no second note is inferred here.
    """
    if status in INFRA_STATUSES:
        full = f"{reason}; {note}" if note else reason
        raise SandboxInfraError(full)


def store_log(
    artifacts: LocalArtifactStore,
    *,
    attempt_id: str,
    check_id: str,
    body: bytes,
    cgroup_limits_held: bool,
    name: str,
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
) -> tuple[str, str, bool]:
    if not attempt_id:
        raise CatalogError("attempt_id_required_for_effect")
    if not name:
        raise CatalogError("store_log_name_required")
    call = f"{attempt_id}:sandbox:{check_id}"
    art = artifacts.put_sandbox(
        body,
        make_tool_ref(call, tool="sandbox", attempt_id=attempt_id),
        name=name,
        media_type="text/plain",
        role="log",
        net_isolation=net_isolation,
        fs_isolation=fs_isolation,
    )
    return (
        art.artifact_id,
        art.sha256,
        limits_fully_held(
            net_isolation, fs_isolation, cgroup_limits_held=cgroup_limits_held
        ),
    )


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
    required: bool,
    effect: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        template_id=template_id,
        status=ReqStatus.MET if ok else ReqStatus.FAILED,
        is_code=True,
        required=required,
        role=role,
        evidence=evidence,
        detail=detail,
        effect=effect,
        used_sesoi=False,
    )


def _junit_attr_int(attrib: dict[str, str], key: str) -> int:
    """Parse a junit XML attribute as int; missing / empty / invalid → raise."""
    if key not in attrib:
        raise ValueError(f"junit_missing_attr:{key}")
    return int(attrib[key])


def _xml_localname(tag: str) -> str:
    """Clark ``{ns}local`` → local; unnamespaced tag returned as-is."""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def parse_junit(raw: bytes) -> tuple[int, int]:
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as e:
        raise ValueError(f"junit_parse:{e.__class__.__name__}") from e
    root_local = _xml_localname(root.tag)
    if root_local == "testsuites":
        nodes = list(root)
    elif root_local == "testsuite":
        nodes = [root]
    else:
        raise ValueError(f"junit_unexpected_root:{root_local}")
    failures = 0
    tests = 0
    for node in nodes:
        # Every child of <testsuites> must be a <testsuite> — no silent skips.
        local = _xml_localname(node.tag)
        if local != "testsuite":
            raise ValueError(f"junit_unexpected_node:{local}")
        # failures, errors, and tests are all required on each suite node.
        failures += _junit_attr_int(node.attrib, "failures")
        failures += _junit_attr_int(node.attrib, "errors")
        tests += _junit_attr_int(node.attrib, "tests")
    return failures, tests


def pointer_get(doc: Any, pointer: str | None) -> Any:
    if pointer is None:
        return doc
    if pointer == "" or not isinstance(pointer, str):
        raise ValueError("invalid json pointer: empty")
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
    name = slot.replace("/", "_")
    if not name:
        raise CatalogError("empty_slot_filename")
    if name in {".", ".."}:
        raise CatalogError(f"bad_slot_filename:{name}")
    return name


def sandbox_effect(
    *,
    attempt_id: str,
    check_id: str,
    art_id: str,
    trusted: bool,
    net_isolation: NetIsolation,
    fs_isolation: FsIsolation,
    note: str | None,
    countable: bool,
    extra: dict[str, Any] | None = None,
    observation_only: bool = False,
    timeout_kind: str | None = None,
    refute_unresolved: bool = False,
) -> dict[str, Any]:
    """Stamp sandbox provenance.

    ``observation_only`` / guest ``timeout_kind`` → never countable.
    ``refute_unresolved`` marks a refute-role check closed by timeout.
    """
    if not attempt_id:
        raise CatalogError("attempt_id_required_for_effect")
    if not isinstance(countable, bool):
        raise CatalogError(
            f"sandbox_effect_countable_must_be_bool:{type(countable).__name__}"
        )
    if timeout_kind is not None and timeout_kind not in TIMEOUT_KINDS:
        raise CatalogError(f"sandbox_effect_bad_timeout_kind:{timeout_kind!r}")
    call = f"{attempt_id}:sandbox:{check_id}"
    if observation_only or timeout_kind is not None:
        countable_flag = False
    else:
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
        "attempt_id": str(attempt_id),
    }
    if observation_only or timeout_kind is not None:
        effect["observation_only"] = True
    if timeout_kind is not None:
        effect["timeout_kind"] = timeout_kind
    if refute_unresolved:
        effect["refute_unresolved"] = True
    if note is not None:
        if note == "":
            raise CatalogError("sandbox_effect_empty_note")
        effect["note"] = note
    if extra is not None:
        for k, v in extra.items():
            if k in _EFFECT_BLOCKED_KEYS:
                raise CatalogError(f"sandbox_effect_blocked_extra:{k}")
            if v is None or v == "":
                raise CatalogError(f"sandbox_effect_empty_extra:{k}")
            effect[k] = v
    return effect


def check_is_guest_timeout(check: CheckResult) -> bool:
    """True when CheckResult.effect carries a host-classified guest timeout.

    Missing / non-dict effect → False (non-sandbox checks). Present but
    unknown ``timeout_kind`` → CatalogError (fail-closed).
    """
    from coscientist.checks.sandbox import TIMEOUT_KINDS

    effect = check.effect
    if not isinstance(effect, dict):
        return False
    if "timeout_kind" not in effect:
        return False
    kind = effect["timeout_kind"]
    if kind not in TIMEOUT_KINDS:
        raise CatalogError(f"check_effect_bad_timeout_kind:{kind!r}")
    return True


def guest_timeout_result(
    *,
    check_id: str,
    template_id: str,
    role: CheckRole,
    attempt_id: str | None,
    result: SandboxResult,
    evidence: list[EvidenceRef],
    jail_inputs_digest: str | None,
) -> CheckResult:
    """Non-countable ``CheckResult`` for a guest timeout.

    Callers branch on ``result.status is SandboxStatus.TIMEOUT`` first; any
    other status is a caller bug → ``SandboxInfraError``.
    """
    from coscientist.checks.sandbox import TIMEOUT_KINDS, classify_guest_timeout

    if result.status is not SandboxStatus.TIMEOUT:
        raise SandboxInfraError("sandbox_unavailable:non_timeout_status")
    if attempt_id is None:
        raise CatalogError("attempt_id_required_for_effect")
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
    note = merge_detail(
        soft_note(result.net_isolation, result.fs_isolation),
        result.note,
    )
    extra: dict[str, Any] = {
        "wall_s": result.duration_s,
    }
    if result.cpu_usage_s is not None:
        extra["cpu_usage_s"] = result.cpu_usage_s
    if jail_inputs_digest is not None:
        extra["jail_inputs_digest"] = jail_inputs_digest
    # Caller may have already stored a log; keep evidence only.
    return base_result(
        check_id=check_id,
        template_id=template_id,
        role=role,
        ok=False,
        required=True,
        evidence=list(evidence),
        detail=merge_detail(kind, note),
        effect=sandbox_effect(
            attempt_id=attempt_id,
            check_id=check_id,
            art_id=f"timeout:{check_id}",
            trusted=False,
            net_isolation=result.net_isolation,
            fs_isolation=result.fs_isolation,
            note=note,
            countable=False,
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
    out: dict[str, Any] = {"est": float(est), "kind": EFFECT_EST_KIND_POINT}
    if prov_fields is None:
        return out
    for k, v in prov_fields.items():
        if k in _PROV_BLOCKED_KEYS:
            raise CatalogError(f"prov_field_blocked:{k}")
        if v is None or v == "":
            raise CatalogError(f"prov_field_empty:{k}")
        out[k] = v
    return out
