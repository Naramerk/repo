"""Append typed agent-layer journal events (bodies live in CAS; events carry digests)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.schema_versions import (
    LLM_CALL_RECORDED_SCHEMA_VERSION,
    MCP_REQUEST_RECORDED_SCHEMA_VERSION,
    MCP_RESPONSE_RECORDED_SCHEMA_VERSION,
    PLAN_EXPANSION_REQUESTED_SCHEMA_VERSION,
    TOOL_CALLED_SCHEMA_VERSION,
    USAGE_RECORDED_SCHEMA_VERSION,
)
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore


async def put_json_artifact(
    artifacts: LocalArtifactStore,
    payload: dict[str, Any] | list[Any],
    *,
    tmp_name: str,
) -> str:
    """Store JCS bytes in CAS; return sha256 digest (artifact id)."""
    raw = canonical_json(payload)
    return artifacts.put_cas(raw, tmp_name=tmp_name)


async def record_llm_call(
    writer: StudyWriter,
    *,
    study_id: str,
    attempt_id: str,
    call_id: str,
    role: str,
    model: str,
    prompt_ref: str,
    request_digest: str,
    request_artifact_id: str,
    response_digest: str,
    response_artifact_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    finish_reason: str,
    from_replay: bool,
    ts: datetime,
    on_event: Any | None = None,
) -> EventEnvelope:
    payload = {
        "call_id": call_id,
        "attempt_id": attempt_id,
        "role": role,
        "model": model,
        "prompt_ref": prompt_ref,
        "request_digest": request_digest,
        "request_artifact_id": request_artifact_id,
        "response_digest": response_digest,
        "response_artifact_id": response_artifact_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "finish_reason": finish_reason,
        "from_replay": from_replay,
    }
    ev = EventEnvelope(
        study_id=study_id,
        type="LlmCallRecorded",
        entity_key=call_id,
        idem_key=make_idem("LlmCallRecorded", call_id),
        payload=payload,
        ts=ts,
        schema_version=LLM_CALL_RECORDED_SCHEMA_VERSION,
    )
    out = await writer.append(ev)
    if on_event is not None:
        on_event(out)
    return out


async def record_usage(
    writer: StudyWriter,
    *,
    study_id: str,
    usage_id: str,
    attempt_id: str,
    role: str,
    call_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    cost_rub: Decimal | None,
    model: str,
    ts: datetime,
    on_event: Any | None = None,
) -> EventEnvelope:
    payload = {
        "usage_id": usage_id,
        "attempt_id": attempt_id,
        "role": role,
        "call_id": call_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "cost_rub": str(cost_rub) if cost_rub is not None else None,
        "model": model,
    }
    ev = EventEnvelope(
        study_id=study_id,
        type="UsageRecorded",
        entity_key=usage_id,
        idem_key=make_idem("UsageRecorded", usage_id),
        payload=payload,
        ts=ts,
        schema_version=USAGE_RECORDED_SCHEMA_VERSION,
    )
    out = await writer.append(ev)
    if on_event is not None:
        on_event(out)
    return out


async def record_tool_called(
    writer: StudyWriter,
    *,
    study_id: str,
    call_id: str,
    attempt_id: str,
    tool: str,
    provider: str,
    args_digest: str,
    result_digest: str | None,
    result_artifact_id: str | None,
    latency_ms: float,
    ok: bool,
    error_kind: str | None,
    error_detail: str | None,
    ts: datetime,
    on_event: Any | None = None,
) -> EventEnvelope:
    payload = {
        "call_id": call_id,
        "attempt_id": attempt_id,
        "tool": tool,
        "provider": provider,
        "args_digest": args_digest,
        "result_digest": result_digest,
        "result_artifact_id": result_artifact_id,
        "latency_ms": latency_ms,
        "ok": ok,
        "error_kind": error_kind,
        "error_detail": error_detail,
    }
    ev = EventEnvelope(
        study_id=study_id,
        type="ToolCalled",
        entity_key=call_id,
        idem_key=make_idem("ToolCalled", call_id),
        payload=payload,
        ts=ts,
        schema_version=TOOL_CALLED_SCHEMA_VERSION,
    )
    out = await writer.append(ev)
    if on_event is not None:
        on_event(out)
    return out


async def record_mcp_request(
    writer: StudyWriter,
    *,
    study_id: str,
    request_id: str,
    attempt_id: str,
    server_name: str,
    method: str,
    tool_name: str | None,
    args_digest: str,
    ts: datetime,
    on_event: Any | None = None,
) -> EventEnvelope:
    payload = {
        "request_id": request_id,
        "attempt_id": attempt_id,
        "server_name": server_name,
        "method": method,
        "tool_name": tool_name,
        "args_digest": args_digest,
    }
    ev = EventEnvelope(
        study_id=study_id,
        type="McpRequestRecorded",
        entity_key=request_id,
        idem_key=make_idem("McpRequestRecorded", request_id),
        payload=payload,
        ts=ts,
        schema_version=MCP_REQUEST_RECORDED_SCHEMA_VERSION,
    )
    out = await writer.append(ev)
    if on_event is not None:
        on_event(out)
    return out


async def record_mcp_response(
    writer: StudyWriter,
    *,
    study_id: str,
    request_id: str,
    attempt_id: str,
    server_name: str,
    method: str,
    content_digest: str,
    result_artifact_id: str,
    is_error: bool,
    latency_ms: float,
    ts: datetime,
    on_event: Any | None = None,
) -> EventEnvelope:
    payload = {
        "request_id": request_id,
        "attempt_id": attempt_id,
        "server_name": server_name,
        "method": method,
        "content_digest": content_digest,
        "result_artifact_id": result_artifact_id,
        "is_error": is_error,
        "latency_ms": latency_ms,
    }
    ev = EventEnvelope(
        study_id=study_id,
        type="McpResponseRecorded",
        entity_key=request_id,
        idem_key=make_idem("McpResponseRecorded", request_id),
        payload=payload,
        ts=ts,
        schema_version=MCP_RESPONSE_RECORDED_SCHEMA_VERSION,
    )
    out = await writer.append(ev)
    if on_event is not None:
        on_event(out)
    return out


async def record_plan_expansion_requested(
    writer: StudyWriter,
    *,
    study_id: str,
    request_id: str,
    attempt_id: str,
    expansion_digest: str,
    expansion_artifact_id: str,
    ts: datetime,
    on_event: Any | None = None,
) -> EventEnvelope:
    """Journal the agent expansion_request; core still applies critic/quotas."""
    payload = {
        "request_id": request_id,
        "attempt_id": attempt_id,
        "expansion_digest": expansion_digest,
        "expansion_artifact_id": expansion_artifact_id,
    }
    ev = EventEnvelope(
        study_id=study_id,
        type="PlanExpansionRequested",
        entity_key=request_id,
        idem_key=make_idem("PlanExpansionRequested", request_id),
        payload=payload,
        ts=ts,
        schema_version=PLAN_EXPANSION_REQUESTED_SCHEMA_VERSION,
    )
    out = await writer.append(ev)
    if on_event is not None:
        on_event(out)
    return out


def digest_of(payload: dict[str, Any] | list[Any]) -> str:
    return sha256_hex(canonical_json(payload))
