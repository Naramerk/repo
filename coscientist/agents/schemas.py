"""Typed role I/O for planner and executor-route (DESIGN §7.1 / §7.2)."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from coscientist.core.execution.domain import Need
from coscientist.llm.types import Usage
from coscientist.tools.tool_resolution import ToolResolutionStatus


class AgentModel(BaseModel):
    """Frozen strict role models — no extras, no silent coercion."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class StudyContext(AgentModel):
    """Planner / executor shared study snapshot (no journal state)."""

    study_id: str = Field(min_length=1)
    request: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    # Exact check-catalog template ids the planner may reference in task
    # ``criteria`` (empty tuple: criteria must be empty). Passed explicitly so
    # the planner never has to guess valid ids.
    criterion_template_ids: tuple[str, ...] = Field(default=())
    # Exact callable tool ids on the study's tool surface (cassette static:<name>
    # or MCP server:tool). tools_hint entries must be exactly these ids.
    tool_ids: tuple[str, ...] = Field(default=())


class RubricItemDraft(AgentModel):
    """Planner draft for one ``RubricItem`` (mapped at accept_plan)."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    required: bool
    check: Literal["code", "judge"]
    code_kind: str | None


class RubricDraft(AgentModel):
    """Planner draft rubric — never invented by CLI."""

    items: tuple[RubricItemDraft, ...] = Field(min_length=1)
    aggregate: Literal["all_required", "k_of_n"]
    k: int | None


class OutputSpecDraft(AgentModel):
    name: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    schema_ref: str | None
    required: bool
    rubric: RubricDraft | None


class TaskSpecDraft(AgentModel):
    """Planner draft task (mapped to core ``TaskSpec`` at accept_plan)."""

    task_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    route: str = Field(min_length=1)
    depends_on: tuple[str, ...]
    addresses: tuple[str, ...]
    tools_hint: tuple[str, ...]
    expected_outputs: tuple[OutputSpecDraft, ...]
    criteria: tuple[str, ...]


class QuestionDraft(AgentModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    final_type: Literal["verdict", "artifact", "both"]


class PlanOut(AgentModel):
    """Planner structured output (DESIGN §7.1)."""

    tasks: tuple[TaskSpecDraft, ...]
    questions: tuple[QuestionDraft, ...]
    hypotheses: Need
    literature: Need


class TaskIntent(AgentModel):
    """Planner typed intent for one task (frozen-plan internals are derived).

    No expected_outputs / rubrics / criteria / question addressing: those are
    derived deterministically (``coscientist.agents.derive``); the LLM never
    guesses frozen-plan internals.
    """

    goal: str = Field(min_length=1)
    route: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    tools_hint: tuple[str, ...] = ()
    # Optional planner slug; system assigns ``t{N}`` (1-based) when unset.
    key: str | None = Field(default=None)
    title: str | None = Field(default=None)


class QuestionIntent(AgentModel):
    """Planner typed intent for one research question."""

    text: str = Field(min_length=1)
    final_type: Literal["verdict", "artifact", "both"]


class PlanIntent(AgentModel):
    """Minimal planner structured output (typed intent, not a frozen plan).

    Core derives the full ``PlanOut`` (TaskSpecDraft rows: output naming after
    tool ids, auto ``artifact_exists`` rubrics, empty criteria, question
    coverage) via ``coscientist.agents.derive.derive_plan`` — single source,
    no hand-written prompt mirror of the accept gates.
    """

    tasks: tuple[TaskIntent, ...]
    questions: tuple[QuestionIntent, ...]
    hypotheses: Need
    literature: Need


def plan_intent_model_for(tool_ids: tuple[str, ...]) -> type[PlanIntent]:
    """Context-typed ``PlanIntent`` request schema (schema-checked tools_hint).

    When the study surface has tool ids, ``tools_hint`` becomes an enum of
    exactly those ids in the response_format JSON schema — the "tools_hint
    must be ids from the context's tool_ids" rule is enforced by the schema
    itself (no prompt sentence). With no tools, ``tools_hint`` must be empty.
    """
    if tool_ids:
        ids = tuple(sorted(set(tool_ids)))
        task_cls = create_model(
            "PlanIntentTaskTools",
            __base__=TaskIntent,
            tools_hint=(tuple[Literal[*ids], ...], Field(default=())),  # type: ignore[valid-type]
        )
        return create_model(
            "PlanIntentTools",
            __base__=PlanIntent,
            tasks=(tuple[task_cls, ...], Field(default=())),  # type: ignore[valid-type]
        )
    no_tools = create_model(
        "PlanIntentTaskNoTools",
        __base__=TaskIntent,
        tools_hint=(tuple[str, ...], Field(default=(), max_length=0)),
    )
    return create_model(
        "PlanIntentNoTools",
        __base__=PlanIntent,
        tasks=(tuple[no_tools, ...], Field(default=())),  # type: ignore[valid-type]
    )


class ToolArg(AgentModel):
    """Closed key/JSON-value pair (tool args and executor ``values``; one type)."""

    key: str = Field(min_length=1)
    value_json: str = Field(min_length=1)

    @field_validator("value_json", mode="before")
    @classmethod
    def _value_json_accepts_object(cls, v: object) -> object:
        """Providers cannot always nest escaped JSON inside a JSON string.

        Accepting the parsed value itself and canonicalizing to JSON text is
        lossless; the wire contract (``value_json`` str) is unchanged.
        """
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        return v

    @field_validator("value_json")
    @classmethod
    def _value_json_is_json(cls, v: str) -> str:
        try:
            json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"value_json must be valid JSON text: {exc}") from exc
        return v


class PlannedToolCall(AgentModel):
    """Typed tool invocation requested for executor-route (args, not empty ``{}``)."""

    tool_name: str = Field(min_length=1)
    args: tuple[ToolArg, ...]


class ExecutorRouteIn(AgentModel):
    """executor-route input: task draft + typed study/attempt ids + tool args."""

    task: TaskSpecDraft
    study_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    tool_args: tuple[PlannedToolCall, ...]

    @model_validator(mode="after")
    def _tool_args_cover_hints(self) -> ExecutorRouteIn:
        hints = list(self.task.tools_hint)
        names = [c.tool_name for c in self.tool_args]
        if sorted(names) != sorted(hints):
            raise ValueError(
                f"tool_args tool_name set must match task.tools_hint exactly (hints={hints!r}, args={names!r})"
            )
        if len(names) != len(set(names)):
            raise ValueError("tool_args tool_name values must be unique")
        return self


class ExecutorRouteOut(AgentModel):
    """Minimal executor structured output for M1 wiring (maps toward AgentTaskOutput)."""

    summary: str = Field(min_length=1)
    values: tuple[ToolArg, ...]
    tool_found: tuple[str, ...]


class ToolCallRef(AgentModel):
    tool_name: str = Field(min_length=1)
    resolution_status: ToolResolutionStatus
    args: tuple[ToolArg, ...]
    result_json: str | None


class AgentRun(AgentModel):
    """Result of ``AgentPort.run`` (DESIGN §7.2)."""

    output: BaseModel | None
    error: str | None
    usage: Usage
    tool_calls: tuple[ToolCallRef, ...]


class AgentError(Exception):
    """Typed agent refuse / configuration failure."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)
