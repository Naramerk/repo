"""Deterministic derivation: ``PlanIntent`` → full ``PlanOut`` (frozen-plan internals).

Single source for the invariants the planner prompt used to mirror by hand:
expected-output naming after the delivering tool id and delivery rubrics.
Catalogue criteria, question bindings and answer forms remain explicit intent;
scientific acceptance is never inferred from a tool result's existence.
The substantive bar is per QUESTION (legacy semantics port, see
``docs/plan/acceptance-question-coverage.md``): a verdict/both question must
have at least one addressing task citing a substantive catalogue criterion;
co-addressing tasks deliver evidence through their auto-registered output
rubrics. Every rule here is fail-closed (typed ``PlanDeriveError``); the
``accept_plan`` / lifecycle freeze gates remain the backstop.

Lives in the agents layer (next to ``TaskSpecDraft``/``PlanOut``) because
``coscientist.core`` must not import agent role schemas.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

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
from coscientist.core.execution.domain import CodeCheck, RequirementTemplate

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


# Check kinds that bind a criterion to the content of the delivered artifact
# (presence-only / unimplemented kinds are never substantive).
_SUBSTANTIVE_CHECK_KINDS = frozenset(
    {
        "json_pointer",
        "value_matches",
        "metric_threshold",
        "quote_in_source",
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
    }
)


def _carries_substantive_criterion(
    criteria: Sequence[str],
    templates: Mapping[str, RequirementTemplate],
) -> bool:
    """True when any cited template is required, code-checked and content-binding."""
    for c in criteria:
        template = templates[c]
        check = template.check
        if template.required and isinstance(check, CodeCheck) and check.kind in _SUBSTANTIVE_CHECK_KINDS:
            return True
    return False


def _assign_task_keys(tasks: Sequence[TaskIntent]) -> tuple[str, ...]:
    """Planner slug when given, else system ``t{N}``; unique and slug-shaped."""
    keys: list[str] = []
    for i, task in enumerate(tasks, start=1):
        key = task.key
        if key is None:
            key = _KEY_AUTO_FMT.format(i)
        if not _KEY_RE.match(key):
            raise PlanDeriveError(f"task #{i} key {key!r} must match {_KEY_RE.pattern!r}")
        keys.append(key)
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise PlanDeriveError(f"task keys must be unique, got duplicates {duplicates!r}")
    return tuple(keys)


def _assert_tools_hint(task: TaskIntent, key: str, tool_ids: frozenset[str]) -> None:
    unknown = [h for h in task.tools_hint if h not in tool_ids]
    if unknown:
        raise PlanDeriveError(
            f"task {key!r} tools_hint {unknown!r} not in the study tool surface (allowed={sorted(tool_ids)!r})"
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
    - criteria: explicit catalogue references, validated without rewriting;
    - addresses: only explicit 1-based question references, mapped to core ids;
    - question coverage: every question needs an addressing task, and every
      verdict/both question needs >=1 addressing task citing a substantive
      catalogue criterion (per-question bar — co-addressing tasks deliver
      evidence only).
    """
    if not intent.tasks:
        raise PlanDeriveError("intent has no tasks")
    if not intent.questions:
        raise PlanDeriveError("intent has no questions")
    templates = {t.id: t for t in catalog_templates}
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
        output_names = task.output_names or task.tools_hint
        if len(set(task.question_indices)) != len(task.question_indices):
            raise PlanDeriveError(f"task {key!r} question_indices must be unique")
        if any(type(i) is not int or i < 1 or i > len(question_ids) for i in task.question_indices):
            raise PlanDeriveError(f"task {key!r} question_indices outside declared questions")
        if len(set(task.criteria)) != len(task.criteria):
            raise PlanDeriveError(f"task {key!r} criteria must be unique")
        unknown_criteria = [c for c in task.criteria if c not in templates]
        if unknown_criteria:
            raise PlanDeriveError(f"task {key!r} unknown criterion templates {unknown_criteria!r}")
        for criterion in task.criteria:
            check = templates[criterion].check
            if isinstance(check, CodeCheck) and check.output and check.output not in output_names:
                raise PlanDeriveError(
                    f"task {key!r} criterion {criterion!r} references undeclared output {check.output!r}"
                )
        unknown_deps = [d for d in task.depends_on if d not in key_set]
        if unknown_deps:
            raise PlanDeriveError(f"task {key!r} depends_on {unknown_deps!r} unknown (keys={sorted(key_set)!r})")
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
            for tool_id in output_names
        )
        specs.append(
            TaskSpecDraft(
                task_key=key,
                title=title,
                goal=task.goal,
                route=task.route,
                depends_on=tuple(task.depends_on),
                addresses=tuple(question_ids[i - 1] for i in task.question_indices),
                tools_hint=tuple(task.tools_hint),
                expected_outputs=outputs,
                criteria=tuple(task.criteria),
            )
        )
    covered = {q for spec in specs for q in spec.addresses}
    uncovered = [qid for qid in question_ids if qid not in covered]
    if uncovered:
        raise PlanDeriveError(f"questions without explicitly addressing tasks: {uncovered!r}")
    # Legacy acceptance semantics: the substantive bar lives on the QUESTION,
    # not on every addressing task. A verdict/both question needs at least one
    # addressing task citing a substantive catalogue criterion; co-addressing
    # tasks deliver evidence through their auto-registered output rubrics.
    # A verdict question without any such task still refuses here — evidence
    # delivery and route strings do not satisfy this gate.
    final_type_by_qid = {qid: q.final_type for qid, q in zip(question_ids, intent.questions, strict=True)}
    addressing_by_qid: dict[str, list[str]] = {
        qid: [spec.task_key for spec in specs if qid in spec.addresses]
        for qid in question_ids
        if final_type_by_qid[qid] in ("verdict", "both")
    }
    substantive_by_key = {spec.task_key: _carries_substantive_criterion(spec.criteria, templates) for spec in specs}
    for qid, addressing in sorted(addressing_by_qid.items()):
        if any(substantive_by_key[key] for key in addressing):
            continue
        index = question_ids.index(qid)
        raise PlanDeriveError(
            f"question {qid!r} ({intent.questions[index].text!r}, final_type="
            f"{final_type_by_qid[qid]!r}) is not covered: addressing tasks {addressing!r} cite "
            f"no substantive catalogue check — at least one addressing task must cite a "
            f"required substantive catalogue check (co-addressing tasks deliver evidence only)"
        )
    return tuple(specs)


def derive_question_drafts(intent: PlanIntent) -> tuple[QuestionDraft, ...]:
    """Assign ids without rewriting the requested answer form."""
    return tuple(
        QuestionDraft(id=_QUESTION_ID_FMT.format(i), text=q.text, final_type=q.final_type)
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
