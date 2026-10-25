"""Deterministic derivation: ``PlanIntent`` → full ``PlanOut`` (frozen-plan internals).

Single source for the invariants the planner prompt used to mirror by hand:
expected-output naming after the delivering tool id, auto ``artifact_exists``
rubrics, empty criteria (measured criteria are never invented), question
coverage. Every rule here is fail-closed (typed ``PlanDeriveError``); the
``accept_plan`` / lifecycle freeze gates remain the backstop.

Lives in the agents layer (next to ``TaskSpecDraft``/``PlanOut``) because
``coscientist.core`` must not import agent role schemas.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from coscientist.agents.schemas import (
    OutputSpecDraft,
    PlanIntent,
    PlanOut,
    QuestionDraft,
    RubricDraft,
    RubricItemDraft,
    TaskIntent,
    TaskSpecDraft,
)
from coscientist.core.execution.domain import RequirementTemplate

__all__ = [
    "CODER_HINT",
    "PlanDeriveError",
    "derive_plan",
    "derive_question_drafts",
    "derive_task_specs",
]

# System-assigned task keys are t1..tN; planner slugs share the same namespace
# (letters / digits / underscore / dash / dot — same shape as existing EXP-0 keys).
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_KEY_AUTO_FMT = "t{}"
_QUESTION_ID_FMT = "q{}"
_MEDIA_TYPE_JSON = "application/json"
# Reserved tools_hint id marking code-execution delivery (router coder binding,
# ``tools.router.CODER_NEED_VALUE``; equality tested). A task either declares
# tool calls or one coder job — never both.
CODER_HINT = "coder"


class PlanDeriveError(Exception):
    """Typed refuse: the intent cannot be derived into a valid plan (fail-closed)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _artifact_exists_rubric() -> RubricDraft:
    return RubricDraft(
        items=(
            RubricItemDraft(
                id="exists",
                text="required artifact exists",
                required=True,
                check="code",
                code_kind="artifact_exists",
            ),
        ),
        aggregate="all_required",
        k=None,
    )


def _assign_task_keys(tasks: Sequence[TaskIntent]) -> tuple[str, ...]:
    """Planner slug when given, else system ``t{N}``; unique and slug-shaped."""
    keys: list[str] = []
    for i, task in enumerate(tasks, start=1):
        key = task.key
        if key is None:
            key = _KEY_AUTO_FMT.format(i)
        if not _KEY_RE.match(key):
            raise PlanDeriveError(
                f"task #{i} key {key!r} must match {_KEY_RE.pattern!r}"
            )
        keys.append(key)
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise PlanDeriveError(f"task keys must be unique, got duplicates {duplicates!r}")
    return tuple(keys)


def _assert_tools_hint(task: TaskIntent, key: str, tool_ids: frozenset[str]) -> None:
    unknown = [h for h in task.tools_hint if h not in tool_ids]
    if unknown:
        raise PlanDeriveError(
            f"task {key!r} tools_hint {unknown!r} not in the study tool surface "
            f"(allowed={sorted(tool_ids)!r})"
        )
    if len(set(task.tools_hint)) != len(task.tools_hint):
        raise PlanDeriveError(f"task {key!r} tools_hint entries must be unique")
    if CODER_HINT in task.tools_hint and len(task.tools_hint) > 1:
        raise PlanDeriveError(
            f"task {key!r} tools_hint {list(task.tools_hint)!r}: code-execution "
            f"delivery ({CODER_HINT!r}) is exclusive — split MCP tool calls into "
            f"another task"
        )


def derive_task_specs(
    intent: PlanIntent,
    *,
    catalog_templates: Sequence[RequirementTemplate],
    tool_ids: Sequence[str],
) -> tuple[TaskSpecDraft, ...]:
    """Derive full ``TaskSpecDraft`` rows from typed intent (deterministic).

    - expected_outputs: exactly one per ``tools_hint`` entry, named after the
      tool id (the tool result artifact is recorded under that id), JSON,
      ``artifact_exists`` rubric auto-attached;
    - criteria: defaulted empty (never invented);
    - addresses: every task addresses every derived question (maximal
      coverage — intent carries no per-task addressing information).
    """
    del catalog_templates  # reserved for the measured-criterion linkage slice
    if not intent.tasks:
        raise PlanDeriveError("intent has no tasks")
    question_ids = tuple(_QUESTION_ID_FMT.format(i) for i in range(1, len(intent.questions) + 1))
    keys = _assign_task_keys(intent.tasks)
    key_set = frozenset(keys)
    tools = frozenset(tool_ids)
    specs: list[TaskSpecDraft] = []
    for task, key in zip(intent.tasks, keys, strict=True):
        title = task.title
        if title is None:
            title = key
        _assert_tools_hint(task, key, tools)
        unknown_deps = [d for d in task.depends_on if d not in key_set]
        if unknown_deps:
            raise PlanDeriveError(
                f"task {key!r} depends_on {unknown_deps!r} unknown (keys={sorted(key_set)!r})"
            )
        if key in task.depends_on:
            raise PlanDeriveError(f"task {key!r} depends_on itself")
        outputs = tuple(
            OutputSpecDraft(
                name=tool_id,
                media_type=_MEDIA_TYPE_JSON,
                schema_ref=None,
                required=True,
                rubric=_artifact_exists_rubric(),
            )
            for tool_id in task.tools_hint
        )
        specs.append(
            TaskSpecDraft(
                task_key=key,
                title=title,
                goal=task.goal,
                route=task.route,
                depends_on=tuple(task.depends_on),
                addresses=question_ids,
                tools_hint=tuple(task.tools_hint),
                expected_outputs=outputs,
                criteria=(),
            )
        )
    return tuple(specs)


def derive_question_drafts(intent: PlanIntent) -> tuple[QuestionDraft, ...]:
    """System-assigned ids ``q1..qN``; final_type='artifact' (intent carries text only)."""
    return tuple(
        QuestionDraft(id=_QUESTION_ID_FMT.format(i), text=q.text, final_type="artifact")
        for i, q in enumerate(intent.questions, start=1)
    )


def derive_plan(
    intent: PlanIntent,
    *,
    catalog_templates: Sequence[RequirementTemplate],
    tool_ids: Sequence[str],
) -> PlanOut:
    """Derive the full planner ``PlanOut`` from typed intent (single gate owner)."""
    questions = derive_question_drafts(intent)
    return PlanOut(
        tasks=derive_task_specs(intent, catalog_templates=catalog_templates, tool_ids=tool_ids),
        questions=questions,
        hypotheses=intent.hypotheses,
        literature=intent.literature,
    )
