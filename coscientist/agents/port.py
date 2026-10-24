"""Journaled ``AgentPort``: LLM + tool loop; live ≡ replay from the event journal."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from coscientist.agents.budget import BudgetTracker
from coscientist.agents.config import AgentBudgetLimits, AgentConfig
from coscientist.agents.errors import (
    AgentBudgetExceeded,
    AgentConfigError,
    AgentDecisionError,
    AgentReplayError,
    AgentRoleError,
)
from coscientist.agents.journal import (
    digest_of,
    put_json_artifact,
    record_llm_call,
    record_plan_expansion_requested,
    record_tool_called,
    record_usage,
)
from coscientist.agents.types import (
    AgentDecision,
    AgentRun,
    AggregateUsage,
    ToolCallRequest,
)
from coscientist.core.events.clock import Clock
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import AgentTaskOutput, HitlPolicy, ToolCallRef
from coscientist.llm.provider import ProviderAdapter
from coscientist.llm.types import ChatMessage, LlmRequest, LlmResponse, Usage
from coscientist.tools import (
    CallCtx,
    Tool,
    ToolError,
    ToolOutput,
    filter_tools_for_context,
    invoke_tool,
)
from coscientist.tools.effects import args_digest


class JournaledAgentPort:
    """Deterministic agent runner. All LLM/tool state is journaled (DESIGN §7).

    New subtasks are never created here — only ``AgentTaskOutput.expansion_request``,
    which the core critic/validator/quotas path may accept as ``PlanExpanded``.
    """

    def __init__(
        self,
        *,
        config: AgentConfig,
        llm: ProviderAdapter,
        writer: StudyWriter,
        artifacts: LocalArtifactStore,
        clock: Clock,
        study_id: str,
        attempt_id: str,
        task_key: str,
        hitl_policy: HitlPolicy,
        monotonic: Callable[[], float],
        on_event: Callable[[Any], None] | None = None,
        llm_call_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._writer = writer
        self._artifacts = artifacts
        self._clock = clock
        self._study_id = study_id
        self._attempt_id = attempt_id
        self._task_key = task_key
        self._hitl_policy = hitl_policy
        self._on_event = on_event
        self._llm_call_lookup = llm_call_lookup
        self._monotonic = monotonic
        self._llm_seq = 0
        self._tool_seq = 0

    async def run(
        self,
        role: str,
        input: BaseModel,
        tools: list[Tool],
        budget: AgentBudgetLimits,
    ) -> AgentRun:
        try:
            role_cfg = self._config.roles[role]
        except KeyError as exc:
            raise AgentRoleError(f"unknown role: {role!r}") from exc

        tracker = BudgetTracker(budget)
        tool_refs: list[ToolCallRef] = []
        llm_ids: list[str] = []
        messages = self._initial_messages(role, role_cfg.system_prompt_ref, input)
        allowed = set(role_cfg.allowed_tool_names)
        bound_tools = [t for t in tools if t.spec.name in allowed]
        unknown_allowed = allowed - {t.spec.name for t in tools}
        if unknown_allowed:
            raise AgentConfigError(
                f"role {role!r} allowed_tool_names not present in tools: "
                f"{sorted(unknown_allowed)}"
            )

        tainted = False
        try:
            output = await self._run_turns(
                role=role,
                role_cfg_model=role_cfg.model,
                prompt_ref=role_cfg.system_prompt_ref,
                max_turns=role_cfg.max_turns,
                messages=messages,
                bound_tools=bound_tools,
                tracker=tracker,
                tool_refs=tool_refs,
                llm_ids=llm_ids,
                tainted=tainted,
            )
        except AgentBudgetExceeded as exc:
            return AgentRun(  # audit-ok[except_silent_return]: typed AgentRun status=budget_exceeded
                output=None,
                error=str(exc),
                usage=self._usage_snapshot(tracker),
                tool_calls=tuple(tool_refs),
                llm_call_ids=tuple(llm_ids),
                status="budget_exceeded",
            )
        except AgentDecisionError as exc:
            return AgentRun(  # audit-ok[except_silent_return]: typed AgentRun status=decision_invalid
                output=None,
                error=str(exc),
                usage=self._usage_snapshot(tracker),
                tool_calls=tuple(tool_refs),
                llm_call_ids=tuple(llm_ids),
                status="decision_invalid",
            )
        except AgentConfigError as exc:
            return AgentRun(  # audit-ok[except_silent_return]: typed AgentRun status=error
                output=None,
                error=str(exc),
                usage=self._usage_snapshot(tracker),
                tool_calls=tuple(tool_refs),
                llm_call_ids=tuple(llm_ids),
                status="error",
            )
        except ToolError as exc:
            return AgentRun(  # audit-ok[except_silent_return]: typed AgentRun status=error
                output=None,
                error=str(exc),
                usage=self._usage_snapshot(tracker),
                tool_calls=tuple(tool_refs),
                llm_call_ids=tuple(llm_ids),
                status="error",
            )

        return AgentRun(
            output=output,
            error=None,
            usage=self._usage_snapshot(tracker),
            tool_calls=tuple(tool_refs),
            llm_call_ids=tuple(llm_ids),
            status="ok",
        )

    async def _run_turns(
        self,
        *,
        role: str,
        role_cfg_model: str,
        prompt_ref: str,
        max_turns: int,
        messages: list[ChatMessage],
        bound_tools: list[Tool],
        tracker: BudgetTracker,
        tool_refs: list[ToolCallRef],
        llm_ids: list[str],
        tainted: bool,
    ) -> AgentTaskOutput:
        for _turn in range(max_turns):
            decision, call_id = await self._llm_turn(
                role=role,
                model=role_cfg_model,
                prompt_ref=prompt_ref,
                messages=messages,
                tracker=tracker,
            )
            llm_ids.append(call_id)
            if decision.final is not None:
                await self._maybe_journal_expansion(decision.final)
                return decision.final
            usable = filter_tools_for_context(bound_tools, tainted_context=tainted)
            usable_by_name = {t.spec.name: t for t in usable}
            for req in decision.tool_calls:
                if req.name not in usable_by_name:
                    raise AgentDecisionError(
                        f"tool {req.name!r} not allowed in current context "
                        f"(role allowlist / taint filter)"
                    )
                ref, tool_out = await self._run_tool(
                    tool=usable_by_name[req.name],
                    request=req,
                    tracker=tracker,
                    tainted_context=tainted,
                )
                tool_refs.append(ref)
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=json.dumps(
                            {
                                "tool_calls": [
                                    {"name": req.name, "arguments": req.arguments}
                                ]
                            },
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                    )
                )
                messages.append(
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "tool_result": {
                                    "name": req.name,
                                    "call_id": ref.call_id,
                                    "data": tool_out.data,
                                }
                            },
                            separators=(",", ":"),
                            ensure_ascii=False,
                            default=str,
                        ),
                    )
                )
        raise AgentDecisionError(
            f"role {role!r} exhausted max_turns={max_turns} without final"
        )

    def _initial_messages(
        self, role: str, prompt_ref: str, input_model: BaseModel
    ) -> list[ChatMessage]:
        system = self._config.prompt_text(prompt_ref)
        user_payload = {
            "role": role,
            "input": input_model.model_dump(mode="json"),
        }
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(
                role="user",
                content=json.dumps(user_payload, separators=(",", ":"), ensure_ascii=False),
            ),
        ]

    async def _llm_turn(
        self,
        *,
        role: str,
        model: str,
        prompt_ref: str,
        messages: list[ChatMessage],
        tracker: BudgetTracker,
    ) -> tuple[AgentDecision, str]:
        self._llm_seq += 1
        call_id = f"{self._attempt_id}:llm:{self._llm_seq}"
        request = LlmRequest(model=model, messages=list(messages))
        request_payload = request.model_dump(mode="json")
        request_digest = digest_of(request_payload)

        tracker.check_can_start_llm_call()
        replayed = await self._try_replay(call_id)
        if replayed is not None:
            response = replayed
        else:
            response = await self._llm.complete(
                request, timeout_s=_timeout_from_llm(self._llm)
            )
            request_art = await put_json_artifact(
                self._artifacts, request_payload, tmp_name=f"llm-req:{call_id}"
            )
            response_payload = {
                "content": response.content,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(mode="json"),
            }
            response_art = await put_json_artifact(
                self._artifacts, response_payload, tmp_name=f"llm-resp:{call_id}"
            )
            response_digest = digest_of(response_payload)
            ts = self._clock.now()
            cost_for_journal = self._config.estimate_cost_rub(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
            )
            await record_llm_call(
                self._writer,
                study_id=self._study_id,
                attempt_id=self._attempt_id,
                call_id=call_id,
                role=role,
                model=response.model,
                prompt_ref=prompt_ref,
                request_digest=request_digest,
                request_artifact_id=request_art,
                response_digest=response_digest,
                response_artifact_id=response_art,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                latency_ms=response.usage.latency_ms,
                finish_reason=response.finish_reason,
                from_replay=False,
                ts=ts,
                on_event=self._on_event,
            )
            await record_usage(
                self._writer,
                study_id=self._study_id,
                usage_id=f"{call_id}:usage",
                attempt_id=self._attempt_id,
                role=role,
                call_id=call_id,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                latency_ms=response.usage.latency_ms,
                cost_rub=cost_for_journal,
                model=response.model,
                ts=ts,
                on_event=self._on_event,
            )

        decision = self._parse_decision(response.content)
        cost = self._config.estimate_cost_rub(
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
        tracker.record_llm_call(
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            cost_rub=cost,
            latency_ms=response.usage.latency_ms,
        )
        return decision, call_id

    async def _try_replay(self, call_id: str) -> LlmResponse | None:
        """Return prior journaled response (no LLM call). Missing row → None."""
        if self._llm_call_lookup is None:
            return None
        row = self._llm_call_lookup(call_id)
        if row is None:
            return None
        art_id = row.get("response_artifact_id")
        if not isinstance(art_id, str) or art_id == "":
            raise AgentReplayError(f"LlmCallRecorded {call_id!r} missing response_artifact_id")
        try:
            raw = self._artifacts.read_cas(art_id)
        except FileNotFoundError as exc:
            raise AgentReplayError(
                f"CAS missing for LlmCallRecorded {call_id!r} artifact {art_id!r}"
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentReplayError(f"corrupt LLM response artifact {art_id!r}") from exc
        if not isinstance(payload, dict):
            raise AgentReplayError(f"LLM response artifact {art_id!r} is not an object")
        try:
            return LlmResponse(
                content=str(payload["content"]),
                model=str(payload["model"]),
                finish_reason=str(payload["finish_reason"]),
                usage=_usage_from_mapping(payload["usage"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise AgentReplayError(f"LLM response artifact {art_id!r} invalid") from exc

    def _parse_decision(self, content: str) -> AgentDecision:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AgentDecisionError("LLM content is not JSON") from exc
        try:
            return AgentDecision.model_validate(raw)
        except ValidationError as exc:
            raise AgentDecisionError(f"AgentDecision invalid: {exc}") from exc

    async def _run_tool(
        self,
        *,
        tool: Tool,
        request: ToolCallRequest,
        tracker: BudgetTracker,
        tainted_context: bool,
    ) -> tuple[ToolCallRef, ToolOutput]:
        tracker.record_tool_call()
        self._tool_seq += 1
        call_id = f"{self._attempt_id}:tool:{self._tool_seq}"
        digest = args_digest(request.arguments)
        started = self._clock.now()
        t0 = self._monotonic()
        ctx = CallCtx(
            study_id=self._study_id,
            task_key=self._task_key,
            attempt_id=self._attempt_id,
            hitl_policy=self._hitl_policy,
            tainted_context=tainted_context,
            call_seq=self._tool_seq,
        )
        try:
            output = await invoke_tool(
                tool,
                request.arguments,
                ctx,
                writer=self._writer,
                artifacts=self._artifacts,
                on_event=self._on_event,
            )
            result_payload = {
                "data": output.data,
                "artifact_ids": [a.artifact_id for a in output.artifacts],
            }
            result_art = await put_json_artifact(
                self._artifacts, result_payload, tmp_name=f"tool-out:{call_id}"
            )
            result_digest = digest_of(result_payload)
            latency_ms = (self._monotonic() - t0) * 1000.0
            ended = self._clock.now()
            await record_tool_called(
                self._writer,
                study_id=self._study_id,
                call_id=call_id,
                attempt_id=self._attempt_id,
                tool=tool.spec.name,
                provider=tool.spec.provider,
                args_digest=digest,
                result_digest=result_digest,
                result_artifact_id=result_art,
                latency_ms=latency_ms,
                ok=True,
                error_kind=None,
                error_detail=None,
                ts=ended,
                on_event=self._on_event,
            )
            ref = ToolCallRef(
                call_id=call_id,
                tool=tool.spec.name,
                args_digest=digest,
                started_at=started,
                ended_at=ended,
            )
            return ref, output
        except ToolError as exc:
            latency_ms = (self._monotonic() - t0) * 1000.0
            ended = self._clock.now()
            await record_tool_called(
                self._writer,
                study_id=self._study_id,
                call_id=call_id,
                attempt_id=self._attempt_id,
                tool=tool.spec.name,
                provider=tool.spec.provider,
                args_digest=digest,
                result_digest=None,
                result_artifact_id=None,
                latency_ms=latency_ms,
                ok=False,
                error_kind=exc.kind,
                error_detail=exc.detail,
                ts=ended,
                on_event=self._on_event,
            )
            raise

    async def _maybe_journal_expansion(self, out: AgentTaskOutput) -> None:
        if out.expansion_request is None:
            return
        payload = out.expansion_request.model_dump(mode="json")
        art = await put_json_artifact(
            self._artifacts,
            payload,
            tmp_name=f"expand:{self._attempt_id}",
        )
        await record_plan_expansion_requested(
            self._writer,
            study_id=self._study_id,
            request_id=f"{self._attempt_id}:expansion",
            attempt_id=self._attempt_id,
            expansion_digest=digest_of(payload),
            expansion_artifact_id=art,
            ts=self._clock.now(),
            on_event=self._on_event,
        )

    def _usage_snapshot(self, tracker: BudgetTracker) -> AggregateUsage:
        cost: Decimal | None
        if (
            self._config.cost_per_prompt_token_rub is None
            and self._config.cost_per_completion_token_rub is None
        ):
            cost = None
        else:
            cost = tracker.cost_rub
        return AggregateUsage(
            prompt_tokens=tracker.prompt_tokens,
            completion_tokens=tracker.completion_tokens,
            latency_ms=tracker.latency_ms,
            cost_rub=cost,
            llm_calls=tracker.llm_calls,
            tool_calls=tracker.tool_calls,
        )


def _timeout_from_llm(llm: ProviderAdapter) -> float:
    """Require an explicit timeout on the adapter or its LlmConfig (no guessed default)."""
    configured = getattr(llm, "complete_timeout_s", None)
    if configured is not None:
        value = float(configured)
        if value <= 0.0:
            raise AgentConfigError("complete_timeout_s must be > 0")
        return value
    cfg = getattr(llm, "_config", None)
    if cfg is not None and hasattr(cfg, "timeout_s"):
        value = float(cfg.timeout_s)
        if value <= 0.0:
            raise AgentConfigError("LlmConfig.timeout_s must be > 0")
        return value
    raise AgentConfigError(
        "LLM adapter must expose complete_timeout_s or LlmConfig.timeout_s"
    )


def _usage_from_mapping(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        raise ValueError("usage must be an object")
    return Usage.model_validate(raw)


def llm_calls_from_events(events: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Build call_id → LlmCallRecorded payload map (for replay lookup)."""
    out: dict[str, dict[str, Any]] = {}
    for ev in events:
        if getattr(ev, "type", None) != "LlmCallRecorded":
            continue
        payload_raw = getattr(ev, "payload", None)
        if not isinstance(payload_raw, dict):
            continue
        payload = dict(payload_raw)
        call_id_raw = payload.get("call_id")
        if isinstance(call_id_raw, str) and call_id_raw != "":
            call_id = call_id_raw
        else:
            entity = getattr(ev, "entity_key", None)
            if not isinstance(entity, str) or entity == "":
                continue
            call_id = entity
        out[call_id] = payload
    return out
