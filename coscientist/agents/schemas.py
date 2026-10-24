"""Typed role I/O for planner and executor-route (DESIGN §7.1 / §7.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ToolArg(AgentModel):
    """Closed key/JSON-value pair (tool args and executor ``values``; one type)."""

    key: str = Field(min_length=1)
    value_json: str = Field(min_length=1)


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
