"""ExecutorRouteIn / ExecutorRouteOut schema refuse paths."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.agents.schemas import (
    ExecutorRouteIn,
    OutputSpecDraft,
    PlannedToolCall,
    TaskSpecDraft,
    ToolArg,
)


def _task(*, tools_hint: tuple[str, ...]) -> TaskSpecDraft:
    return TaskSpecDraft(
        task_key="EXP-0",
        title="t",
        goal="g",
        route="experiment",
        depends_on=(),
        addresses=("Q1",),
        tools_hint=tools_hint,
        expected_outputs=(
            OutputSpecDraft(
                name="main",
                media_type="application/json",
                schema_ref=None,
                required=True,
                rubric=None,
            ),
        ),
        criteria=("artifact_present",),
    )


def test_executor_route_in_mismatched_hints_raises() -> None:
    with pytest.raises(ValidationError, match="tool_args tool_name set must match task.tools_hint"):
        ExecutorRouteIn(
            task=_task(tools_hint=("agent-tools:echo",)),
            study_id="s1",
            attempt_id="a1",
            tool_args=(
                PlannedToolCall(
                    tool_name="agent-tools:other",
                    args=(ToolArg(key="x", value_json="1"),),
                ),
            ),
        )


def test_executor_route_in_duplicate_tool_names_raises() -> None:
    name = "agent-tools:echo"
    with pytest.raises(ValidationError, match="tool_args tool_name values must be unique"):
        ExecutorRouteIn(
            task=_task(tools_hint=(name, name)),
            study_id="s1",
            attempt_id="a1",
            tool_args=(
                PlannedToolCall(tool_name=name, args=(ToolArg(key="x", value_json="1"),)),
                PlannedToolCall(tool_name=name, args=(ToolArg(key="y", value_json="2"),)),
            ),
        )
