"""LlmAgentAdapter: ``AgentPort`` over ``LlmClient`` + ``ToolRouter`` (DESIGN §7.2 / §13 D)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from coscientist.agents.prompts.registry import PromptRef, PromptRegistry
from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import (
    AgentError,
    AgentRun,
    ExecutorRouteIn,
    ExecutorRouteOut,
    PlanIntent,
    PlannedToolCall,
    StudyContext,
    ToolArg,
    ToolCallRef,
    plan_intent_model_for,
)
from coscientist.core.events.clock import Clock
from coscientist.core.execution.call_ctx import CallCtx
from coscientist.core.execution.domain import Budget, HitlPolicy
from coscientist.llm.client import LlmClient
from coscientist.llm.types import ChatMessage, JsonSchemaResponseFormat, StructuredLlmRequest, Usage
from coscientist.tools.router import ToolNeed, ToolRouter, ToolRouterError

_T = TypeVar("_T", bound=BaseModel)


class _ToolSpecName(Protocol):
    @property
    def name(self) -> str: ...


class NamedTool(Protocol):
    """Allow-list entry for ``run`` (DESIGN §7.2 ``tools: list[Tool]``)."""

    @property
    def spec(self) -> _ToolSpecName: ...


_ROLE_PLANNER = "planner"
_ROLE_EXECUTOR = "executor-route"
_ROLE_TZ_SPEC = "tz_spec"
_REQUIRED_ROLES = (_ROLE_PLANNER, _ROLE_EXECUTOR, _ROLE_TZ_SPEC)

_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    _ROLE_PLANNER: PlanIntent,
    _ROLE_EXECUTOR: ExecutorRouteOut,
    _ROLE_TZ_SPEC: ResearchSpec,
}
_CONTEXT_ROLES = frozenset({_ROLE_PLANNER, _ROLE_TZ_SPEC})


def _json_schema_response_format(model: type[BaseModel], *, name: str) -> JsonSchemaResponseFormat:
    """OpenAI-compatible json_schema response_format (strict)."""
    return JsonSchemaResponseFormat(
        name=name,
        strict=True,
        schema=model.model_json_schema(),
    )


def _tool_names(tools: Sequence[NamedTool]) -> frozenset[str]:
    """Allow-list names from DESIGN §7.2 ``tools: list[Tool]`` via ``NamedTool.spec``."""
    names: set[str] = set()
    for tool in tools:
        name = tool.spec.name
        if type(name) is not str or not name.strip():
            raise AgentError("tools entries must expose spec.name as a non-empty str")
        names.add(name)
    return frozenset(names)


def _args_mapping(args: tuple[ToolArg, ...]) -> dict[str, object]:
    out: dict[str, object] = {}
    for arg in args:
        try:
            parsed: object = json.loads(arg.value_json)
        except json.JSONDecodeError as exc:
            raise AgentError(f"tool arg {arg.key!r} value_json is not valid JSON: {exc}") from exc
        out[arg.key] = parsed
    return out


def _assert_budget_allows_llm(
    budget: Budget,
    *,
    llm_calls_used: int,
    tokens_used: int,
    now: datetime,
) -> None:
    if llm_calls_used >= budget.max_llm_calls:
        raise AgentError("budget.max_llm_calls exceeded")
    remaining_tokens = budget.max_tokens - tokens_used
    if remaining_tokens <= 0:
        raise AgentError("budget.max_tokens exceeded")
    if budget.deadline is None:
        return
    if budget.deadline.tzinfo is None:
        raise AgentError("budget.deadline must be timezone-aware when set")
    if now >= budget.deadline:
        raise AgentError("budget.deadline exceeded")


def _assert_budget_cost_unmetered(budget: Budget) -> None:
    if budget.max_cost_rub is not None:
        raise AgentError("budget.max_cost_rub is not metered by LlmAgentAdapter; set max_cost_rub=None")


class LlmAgentAdapter:
    """Implements ``core.events.ports.AgentPort`` without editing ports.py.

    ``role_models`` maps role → provider model id (from profile; required, no fallback).
    Tool calls use ``tools.router.ToolRouter`` (MCP #139): ``resolve`` then
    ``call(server_name, tool_name, args, ctx)``.
    System prompts come from ``PromptRegistry`` via ``role_prompts`` (CAP #27).
    ``clock`` supplies wall time for ``Budget.deadline`` enforcement (DESIGN §7.4).
    ``hitl_policy`` is the study HitlPolicy (required; never invented).
    """

    @staticmethod
    def supports_role(role: str) -> bool:
        """True when ``run`` has an output schema for ``role`` (CLI optional-role seam)."""
        return role in _OUTPUT_MODELS

    def __init__(
        self,
        client: LlmClient,
        *,
        role_models: Mapping[str, str],
        tool_router: ToolRouter,
        prompts: PromptRegistry,
        role_prompts: Mapping[str, PromptRef],
        clock: Clock,
        hitl_policy: HitlPolicy,
    ) -> None:
        missing_models = [r for r in _REQUIRED_ROLES if r not in role_models]
        if missing_models:
            raise AgentError(
                f"role_models must include {list(_REQUIRED_ROLES)} (no defaults); missing {missing_models}"
            )
        missing_prompts = [r for r in _REQUIRED_ROLES if r not in role_prompts]
        if missing_prompts:
            raise AgentError(
                f"role_prompts must include {list(_REQUIRED_ROLES)} (no defaults); missing {missing_prompts}"
            )
        for role, model in role_models.items():
            if type(model) is not str or not model.strip():
                raise AgentError(f"role_models[{role!r}] must be a non-empty str")
        for role, ref in role_prompts.items():
            if not isinstance(ref, PromptRef):
                raise AgentError(f"role_prompts[{role!r}] must be PromptRef")
            prompts.get(ref)
        if not isinstance(hitl_policy, HitlPolicy):
            raise AgentError(f"hitl_policy must be HitlPolicy, got {type(hitl_policy).__name__}")
        self._client = client
        self._role_models = dict(role_models)
        self._router = tool_router
        self._prompts = prompts
        self._role_prompts = dict(role_prompts)
        self._clock = clock
        self._hitl_policy = hitl_policy
        # Accumulated across ``run`` calls on this adapter (study / session budget).
        self._llm_calls_used = 0
        self._tokens_used = 0

    async def run(
        self,
        role: str,
        input: BaseModel | Mapping[str, object],
        tools: list[NamedTool],
        budget: Budget,
    ) -> AgentRun:
        """DESIGN §7.2: ``input: BaseModel``, ``tools: list[Tool]``, ``budget: Budget``."""
        if not isinstance(budget, Budget):
            raise AgentError(f"budget must be Budget, got {type(budget).__name__}")
        _assert_budget_cost_unmetered(budget)
        if not isinstance(tools, list):
            raise AgentError("tools must be a list")
        allowed = _tool_names(tools)
        if role not in _OUTPUT_MODELS:
            raise AgentError(f"unsupported role {role!r}")
        # Budget limits must be positive; usage accumulates on the adapter instance.
        if budget.max_llm_calls < 1:
            raise AgentError("budget.max_llm_calls must be >= 1 for a role run")
        if budget.max_tokens < 1:
            raise AgentError("budget.max_tokens must be >= 1 for a role run")
        model_id = self._role_models[role]
        out_cls = _OUTPUT_MODELS[role]
        tool_refs: list[ToolCallRef] = []

        if role in _CONTEXT_ROLES:
            ctx = _parse_input(StudyContext, input)
            if role == _ROLE_PLANNER:
                # Context-typed request schema: tools_hint becomes an enum of the
                # study's tool ids (schema-checked — no prompt rule needed).
                out_cls = plan_intent_model_for(tuple(ctx.tool_ids))
            user_payload = ctx.model_dump()
        else:
            route_in = _parse_input(ExecutorRouteIn, input)
            tool_refs = await self._run_tools(route_in, allowed=allowed, budget=budget)
            user_payload = {
                "task": route_in.task.model_dump(),
                "study_id": route_in.study_id,
                "attempt_id": route_in.attempt_id,
                "inputs": [inp.model_dump() for inp in route_in.inputs],
                "tool_results": [r.model_dump() for r in tool_refs],
            }

        _assert_budget_allows_llm(
            budget,
            llm_calls_used=self._llm_calls_used,
            tokens_used=self._tokens_used,
            now=self._clock.now(),
        )
        system = self._prompts.render(self._role_prompts[role], {"role": role})
        response_format = _json_schema_response_format(out_cls, name=role.replace("-", "_"))
        # One bounded repair round: live providers occasionally emit schema-
        # invalid JSON (e.g. unquoted keys). The validation error is fed back
        # once; acceptance stays model_validate_json — no gate is weakened.
        messages: list[ChatMessage] = [
            ChatMessage(
                role="system",
                content=(
                    system
                    + "\nExact JSON Schema of the required response:\n"
                    + json.dumps(
                        response_format.schema_,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
            ),
        ]
        parsed, usage = await self._complete_validated(
            model_id=model_id,
            messages=messages,
            response_format=response_format,
            out_cls=out_cls,
            role=role,
            budget=budget,
        )
        return AgentRun(output=parsed, error=None, usage=usage, tool_calls=tuple(tool_refs))

    async def _complete_validated(
        self,
        *,
        model_id: str,
        messages: list[ChatMessage],
        response_format: JsonSchemaResponseFormat,
        out_cls: type[BaseModel],
        role: str,
        budget: Budget,
    ) -> tuple[BaseModel, Usage]:
        """Complete → validate; on schema failure feed the error back once."""
        schema_error = ""
        last_content = ""
        for attempt in (0, 1):
            if attempt:
                if not self._budget_allows_repair(budget):
                    break
                messages = [
                    *messages,
                    ChatMessage(role="assistant", content=last_content),
                    ChatMessage(
                        role="user",
                        content=(
                            "Your previous reply failed schema validation:\n"
                            + schema_error
                            + "\nReply again with the complete corrected JSON only."
                        ),
                    ),
                ]
            response = await self._client.complete(
                StructuredLlmRequest(
                    model=model_id, messages=messages, response_format=response_format
                )
            )
            last_content = response.content
            self._llm_calls_used += 1
            usage = response.usage
            self._tokens_used += usage.prompt_tokens + usage.completion_tokens
            if self._tokens_used > budget.max_tokens:
                raise AgentError("budget.max_tokens exceeded")
            try:
                return out_cls.model_validate_json(response.content), usage
            except ValidationError as exc:
                schema_error = str(exc)
        raise AgentError(
            f"{role} output failed schema validation (after repair retry): {schema_error}"
        )

    def _budget_allows_repair(self, budget: Budget) -> bool:
        # Same conditions as _assert_budget_allows_llm, as a predicate.
        if self._llm_calls_used >= budget.max_llm_calls:
            return False
        if budget.max_tokens - self._tokens_used <= 0:
            return False
        deadline = budget.deadline
        if deadline is not None:
            if deadline.tzinfo is None:
                raise AgentError("budget.deadline must be timezone-aware when set")
            if self._clock.now() >= deadline:
                return False
        return True

    async def _run_tools(
        self,
        route_in: ExecutorRouteIn,
        *,
        allowed: frozenset[str],
        budget: Budget,
    ) -> list[ToolCallRef]:
        hints = route_in.task.tools_hint
        if hints and not allowed:
            raise AgentError("executor-route tools list is empty but task.tools_hint is not")
        if len(hints) > budget.max_tool_calls:
            raise AgentError("budget.max_tool_calls exceeded")
        args_by_name = {c.tool_name: c for c in route_in.tool_args}
        refs: list[ToolCallRef] = []
        for hint in hints:
            if hint not in allowed:
                raise AgentError(f"tool {hint!r} not in AgentPort.tools allow-list")
            planned: PlannedToolCall = args_by_name[hint]
            try:
                resolution = self._router.resolve(ToolNeed(kind="by_name", value=hint))
            except ToolRouterError as exc:
                raise AgentError(f"tool resolve for {hint!r} refused: {exc}") from exc
            if resolution.status != "tool_found" or resolution.server_name is None or resolution.tool_name is None:
                raise AgentError(
                    f"tool resolve for {hint!r} refused: status={resolution.status!r} "
                    "(fail-closed; only tool_found may proceed)"
                )
            ctx = CallCtx(
                study_id=route_in.study_id,
                task_key=route_in.task.task_key,
                attempt_id=route_in.attempt_id,
                hitl_policy=self._hitl_policy,
            )
            tool_out = await self._router.call(
                resolution.server_name,
                resolution.tool_name,
                _args_mapping(planned.args),
                ctx,
            )
            try:
                result_json = json.dumps(
                    tool_out.data,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise AgentError(f"tool {hint!r} result is not JSON-serializable: {exc}") from exc
            bound_name = f"{resolution.server_name}:{resolution.tool_name}"
            refs.append(
                ToolCallRef(
                    tool_name=bound_name,
                    resolution_status=resolution.status,
                    args=planned.args,
                    result_json=result_json,
                )
            )
        return refs


def _parse_input(model: type[_T], raw: BaseModel | Mapping[str, object]) -> _T:
    if isinstance(raw, model):
        return raw
    if isinstance(raw, BaseModel):
        raise AgentError(f"invalid {model.__name__}: expected {model.__name__} or mapping, got {type(raw).__name__}")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise AgentError(f"invalid {model.__name__}: {exc}") from exc
