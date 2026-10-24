"""LlmAgentAdapter: ``AgentPort`` over ``LlmClient`` + ``ToolRouter`` (DESIGN §7.2 / §13 D)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from coscientist.agents.schemas import (
    AgentError,
    AgentRun,
    ExecutorRouteIn,
    ExecutorRouteOut,
    PlanOut,
    StudyContext,
    ToolCallRef,
)
from coscientist.llm.client import LlmClient
from coscientist.llm.types import ChatMessage, StructuredLlmRequest, Usage
from coscientist.tools.router import ToolNeed, ToolRouter

_T = TypeVar("_T", bound=BaseModel)

_ROLE_PLANNER = "planner"
_ROLE_EXECUTOR = "executor-route"

_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    _ROLE_PLANNER: PlanOut,
    _ROLE_EXECUTOR: ExecutorRouteOut,
}


def _json_schema_response_format(model: type[BaseModel], *, name: str) -> dict[str, Any]:
    """OpenAI-compatible json_schema response_format (strict)."""
    schema = model.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


class LlmAgentAdapter:
    """Implements ``core.events.ports.AgentPort`` without editing ports.py.

    ``role_models`` maps role → provider model id (from profile; required, no fallback).
    Tool calls go through the injected ``ToolRouter`` (fake in tests; MCP = G7).
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        role_models: Mapping[str, str],
        tool_router: ToolRouter,
    ) -> None:
        if _ROLE_PLANNER not in role_models or _ROLE_EXECUTOR not in role_models:
            raise AgentError("role_models must include 'planner' and 'executor-route' (no defaults)")
        for role, model in role_models.items():
            if type(model) is not str or not model.strip():
                raise AgentError(f"role_models[{role!r}] must be a non-empty str")
        self._client = client
        self._role_models = dict(role_models)
        self._router = tool_router

    async def run(self, role: str, input: Any, tools: list[Any], budget: Any) -> AgentRun:
        del tools, budget  # port surface; tools resolved via ToolRouter
        if role not in _OUTPUT_MODELS:
            raise AgentError(f"unsupported role {role!r}")
        model_id = self._role_models[role]
        out_cls = _OUTPUT_MODELS[role]
        tool_refs: list[ToolCallRef] = []
        usage_acc = Usage(prompt_tokens=0, completion_tokens=0, latency_ms=0.0)

        if role == _ROLE_PLANNER:
            ctx = _parse_input(StudyContext, input)
            user_payload = ctx.model_dump()
        else:
            route_in = _parse_input(ExecutorRouteIn, input)
            tool_refs = await self._run_tools(route_in)
            user_payload = {
                "task": route_in.task.model_dump(),
                "dependency_context": route_in.dependency_context,
                "tool_results": [r.model_dump() for r in tool_refs],
            }

        request = StructuredLlmRequest(
            model=model_id,
            messages=[
                ChatMessage(
                    role="system",
                    content=(f"You are the {role} role. Reply with JSON matching the response_format schema only."),
                ),
                ChatMessage(
                    role="user",
                    content=json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
                ),
            ],
            response_format=_json_schema_response_format(out_cls, name=role.replace("-", "_")),
        )
        response = await self._client.complete(request)
        usage = Usage(
            prompt_tokens=usage_acc.prompt_tokens + response.usage.prompt_tokens,
            completion_tokens=usage_acc.completion_tokens + response.usage.completion_tokens,
            latency_ms=usage_acc.latency_ms + response.usage.latency_ms,
        )
        try:
            parsed = out_cls.model_validate_json(response.content)
        except ValidationError as exc:
            raise AgentError(f"{role} output failed schema validation: {exc}") from exc
        return AgentRun(output=parsed, usage=usage, tool_calls=tuple(tool_refs))

    async def _run_tools(self, route_in: ExecutorRouteIn) -> list[ToolCallRef]:
        refs: list[ToolCallRef] = []
        study_id = route_in.dependency_context.get("study_id")
        attempt_id = route_in.dependency_context.get("attempt_id")
        if type(study_id) is not str or not study_id:
            raise AgentError("executor-route dependency_context.study_id must be str")
        if type(attempt_id) is not str or not attempt_id:
            raise AgentError("executor-route dependency_context.attempt_id must be str")
        for hint in route_in.task.tools_hint:
            resolution = self._router.resolve(ToolNeed(kind="by_name", value=hint))
            refs.append(ToolCallRef(tool_name=hint, resolution_status=resolution.status))
            if resolution.status != "tool_found" or resolution.tool_name is None:
                continue
            await self._router.call(
                resolution.tool_name,
                {},
                study_id=study_id,
                task_key=route_in.task.task_key,
                attempt_id=attempt_id,
            )
        return refs


def _parse_input(model: type[_T], raw: Any) -> _T:
    if isinstance(raw, model):
        return raw
    try:
        if isinstance(raw, BaseModel):
            return model.model_validate(raw.model_dump())
        return model.model_validate(raw)
    except ValidationError as exc:
        raise AgentError(f"invalid {model.__name__}: {exc}") from exc
