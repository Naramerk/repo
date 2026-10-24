"""Typed role I/O for planner and executor-route (DESIGN §7.1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.execution.domain import Need
from coscientist.llm.types import Usage


class AgentModel(BaseModel):
    """Frozen strict role models — no extras, no silent coercion."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class StudyContext(AgentModel):
    """Planner / executor shared study snapshot (no journal state)."""

    study_id: str = Field(min_length=1)
    request: str = Field(min_length=1)
    profile: str = Field(min_length=1)


class OutputSpecDraft(AgentModel):
    name: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    schema_ref: str | None
    required: bool


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


class ExecutorRouteIn(AgentModel):
    """executor-route input: task draft + dependency context blob."""

    task: TaskSpecDraft
    dependency_context: dict[str, Any]


class ExecutorRouteOut(AgentModel):
    """Minimal executor structured output for M1 wiring (maps toward AgentTaskOutput)."""

    summary: str = Field(min_length=1)
    values: dict[str, Any]
    tool_found: tuple[str, ...]


class ToolCallRef(AgentModel):
    tool_name: str = Field(min_length=1)
    resolution_status: Literal["tool_found", "tool_built", "coder", "tool_missing"]


class AgentRun(AgentModel):
    """Result of ``AgentPort.run`` (DESIGN §7.2). ``usage`` always set on success."""

    output: BaseModel | None
    usage: Usage
    tool_calls: tuple[ToolCallRef, ...]


class AgentError(Exception):
    """Typed agent failure — not an ``AgentRun.error`` string field."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)
