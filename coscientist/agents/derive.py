"""Deterministic derivation: ``PlanIntent`` → full ``PlanOut`` (frozen-plan internals).

Single source for the invariants the planner prompt used to mirror by hand:
expected-output naming after the delivering tool id and delivery rubrics.
Catalogue criteria, question bindings and answer forms remain explicit intent;
scientific acceptance is never inferred from a tool result's existence.

Acceptance is routed, not gated (universal acceptance S1,
``docs/plan/universal-acceptance-design.md`` §1/§2): every verdict/both
question resolves deterministically to one route —

- ``catalog_verified``: an addressing task cites a required substantive
  catalogue criterion (the pre-S1 path, unchanged);
- ``hypothesis_claimed``: the question is explicitly ``claim_class=falsifiable``
  and the plan turns the hypothesis contour on (``hypotheses.needed``) — RC4
  stays the owner of HYP-n presence, RC5/RC6 unchanged;
- ``evidence_grounded``: ``claim_class=value|review`` — value/review requests
  close on registered receipts with provenance, never on bare existence
  (honesty rails below), and never promoted to "verified".

The substantive bar itself is per QUESTION (legacy semantics port, see
``docs/plan/acceptance-question-coverage.md``). ``claim_class`` absent keeps
the strict pre-S1 gate — the wall is lifted only by an explicit typed claim.
Existence checks are never substantive; ``evidence_grounded`` never closes a
``falsifiable`` question (typed refuse — an honestly unverifiable claim is
refused even under the "the task always executes" law). Every rule here is
fail-closed (typed ``PlanDeriveError``); the ``accept_plan`` / lifecycle
freeze gates remain the backstop.

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
from coscientist.core.execution.domain import (
    AcceptanceRoute,
    CodeCheck,
    RequirementTemplate,
    substantive_code_check,
    template_binds_content,
)

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


def _carries_substantive_criterion(
    criteria: Sequence[str],
    templates: Mapping[str, RequirementTemplate],
) -> bool:
    """True when any cited template is required, code-checked and content-binding."""
    return any(template_binds_content(templates[c]) for c in criteria)


def _citable_content_template_ids(
    templates: Mapping[str, RequirementTemplate],
    declared_outputs: frozenset[str],
) -> list[str]:
    """Content-bearing template ids the addressing tasks COULD cite.

    A template is citable when it is substantive (required + code-checked +
    content-binding kind — same definition as the coverage bar) and its check
    either binds no concrete output or pins one of the declared outputs of
    this question's addressing tasks. Templates pinned to outputs no
    addressing task declares are not satisfiable for the question and never
    force a citation (no wall resurrection).
    """
    citable: list[str] = []
    for template_id, template in templates.items():
        check = substantive_code_check(template)
        if check is None:
            continue
        if not check.output or check.output in declared_outputs:
            citable.append(template_id)
    return sorted(citable)


def _uncovered_question_detail(intent: PlanIntent, index: int, qid: str, final_type: str) -> str:
    question = intent.questions[index]
    return f"question {qid!r} ({question.text!r}, final_type={final_type!r}) is not covered"


def _resolve_acceptance_routes(
    intent: PlanIntent,
    question_ids: tuple[str, ...],
    specs: Sequence[TaskSpecDraft],
    templates: Mapping[str, RequirementTemplate],
) -> dict[str, AcceptanceRoute]:
    """Deterministic acceptance-route resolution per verdict/both question.

    Order (design §1/§2): catalogue criterion → falsifiable-claim hypothesis
    route → value/review grounded → refuse. ``claim_class`` absent keeps the
    strict pre-S1 refusal (message unchanged); ``falsifiable`` without a
    verdict engine refuses (honest typed refuse — never grounded); a
    ``require_hypotheses`` profile already refuses ``needed=false`` plans at
    ``accept_plan``, so the plan's own ``hypotheses.needed`` is the (b) signal
    and RC4 stays the owner of HYP-n presence.

    Returns the route per resolved question id (verdict/both only — artifact
    questions have no acceptance route). Raises ``PlanDeriveError`` on
    uncoverable questions.
    """
    final_type_by_qid = {qid: q.final_type for qid, q in zip(question_ids, intent.questions, strict=True)}
    addressing_by_qid: dict[str, list[str]] = {
        qid: [spec.task_key for spec in specs if qid in spec.addresses]
        for qid in question_ids
        if final_type_by_qid[qid] in ("verdict", "both")
    }
    substantive_by_key = {spec.task_key: _carries_substantive_criterion(spec.criteria, templates) for spec in specs}
    outputs_by_key = {spec.task_key: frozenset(o.name for o in spec.expected_outputs) for spec in specs}
    routes: dict[str, AcceptanceRoute] = {}
    for qid, addressing in sorted(addressing_by_qid.items()):
        index = question_ids.index(qid)
        question = intent.questions[index]
        if any(substantive_by_key[key] for key in addressing):
            routes[qid] = "catalog_verified"
            continue
        detail = _uncovered_question_detail(intent, index, qid, final_type_by_qid[qid])
        if question.claim_class == "falsifiable":
            if intent.hypotheses.needed:
                routes[qid] = "hypothesis_claimed"
                continue
            raise PlanDeriveError(
                f"{detail}: claim_class=falsifiable requires a verdict engine — a required substantive "
                f"catalogue check on an addressing task or hypotheses.needed with HYP-n tasks (RC4); "
                f"a falsifiable claim is never closed on bare receipts"
            )
        if question.claim_class in ("value", "review"):
            # Honesty rails (design §8.i): grounded closure is receipt-backed
            # content, never bare existence. When a content-bearing catalogue
            # check is citable by these tasks, the question must cite one; and
            # at least one addressing task must declare an output at all — a
            # task delivering nothing cannot ground anything.
            declared_outputs: frozenset[str] = frozenset()
            for key in addressing:
                declared_outputs |= outputs_by_key[key]
            citable = _citable_content_template_ids(templates, declared_outputs)
            cited_substantive = any(substantive_by_key[key] for key in addressing)
            if citable and not cited_substantive:
                raise PlanDeriveError(
                    f"{detail}: claim_class={question.claim_class!r} routes evidence_grounded, but a "
                    f"content-bearing catalogue check is available — cite one of {citable!r} on an "
                    f"addressing task (grounded closure must bind receipt content, not bare existence)"
                )
            if not any(outputs_by_key[key] for key in addressing):
                raise PlanDeriveError(
                    f"{detail}: claim_class={question.claim_class!r} routes evidence_grounded, but no "
                    f"addressing task declares any output — grounded closure needs at least one "
                    f"registered receipt (declare tools_hint/output_names)"
                )
            routes[qid] = "evidence_grounded"
            continue
        # Legacy semantics (claim_class absent): the pre-S1 per-question bar,
        # message preserved verbatim — the wall is lifted only by an explicit
        # typed claim_class, never by omission (design §8.v back-compat).
        raise PlanDeriveError(
            f"{detail}: addressing tasks {addressing!r} cite "
            f"no substantive catalogue check — at least one addressing task must cite a "
            f"required substantive catalogue check (co-addressing tasks deliver evidence only)"
        )
    return routes


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
    - acceptance routes: every question needs an addressing task, and every
      verdict/both question resolves to a route (catalogue criterion cited /
      falsifiable + hypotheses.needed / value|review grounded); a question
      without an explicit ``claim_class`` still needs >=1 addressing task
      citing a substantive catalogue criterion (per-question bar preserved).
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
    # Acceptance routes (S1): per-question resolution — catalogue criterion →
    # falsifiable hypothesis route → value/review grounded (with honesty rails)
    # → typed refuse. The strict pre-S1 message is preserved for questions
    # without an explicit claim_class.
    _resolve_acceptance_routes(intent, question_ids, specs, templates)
    return tuple(specs)


def derive_question_drafts(
    intent: PlanIntent,
    *,
    acceptance_routes: Mapping[str, AcceptanceRoute] | None = None,
) -> tuple[QuestionDraft, ...]:
    """Assign ids without rewriting the requested answer form.

    ``acceptance_routes`` (from :func:`_resolve_acceptance_routes`) stamps the
    derive-resolved route on the draft; questions without a resolved route
    (artifact-only, or legacy direct callers) keep ``None`` — never a silent
    ``catalog_verified``.
    """
    routes: Mapping[str, AcceptanceRoute] = {}
    if acceptance_routes is not None:
        routes = acceptance_routes
    return tuple(
        QuestionDraft(
            id=_QUESTION_ID_FMT.format(i),
            text=q.text,
            final_type=q.final_type,
            claim_class=q.claim_class,
            acceptance_route=routes.get(_QUESTION_ID_FMT.format(i)),
        )
        for i, q in enumerate(intent.questions, start=1)
    )


def derive_plan(
    intent: PlanIntent,
    *,
    catalog_templates: Sequence[RequirementTemplate],
    tool_ids: Sequence[str],
) -> PlanOut:
    """Derive the full planner ``PlanOut`` from typed intent (single gate owner)."""
    tasks = derive_task_specs(intent, catalog_templates=catalog_templates, tool_ids=tool_ids)
    templates = {t.id: t for t in catalog_templates}
    question_ids = tuple(_QUESTION_ID_FMT.format(i) for i in range(1, len(intent.questions) + 1))
    routes = _resolve_acceptance_routes(intent, question_ids, tasks, templates)
    questions = derive_question_drafts(intent, acceptance_routes=routes)
    return PlanOut(
        tasks=tasks,
        questions=questions,
        hypotheses=intent.hypotheses,
        literature=intent.literature,
    )
