"""LlmAgentAdapter: ``AgentPort`` over ``LlmClient`` + ``ToolRouter`` (DESIGN §7.2 / §13 D)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from coscientist.agents.critic import CriticVerdict
from coscientist.agents.prompts.registry import PromptRef, PromptRegistry
from coscientist.agents.research_spec import ResearchSpec
from coscientist.agents.schemas import (
    AgentError,
    AgentRun,
    AutomaticExecutorIn,
    CriticIn,
    ExecutorRouteIn,
    ExecutorRouteOut,
    HypothesisOut,
    LiteratureOut,
    PlanIntent,
    PlannedToolCall,
    ReviewerIn,
    ReviewerOut,
    StudyContext,
    ToolAction,
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


class RuntimeToolExecutor(Protocol):
    """CLI-owned invocation boundary; agents never acquire registry authority."""

    async def check_active(self, study_id: str, attempt_id: str) -> None: ...

    async def invoke(self, name: str, args: dict[str, object], ctx: CallCtx) -> ToolCallRef: ...


_ROLE_PLANNER = "planner"
_ROLE_EXECUTOR = "executor-route"
_ROLE_TZ_SPEC = "tz_spec"
_ROLE_CRITIC = "critic"
_ROLE_REVIEWER = "reviewer"
_ROLE_HYPOTHEIZER = "hypothesizer"
_ROLE_RESEARCHER = "researcher"
_REQUIRED_ROLES = (_ROLE_PLANNER, _ROLE_EXECUTOR, _ROLE_TZ_SPEC)

_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    _ROLE_PLANNER: PlanIntent,
    _ROLE_EXECUTOR: ExecutorRouteOut,
    _ROLE_TZ_SPEC: ResearchSpec,
    # Wired plan critic (DESIGN §7.1): the derived PlanOut → CriticVerdict.
    _ROLE_CRITIC: CriticVerdict,
    # Wired post-close reviewer lane: graph digest → ReviewerOut (advisory;
    # the lane never writes journal events, so it cannot flip a verdict).
    _ROLE_REVIEWER: ReviewerOut,
    # Wired optional roles (CLI --wired-roles; Need-gated; see cli/run.py):
    _ROLE_HYPOTHEIZER: HypothesisOut,
    _ROLE_RESEARCHER: LiteratureOut,
}
_CONTEXT_ROLES = frozenset({_ROLE_PLANNER, _ROLE_TZ_SPEC, _ROLE_HYPOTHEIZER})


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
        if arg.key in out:
            raise AgentError("duplicate tool argument keys")
        try:
            parsed: object = _strict_json(arg.value_json)
        except ValueError as exc:
            raise AgentError(f"tool arg {arg.key!r} value_json is not valid JSON: {exc}") from exc
        out[arg.key] = parsed
    return out


def _strict_json(raw: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> object:
        raise ValueError(f"nonfinite JSON value: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


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

    def can_run_role(self, role: str) -> bool:
        """True when THIS adapter can ``run`` ``role`` (model + prompt both registered).

        The CLI optional-role dependency seam: ``supports_role`` says the adapter
        class has the output schema; ``can_run_role`` says this instance was
        built with the role's model and prompt ref — both required before a
        wired stage may call ``run`` (no mid-study KeyError on a drifted build).
        """
        return role in self._role_models and role in self._role_prompts

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
        self._tool_calls_used = 0
        self._tool_executor: RuntimeToolExecutor | None = None  # audit-ok[field_default]: unbound until runtime binds

    def bind_tool_executor(self, executor: RuntimeToolExecutor) -> None:
        """Bind the currently leased runtime (also replaced for a repair child)."""
        self._tool_executor = executor

    @property
    def tool_router(self) -> ToolRouter:
        return self._router

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
        if role not in self._role_models or role not in self._role_prompts:
            # Schema exists on the class but this adapter was built without the
            # role's model/prompt — typed refuse, never a KeyError mid-study.
            raise AgentError(f"role {role!r} not configured on this adapter (role_models/role_prompts)")
        # Budget limits must be positive; usage accumulates on the adapter instance.
        if budget.max_llm_calls < 1:
            raise AgentError("budget.max_llm_calls must be >= 1 for a role run")
        if budget.max_tokens < 1:
            raise AgentError("budget.max_tokens must be >= 1 for a role run")
        model_id = self._role_models.get(role)
        if model_id is None:
            raise AgentError(
                f"role {role!r} has no output schema model wired: role_models entry missing "
                "(wired roles need profile-backed role_models + role_prompts)"
            )
        prompt_ref = self._role_prompts.get(role)
        if prompt_ref is None:
            raise AgentError(f"role {role!r} has no role_prompts entry (no invented prompt)")
        out_cls = _OUTPUT_MODELS[role]
        tool_refs: list[ToolCallRef] = []

        if role in _CONTEXT_ROLES:
            ctx = _parse_input(StudyContext, input)
            if role == _ROLE_PLANNER:
                # Context-typed request schema: tools_hint becomes an enum of the
                # study's tool ids (schema-checked — no prompt rule needed).
                out_cls = plan_intent_model_for(
                    tuple(ctx.tool_ids), tuple(ctx.criterion_template_ids), tuple(ctx.criterion_output_pins)
                )
            user_payload = ctx.model_dump()
        elif role == _ROLE_CRITIC:
            critic_in = _parse_input(CriticIn, input)
            user_payload = {
                "context": critic_in.context.model_dump(),
                "plan": critic_in.plan.model_dump(),
            }
        elif role == _ROLE_REVIEWER:
            reviewer_in = _parse_input(ReviewerIn, input)
            user_payload = {
                "context": reviewer_in.context.model_dump(),
                "digest_markdown": reviewer_in.digest_markdown,
            }
        else:
            if isinstance(input, AutomaticExecutorIn) or (
                isinstance(input, Mapping) and input.get("mode") == "automatic"
            ):
                return await self._run_automatic(
                    role,
                    _parse_input(AutomaticExecutorIn, input),
                    allowed=allowed,
                    budget=budget,
                    model_id=model_id,
                    prompt_ref=prompt_ref,
                    out_cls=out_cls,
                )
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
        system = self._prompts.render(prompt_ref, {"role": role})
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

    async def _run_automatic(
        self,
        role: str,
        route: AutomaticExecutorIn,
        *,
        allowed: frozenset[str],
        budget: Budget,
        model_id: str,
        prompt_ref: PromptRef,
        out_cls: type[BaseModel],
    ) -> AgentRun:
        executor = self._tool_executor
        if executor is None:
            raise AgentError("automatic tools require a leased runtime executor")
        hints = frozenset(route.task.tools_hint)
        if hints - allowed:
            raise AgentError("task tools_hint exceeds authorized tools")
        if hints:
            allowed = allowed & hints
        descriptors: list[dict[str, object]] = []
        for name in sorted(allowed):
            resolved = self._router.resolve(ToolNeed(kind="by_name", value=name))
            if resolved.status != "tool_found" or resolved.server_name is None or resolved.tool_name is None:
                raise AgentError(f"tool {name!r} is not an executable MCP binding")
            bound = self._router.require_tool(resolved.server_name, resolved.tool_name)
            descriptors.append(
                {"tool_name": name, "description": bound.description, "input_schema": bound.spec.input_schema}
            )
        refs: list[ToolCallRef] = []
        usage = Usage(prompt_tokens=0, completion_tokens=0, latency_ms=0)
        payload = {
            **route.model_dump(mode="json"),
            "tools": descriptors,
            "finish_output_schema": out_cls.model_json_schema(),
        }
        messages = [
            ChatMessage(role="system", content=self._prompts.render(prompt_ref, {"role": role})),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]
        response_format = _json_schema_response_format(ToolAction, name="tool_action")
        while True:
            await executor.check_active(route.study_id, route.attempt_id)
            _assert_budget_allows_llm(
                budget, llm_calls_used=self._llm_calls_used, tokens_used=self._tokens_used, now=self._clock.now()
            )
            parsed, step_usage = await self._complete_validated(
                model_id=model_id,
                messages=messages,
                response_format=response_format,
                out_cls=ToolAction,
                role=role,
                budget=budget,
                before_call=lambda: executor.check_active(route.study_id, route.attempt_id),
            )
            await executor.check_active(route.study_id, route.attempt_id)
            usage = Usage(
                prompt_tokens=usage.prompt_tokens + step_usage.prompt_tokens,
                completion_tokens=usage.completion_tokens + step_usage.completion_tokens,
                latency_ms=usage.latency_ms + step_usage.latency_ms,
            )
            assert isinstance(parsed, ToolAction)
            if parsed.action == "finish":
                raw_output = parsed.output_json
                if raw_output is None:
                    raise AgentError("invalid finish output: output_json is required")
                try:
                    _strict_json(raw_output)
                    output = out_cls.model_validate_json(raw_output)
                except ValueError as exc:
                    raise AgentError(f"invalid finish output: {exc}") from exc
                actual = {ref.call_id: ref for ref in refs}
                required = {output.name for output in route.task.expected_outputs if output.required}
                selected = {selection.output_name for selection in parsed.selections}
                if required - selected:
                    raise AgentError("finish is missing required output selections")
                permitted = {output.name for output in route.task.expected_outputs} | {ref.tool_name for ref in refs}
                for selection in parsed.selections:
                    ref = actual.get(selection.call_id)
                    if selection.output_name not in permitted or ref is None or ref.outcome != "ok":
                        raise AgentError("finish selection must name an allowed output and successful actual receipt")
                return AgentRun(
                    output=output, error=None, usage=usage, tool_calls=tuple(refs), output_selections=parsed.selections
                )
            call = parsed.call
            assert call is not None
            if call.tool_name not in allowed:
                raise AgentError("tool call exceeds authorized tools")
            if self._tool_calls_used >= budget.max_tool_calls:
                raise AgentError("budget.max_tool_calls exceeded")
            ctx = CallCtx(
                study_id=route.study_id,
                task_key=route.task.task_key,
                attempt_id=route.attempt_id,
                hitl_policy=self._hitl_policy,
            )
            self._tool_calls_used += 1
            ref = await executor.invoke(call.tool_name, _args_mapping(call.args), ctx)
            refs.append(ref)
            messages.extend(
                [
                    ChatMessage(role="assistant", content=parsed.model_dump_json()),
                    ChatMessage(role="user", content=ref.model_dump_json()),
                ]
            )

    async def _complete_validated(
        self,
        *,
        model_id: str,
        messages: list[ChatMessage],
        response_format: JsonSchemaResponseFormat,
        out_cls: type[BaseModel],
        role: str,
        budget: Budget,
        before_call: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[BaseModel, Usage]:
        """Complete → validate; on schema failure feed the error back once."""
        schema_error = ""
        last_content = ""
        total = Usage(prompt_tokens=0, completion_tokens=0, latency_ms=0)
        for attempt in (0, 1):
            if attempt and not self._budget_allows_repair(budget):
                break
            if before_call is not None:
                await before_call()
            _assert_budget_allows_llm(
                budget, llm_calls_used=self._llm_calls_used, tokens_used=self._tokens_used, now=self._clock.now()
            )
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
                StructuredLlmRequest(model=model_id, messages=messages, response_format=response_format)
            )
            last_content = response.content
            self._llm_calls_used += 1
            usage = response.usage
            total = Usage(
                prompt_tokens=total.prompt_tokens + usage.prompt_tokens,
                completion_tokens=total.completion_tokens + usage.completion_tokens,
                latency_ms=total.latency_ms + usage.latency_ms,
            )
            self._tokens_used += usage.prompt_tokens + usage.completion_tokens
            if self._tokens_used > budget.max_tokens:
                raise AgentError("budget.max_tokens exceeded")
            try:
                _strict_json(response.content)
                return out_cls.model_validate_json(response.content), total
            except ValueError as exc:
                schema_error = str(exc)
        raise AgentError(f"{role} output failed schema validation (after repair retry): {schema_error}")

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
        if self._tool_calls_used + len(hints) > budget.max_tool_calls:
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
            if self._tool_executor is None:
                raise AgentError("tool calls require a leased runtime executor")
            self._tool_calls_used += 1
            ref = await self._tool_executor.invoke(hint, _args_mapping(planned.args), ctx)
            refs.append(ref)
            if ref.outcome != "ok":
                raise AgentError(f"fixed recipe tool failed; actual receipt={ref.call_id}")
            continue
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
