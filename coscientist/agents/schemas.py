"""Typed role I/O for planner and executor-route (DESIGN §7.1 / §7.2)."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from coscientist.core.execution.domain import (
    AcceptanceRoute,
    ClaimClass,
    ExpansionRequest,
    Need,
    TaskKind,
)
from coscientist.llm.types import Usage
from coscientist.tools.tool_resolution import ToolResolutionStatus


class AgentModel(BaseModel):
    """Frozen strict role models — no extras, no silent coercion."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RegisteredInput(AgentModel):
    """One registered user study input (name + CAS artifact ref).

    Journal-sourced readback of the ingest-gate ``ArtifactRegistered`` rows
    (``producer=user``). Mirrors the POST /studies study-level inputs
    semantics exactly (DESIGN_M1 §9 / PR19): user blobs are Study-scoped
    (``Study.user_input_ids``; bound to every task attempt by core via
    ``study_user_input_bindings``), NOT per-task LLM-chosen — so
    ``TaskIntent`` deliberately carries no input references and the planner
    sees names (no prompt-path guessing), while the executor route receives
    the same rows as CAS refs.
    """

    name: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    media_type: str = Field(min_length=1)


class CriterionOutputPin(AgentModel):
    """One catalogue criterion bound to the output its check reads."""

    criterion: str = Field(min_length=1)
    requires_output: str = Field(min_length=1)


# RC4 readable pin (live st_idem_1ea2b5c4…): with the hypotheses stage required,
# the model twice produced plans with ``hypotheses.needed=true`` but ZERO HYP-n
# tasks — the ≥1-HYP-task rule lived only in the accept_plan validator
# (``assert_hyp_n_when_needed``), invisible at generation time. This pin states
# the contract to the planner as fact + consequence (criterion_output_pins
# precedent: readable context, not a prompt patch). The validator stays the
# honest backstop; the pin only makes the rule visible before it fires.
HYPOTHESES_STAGE_REQUIRED_PIN = (
    "Стадия гипотез обязательна: план обязан содержать задачу стадии гипотез "
    "(kind=hypotheses) с ключом вида HYP-1 (HYP-2, …), иначе план будет "
    "отклонён валидатором accept_plan (планы без задачи HYP-n отклоняются). "
    "Задачи экспериментов (EXP-…), где применимо, зависят от выбранной "
    "гипотезы через depends_on."
)


class StudyContext(AgentModel):
    """Planner / executor shared study snapshot (no journal state)."""

    study_id: str = Field(min_length=1)
    request: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    # Study-level hypothesis requirement (read-only mirror of
    # ``Study.require_hypotheses``): when true, the profile expects this plan
    # to turn hypotheses ON — ``hypotheses.needed=true`` with the motivating
    # reason, plus at least one ``HYP-n`` task. Carried as readable context so
    # the planner can satisfy the requirement while planning (live refusal
    # bor7/st_1a17f6db…: the model set ``needed=false`` because it never SAW
    # the requirement). Leaving it off is refused downstream by the honest
    # typed profile gate — this field adds visibility, not a new constraint.
    require_hypotheses: StrictBool = Field(default=False)
    # RC4 readable pin (see ``HYPOTHESES_STAGE_REQUIRED_PIN``): set by the CLI
    # exactly when ``require_hypotheses`` is true — the accept gate additionally
    # demands ≥1 task keyed ``HYP-n``, and the live refusal behind the constant
    # shows a bare bool flag is not enough for the model to plan the stage in.
    # Empty string when the stage is not required (payload parity). Visibility
    # only: ``assert_hyp_n_when_needed`` stays the honest typed refusal.
    hypotheses_stage_pin: str = Field(default="")
    # Exact check-catalog template ids the planner may reference in task
    # ``criteria`` (empty tuple: criteria must be empty). Passed explicitly so
    # the planner never has to guess valid ids.
    criterion_template_ids: tuple[str, ...] = Field(default=())
    # Catalogue templates whose check pins a concrete output name, serialized
    # as readable objects ({"criterion": ..., "requires_output": ...}) so the
    # planner sees the binding without decoding position arrays. Citing such a
    # criterion on a task that does not declare that output (output_names or
    # tools_hint — the delivering tool id) is uncitable: derive DROPS it and
    # records the drop; accept_plan journals it (the schema description on
    # criteria states the contract — parse no longer refuses).
    criterion_output_pins: tuple[CriterionOutputPin, ...] = Field(default=())
    # Exact callable tool ids on the study's tool surface (cassette static:<name>,
    # MCP server:tool, or the reserved coder binding id "coder" when a coder
    # route is attached). tools_hint entries must be exactly these ids.
    tool_ids: tuple[str, ...] = Field(default=())
    # Registered user study inputs (see ``RegisteredInput``): names + CAS refs
    # the planner plans against; empty tuple when the study has no user blobs.
    inputs: tuple[RegisteredInput, ...] = Field(default=())
    # Bounded replan feedback (adapter repair pattern at plan level): the
    # derive/accept_plan refusal detail from the previous planner round, fed
    # back once by the driver. Empty string on every first-round call (the
    # scientific request itself is never edited to carry repairs).
    plan_refusal_hint: str = Field(default="")


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
    # S1 universal acceptance: planner claim-class intent carried verbatim and
    # the acceptance route resolved deterministically in derive. ``None`` only
    # for questions whose final_type never entered route resolution (artifact)
    # or legacy callers that bypass derive — never a silent catalog_verified.
    claim_class: ClaimClass | None = None  # audit-ok[field_default]: absent keeps pre-S1 strict gate
    acceptance_route: AcceptanceRoute | None = None  # audit-ok[field_default]: unrouted unless derive resolved it


class PlanOut(AgentModel):
    """Planner structured output (DESIGN §7.1).

    This is the DERIVED plan, never the planner response model — the planner
    emits ``PlanIntent`` (``plan_intent_model_for`` builds that wire schema),
    and the critic only reads ``PlanOut`` serialized into its user payload.
    No response_format is ever built from this model, so system-set fields
    need no wire exclusion here.
    """

    tasks: tuple[TaskSpecDraft, ...]
    questions: tuple[QuestionDraft, ...]
    hypotheses: Need
    literature: Need
    # Uncitable pin-mismatch criteria derive DROPPED, as ``(task_key,
    # (template_id, ...))`` — set by ``derive_task_specs``, never by a model
    # (the planner cannot emit it: it is not in PlanIntent); ``accept_plan``
    # journals the drops on PlanAccepted's readable system note.
    uncitable_drops: tuple[tuple[str, tuple[str, ...]], ...] = ()


class TaskIntent(AgentModel):
    """Planner typed intent for one task (frozen-plan internals are derived).

    Question references are 1-based positions, not model-invented core ids.
    Criteria reference catalogue templates; frozen check objects stay core-owned.

    ``route`` is free ``str`` on THIS base model only because core ``TaskSpec``
    legitimately carries system-stage routes (``spec``/``plan``/``report``) the
    planner must never choose. The planner's wire schema (built by
    :func:`plan_intent_model_for`) narrows it to
    ``PLANNER_TASK_ROUTES`` — a closed Literal enum, so an invented route
    synonym fails at parse, not at dispatch.
    """

    goal: str = Field(min_length=1)
    route: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    tools_hint: tuple[str, ...] = ()
    output_names: tuple[str, ...] = ()
    # 1-based positions; ge=1 lives in the response_format JSON schema (same
    # schema-guidance pattern as the tools_hint enum) — models stop emitting
    # 0-based indices when the bound is visible, not only when told.
    question_indices: tuple[Annotated[int, Field(ge=1)], ...] = ()
    criteria: tuple[str, ...] = ()
    # Optional planner slug; system assigns ``t{N}`` (1-based) when unset.
    key: str | None = Field(default=None)
    title: str | None = Field(default=None)

    @field_validator("output_names")
    @classmethod
    def _declared_output_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name or name != name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("output_names must be unique nonblank names without surrounding whitespace")
        return names


class QuestionIntent(AgentModel):
    """Question meaning and requested answer form, preserved by derivation.

    ``claim_class`` is the planner's typed claim-class intent (legacy JEV
    triage): ``value``/``review`` questions may close on grounded evidence
    (route ``evidence_grounded``), ``falsifiable`` ones require a verdict
    engine (catalogue criterion or the hypothesis route). Absent (``None``)
    keeps the strict pre-S1 gate — the wall is lifted only by an explicit
    typed claim, never by omission.
    """

    text: str = Field(min_length=1)
    final_type: Literal["verdict", "artifact", "both"]
    claim_class: ClaimClass | None = None  # audit-ok[field_default]: absent keeps pre-S1 strict gate


class PlanIntent(AgentModel):
    """Minimal planner structured output (typed intent, not a frozen plan).

    Core assigns ids/output names and freezes selected catalogue checks.
    It never guesses scientific acceptance or question coverage.
    """

    tasks: tuple[TaskIntent, ...]
    questions: tuple[QuestionIntent, ...]
    hypotheses: Need
    literature: Need

    @model_validator(mode="after")
    def _question_indices_reference_declared_questions(self) -> PlanIntent:
        """Task question references must resolve inside THIS intent's questions.

        ``question_indices`` are 1-based positions (``TaskIntent`` docstring).
        Failing here — at schema parse — feeds the adapter's targeted repair
        round the exact offsets, instead of burning a full derive→critic→replan
        cycle before ``PlanDeriveError`` refuses the same mistake.
        """
        declared = len(self.questions)
        for position, task in enumerate(self.tasks, start=1):
            outside = [idx for idx in task.question_indices if idx < 1 or idx > declared]
            if outside:
                raise ValueError(
                    f"tasks[{position}].question_indices {outside} outside declared "
                    f"questions 1..{declared} (1-based positions of this intent's questions)"
                )
        return self


# Closed planner route vocabulary (live st_1afbcc28, wave-11 DeepSeek: the
# planner emitted route="hypothesis_formation" — a string with no executor —
# so the route-kind dispatch (#323, exact match on "hypotheses") fell the HYP-n
# task through to the executor lane and the study burned on the coder fallback).
# Same class as #320 (invented coder outputs): the fix is generation-time, not
# another alias table — the vocabulary is closed in the response schema itself.
# Legal = TaskKind minus SYSTEM stages: spec/plan/report are intake/system
# stages (STAGE_KEYS + PLAN containers are created by the system, never
# planner-chosen); hypotheses → wired hypothesizer (route-kind dispatch),
# literature → wired researcher stage / literature-tool executor lane,
# experiment → executor-route lane (tools / coder delivery). Core ``TaskSpec``
# keeps free-text routes — system stages legitimately use the reserved values.
PLANNER_TASK_ROUTES: tuple[str, ...] = tuple(
    kind.value
    for kind in TaskKind
    if kind not in (TaskKind.SPEC, TaskKind.PLAN, TaskKind.REPORT)
)

# Route guidance riding the response_format JSON schema (the tools_hint /
# question_indices precedent): the enum is the constraint; this sentence states
# the consequence so the planner picks the route that runs the right executor.
_ROUTE_VOCABULARY_GUIDANCE = (
    "Route kind determines the executor: experiment runs the tool/coder "
    "executor lane, hypotheses runs the wired hypothesizer, literature runs "
    "the literature stage. Choose exactly one of the enum values — any other "
    "spelling is refused at parse and no executor exists for it."
)


def _single_const_to_enum(js: dict[str, Any], *path: str) -> dict[str, Any]:
    """Normalize a single-``const`` schema node at ``path`` to ``{"enum": [...]}``.

    ``Literal[one_member]`` serializes as ``{"const": member}``; some providers
    ignore ``const`` in response_format schemas. Review finding (LOW): always
    emit ``{"enum": [member]}``. Mutates the handler's schema tree in place and
    returns it unchanged when the path or the const is absent.
    """
    node: dict[str, Any] | None
    node = js
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    leaf = path[-1]
    if isinstance(node, dict):
        target = node.get(leaf)
        if isinstance(target, dict) and "const" in target:
            node[leaf] = {"enum": [target["const"]], "type": "string"}
    return js


class _ToolsHintEnumJsonSchemaMixin(AgentModel):
    """Normalize a single-id ``tools_hint`` schema to ``enum`` (not ``const``)."""

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> dict[str, Any]:
        return _single_const_to_enum(handler(schema), "properties", "tools_hint", "items")


class _OutputNameEnumJsonSchemaMixin(AgentModel):
    """Normalize a single-name ``name`` schema to ``enum`` (not ``const``).

    Same provider finding as ``_ToolsHintEnumJsonSchemaMixin``; the hook runs on
    the output item's own schema (a ``$defs`` entry), so the path is one level.
    """

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> dict[str, Any]:
        return _single_const_to_enum(handler(schema), "properties", "name")


# Schema-guidance text for the required-hypotheses plan request (rides the
# response_format JSON schema the adapter echoes into the system message — the
# question_indices ge=1 / tools_hint enum precedent: bounds visible at
# generation time, not only when a validator refuses afterwards).
_HYP_STAGE_TASKS_GUIDANCE = (
    "Hypotheses stage is required: hypotheses.needed must be true and tasks must "
    "include at least one hypotheses-stage task (kind=hypotheses) whose key is "
    "HYP-1 (HYP-2, ...). A plan without a HYP-n task is refused by the accept "
    "validator (hyp_n_required). Where applicable, experiment (EXP-...) tasks "
    "depend on the chosen HYP task via depends_on."
)

# Generation-time citation teacher (#313 map + #316 escape, now the ONLY
# parse-side hint): the mis-citation raise is gone — a criterion whose check
# pins an output the task never declares is DROPPED at derive (recorded, then
# journaled at accept). The schema states the contract as fact + consequence;
# the derive drop + the per-question coverage gate are the enforcement.
_CRITERIA_CITATION_GUIDANCE = (
    "Cite catalogue templates by id only. A criterion whose check pins a "
    "concrete output is verifiable only on a task that declares that output "
    "(output_names or tools_hint) — an uncitable citation is dropped by the "
    "system at derive and the task runs with its own receipts. Cite no "
    "criteria at all (criteria: []) only when no citable content-bearing "
    "check exists for the question."
)


def plan_intent_model_for(
    tool_ids: tuple[str, ...],
    criterion_ids: tuple[str, ...] = (),
    require_hypotheses_stage: bool = False,
) -> type[PlanIntent]:
    """Context-typed ``PlanIntent`` request schema (schema-checked tools_hint).

    When the study surface has tool ids, ``tools_hint`` becomes an enum of
    exactly those ids in the response_format JSON schema — the "tools_hint
    must be ids from the context's tool_ids" rule is enforced by the schema
    itself (no prompt sentence). With no tools, ``tools_hint`` must be empty.
    ``criterion_ids`` (the context's satisfiable-template surface, #313) is
    the criteria enum — the generation-time citation teacher: mis-citations
    are not refused at parse any more (the live walls outlived the repair
    rounds); ``derive`` drops them and records the drops, ``accept_plan``
    journals them, and the per-question coverage gate still refuses when a
    question loses its last substantive citation.
    ``require_hypotheses_stage`` (the context's ``require_hypotheses``) adds a
    description on ``tasks`` stating the ≥1 HYP-n contract as fact +
    consequence. Guidance only — the schema stays permissive (a HYP-less plan
    still parses, so the readable pin never becomes a second validator);
    ``assert_hyp_n_when_needed`` at accept_plan remains the enforcement point.
    A ``contains`` clause would make the array itself refuse, but provider
    support for ``contains`` in response_format schemas is not guaranteed
    (GLM compat) — the description is the safe visible form.
    ``route`` is always the closed ``PLANNER_TASK_ROUTES`` Literal (live
    st_1afbcc28: an invented route synonym slipped past the exact-match
    route-kind dispatch). Unlike ``tools_hint`` the set is context-free and
    always multi-member, so pydantic emits ``{"enum": [...]}`` directly.
    """
    # No satisfiable templates ⇒ no citable criteria at all (same closed-surface
    # rule as tools_hint with no tools); free-form criterion strings would only
    # defer the refusal to derive.
    criteria_type: Any = tuple[Literal[*criterion_ids], ...] if criterion_ids else tuple[str, ...]  # type: ignore[valid-type]
    criteria_field = (
        Field(default=(), description=_CRITERIA_CITATION_GUIDANCE)
        if criterion_ids
        else Field(default=(), max_length=0)
    )
    # Closed route vocabulary on every context-typed task model (PLANNER_TASK_
    # ROUTES is a fixed 3-value set, so the Literal serializes as {"enum": [...]}
    # natively — no single-const→enum normalization needed, unlike tools_hint).
    route_type: Any = Literal[*PLANNER_TASK_ROUTES]  # type: ignore[valid-type]
    route_field = Field(description=_ROUTE_VOCABULARY_GUIDANCE)

    task_base = create_model(
        "PlanIntentTaskCriteria",
        __base__=TaskIntent,
        route=(route_type, route_field),
        criteria=(criteria_type, criteria_field),
    )
    tasks_field: Any = Field(default=())
    if require_hypotheses_stage:
        tasks_field = Field(default=(), description=_HYP_STAGE_TASKS_GUIDANCE)
    if tool_ids:
        ids = tuple(sorted(set(tool_ids)))
        task_cls = create_model(
            "PlanIntentTaskTools",
            __base__=(_ToolsHintEnumJsonSchemaMixin, task_base),
            tools_hint=(tuple[Literal[*ids], ...], Field(default=())),  # type: ignore[valid-type]
        )
        return create_model(
            "PlanIntentTools",
            __base__=PlanIntent,
            tasks=(tuple[task_cls, ...], tasks_field),  # type: ignore[valid-type]
        )
    no_tools = create_model(
        "PlanIntentTaskNoTools",
        __base__=task_base,
        tools_hint=(tuple[str, ...], Field(default=(), max_length=0)),
    )
    return create_model(
        "PlanIntentNoTools",
        __base__=PlanIntent,
        tasks=(tuple[no_tools, ...], tasks_field),  # type: ignore[valid-type]
    )


_JSON_STRUCTURE_CHARS = "{}[]"


def _canonical_json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _required_recovery(raw: str, strict_exc: json.JSONDecodeError) -> str:
    """Canonical text from the fallback ladder, or the unchanged typed refusal."""
    recovered = _recovered_json_text(raw)
    if recovered is None:
        raise ValueError(f"value_json must be valid JSON text: {strict_exc}") from strict_exc
    return recovered


def _recovered_json_text(v: str) -> str | None:
    """Deterministic parse-or-refuse fallback ladder for ``value_json``.

    Rungs, in order:

    1. ``json.loads(strict=False)`` — tolerates raw control characters
       (newline/tab) inside JSON strings, the most common live GLM failure.
       It must run before the Python-literal parser, which would mangle JSON
       escape dialects.
    2. ``ast.literal_eval`` — a strict literal parser (no code execution, no
       invention) for the single-quote / True / False / None / trailing-comma
       dialect.
    3. Bare prose — a value that fails to parse AND carries no JSON structure
       characters (``{}[]``) is losslessly representable only as a string;
       the delivered value IS the text the model wrote (interpretation of
       intent, not data invention). Text that contains structure characters
       is truncated or malformed JSON — it still refuses so the typed repair
       path stays for anything ambiguous.

    Each successful rung canonicalizes exactly like the dict/list branch of
    ``_value_json_accepts_object``. None means every rung refused — a value
    is never silently coerced to a placeholder.
    """
    recovered: str | None
    try:
        recovered = _canonical_json_text(json.loads(v, strict=False))
    except ValueError:
        recovered = _python_literal_text(v)
    return recovered


def _python_literal_text(v: str) -> str | None:
    recovered: str | None
    try:
        recovered = _canonical_json_text(ast.literal_eval(v))
    except (ValueError, SyntaxError, TypeError):
        # Also covers literals with no JSON form (set/bytes/complex): they
        # fall through to the prose rung, which refuses structured text.
        recovered = _prose_json_text(v)
    return recovered


def _prose_json_text(v: str) -> str | None:
    if v.strip() == "":
        return None
    for ch in v:
        if ch in _JSON_STRUCTURE_CHARS:
            return None
    return _canonical_json_text(v)


class ToolArg(AgentModel):
    """Closed key/JSON-value pair (tool args and executor ``values``; one type)."""

    key: str = Field(min_length=1)
    value_json: str = Field(min_length=1)

    @field_validator("value_json", mode="before")
    @classmethod
    def _value_json_accepts_object(cls, v: object) -> object:
        """Providers cannot always nest escaped JSON inside a JSON string.

        Accepting the parsed value itself and canonicalizing to JSON text is
        lossless; the wire contract (``value_json`` str) is unchanged.Scalars
        too: a model that emits ``false``/``3``/``2.5`` as the JSON value
        means exactly that value (live: ``input_value=False, input_type=bool``).
        """
        if isinstance(v, (dict, list, bool, int, float)):
            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        return v

    @field_validator("value_json")
    @classmethod
    def _value_json_is_json(cls, v: str) -> str:
        try:
            json.loads(v)
        except json.JSONDecodeError as exc:
            v = _required_recovery(v, exc)
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
    # Registered user study inputs (``RegisteredInput``): study-level, wired to
    # every derived task exactly like core binds ``Study.user_input_ids`` to
    # every attempt — the executor consumes file data by CAS ref, not by
    # guessing paths from the prompt text.
    inputs: tuple[RegisteredInput, ...] = Field(default=())

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
    """Minimal executor structured output for M1 wiring (maps toward AgentTaskOutput).

    ``expansion_request`` is the PR9/H-g typed plan-expansion request: set only
    on replan work (core ``PLAN-n`` containers) whose result the core feeds
    through the deterministic critic gate before any ``PlanExpanded``.

    Required-nullable, no default: OpenAI strict structured outputs demand
    every property in ``required`` (optional = required + nullable
    ``anyOf[…, null]``); a defaulted field would leave the property out of
    ``required`` and the first live strict executor-route request would be
    rejected on the response schema. Constructors must therefore pass
    ``expansion_request`` explicitly (``None`` for ordinary attempts).
    """

    summary: str = Field(min_length=1)
    values: tuple[ToolArg, ...]
    tool_found: tuple[str, ...]
    expansion_request: ExpansionRequest | None


class AutomaticExecutorIn(AgentModel):
    """Model chooses calls within the independently authorized runtime surface."""

    mode: Literal["automatic"]
    request: str = Field(min_length=1)
    task: TaskSpecDraft
    study_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    inputs: tuple[RegisteredInput, ...] = Field(default=())


class RemoteCoderGenIn(AgentModel):
    """Generation payload for one model-authored remote coder task.

    Carries only journaled facts (study request, the task draft, its expected
    outputs, registered input NAMES — no CAS bytes and no file contents). The
    model authors the task text and maps every expected output to a sandbox
    workspace path (docs/remote-coder-runtime.md §2).
    """

    study_request: str = Field(min_length=1)
    task: TaskSpecDraft
    input_names: tuple[str, ...] = Field(default=())


class RemoteCoderOutputSpec(AgentModel):
    """One model-declared output: expected-output name → workspace path."""

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)

    @field_validator("name", "path", "media_type")
    @classmethod
    def _bare(cls, value: str) -> str:
        if value.strip() != value or any(ord(c) < 32 for c in value):
            raise ValueError("must be a non-empty bare string")
        return value

    @field_validator("path")
    @classmethod
    def _workspace_relative(cls, value: str) -> str:
        segments = value.split("/")  # audit-ok[str_split]: a ".." segment IS the traversal shape refused here
        if value.startswith("/") or "\\" in value or ".." in segments:
            raise ValueError("path must be sandbox-workspace-relative")
        return value


class RemoteCoderTaskSpec(AgentModel):
    """Model-authored remote coder task content (adapter generation output)."""

    task: str = Field(min_length=1)
    outputs: tuple[RemoteCoderOutputSpec, ...] = Field(min_length=1)

    @field_validator("task")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must be non-blank")
        return value

    @model_validator(mode="after")
    def _unique(self) -> RemoteCoderTaskSpec:
        names = [output.name for output in self.outputs]
        paths = [output.path for output in self.outputs]
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            raise ValueError("output names and paths must be unique")
        return self


def remote_coder_task_model_for(expected_output_names: tuple[str, ...]) -> type[RemoteCoderTaskSpec]:
    """Context-typed ``RemoteCoderTaskSpec`` request schema (pinned output names).

    ``outputs[].name`` becomes an enum of exactly the task's declared expected
    output names in the response_format JSON schema — the tools_hint
    Literal-enum precedent: "register exactly these outputs" is enforced by the
    schema itself, so the model cannot name an undeclared output (live
    st_idem_234ce9bd: it invented 8 output names the task never declared, the
    honest dispatch validator refused the invented set, and the study died in
    repair). Output paths/media types stay validator-checked; required-output
    coverage stays the caller's honest backstop.
    """
    if not expected_output_names:
        raise ValueError("expected_output_names must be non-empty (outputs have min_length=1)")
    names = tuple(sorted(set(expected_output_names)))
    name_type: Any = Literal[*names]  # type: ignore[valid-type]
    output_cls = create_model(
        "RemoteCoderOutputPinned",
        __base__=(_OutputNameEnumJsonSchemaMixin, RemoteCoderOutputSpec),
        name=(name_type, Field(min_length=1)),
    )
    return create_model(
        "RemoteCoderTaskPinned",
        __base__=RemoteCoderTaskSpec,
        outputs=(tuple[output_cls, ...], Field(min_length=1)),  # type: ignore[valid-type]
    )


class OutputSelection(AgentModel):
    output_name: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


class ToolAction(AgentModel):
    """One tool decision; finish output is validated against the role's schema."""

    action: Literal["call", "finish"]
    call: PlannedToolCall | None
    output_json: str | None
    selections: tuple[OutputSelection, ...]

    @model_validator(mode="after")
    def _exclusive_action(self) -> ToolAction:
        if self.action == "call":
            if self.call is None or self.output_json is not None or self.selections:
                raise ValueError("call action requires only call")
            keys = [arg.key for arg in self.call.args]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate tool argument keys")
        elif self.call is not None or self.output_json is None:
            raise ValueError("finish action requires output_json and no call")
        names = [selection.output_name for selection in self.selections]
        if len(names) != len(set(names)):
            raise ValueError("duplicate output selections")
        return self


class CriticIn(AgentModel):
    """critic input: the derived plan draft + the study context it came from.

    The context carries the request and the exact tool/input surface the plan
    was derived against (DESIGN §7.1: critic over ``PlanOut``); the output
    contract is ``coscientist.agents.critic.CriticVerdict``.
    """

    context: StudyContext
    plan: PlanOut


class ReviewerIn(AgentModel):
    """reviewer input: the post-close graph digest + the study context.

    ``digest_markdown`` is the compact ``build_graph_digest`` rendering of the
    closed research graph — the only facts the reviewer may speak to.
    """

    context: StudyContext
    digest_markdown: str = Field(min_length=1)


class ReviewerOut(AgentModel):
    """reviewer lane output: an assessment of the closed graph + its gaps.

    Advisory by construction: the lane renders it into a CAS artifact beside
    the digest and never writes journal events, so it cannot flip a verdict.
    """

    assessment: str = Field(min_length=1)
    gaps: tuple[Annotated[str, Field(min_length=1)], ...]


class ToolCallRef(AgentModel):
    tool_name: str = Field(min_length=1)
    resolution_status: ToolResolutionStatus
    args: tuple[ToolArg, ...]
    result_json: str | None
    call_id: str | None = None  # audit-ok[field_default]: old recipe receipts predate call IDs
    artifact_id: str | None = None  # audit-ok[field_default]: historical receipts are materialized by the record lane
    outcome: Literal["ok", "error"] = "ok"  # audit-ok[field_default]: old receipts represented success only


class AgentRun(AgentModel):
    """Result of ``AgentPort.run`` (DESIGN §7.2)."""

    output: BaseModel | None
    error: str | None
    usage: Usage
    tool_calls: tuple[ToolCallRef, ...]
    # None identifies recipes; automatic finish explicitly declares even an empty selection.
    output_selections: tuple[OutputSelection, ...] | None = None  # audit-ok[field_default]: recipe wire compatibility


class AgentError(Exception):
    """Typed agent refuse / configuration failure."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


# --- Wired optional roles (DESIGN §7.1 hypothesizer / researcher) -------------
# Additive block: new typed outputs for the HYP / LIT need stages. Kept at the
# end of the module so concurrent schema edits elsewhere do not conflict.


class HypothesisDraft(AgentModel):
    """One hypothesis row, v2 (DESIGN §2: id/statement/rank/prediction/selected).

    v2 adds the legacy-H1..H4 shape: a checkable numeric/structural
    ``prediction``, a competing-set ``rank``, the research ``question_ref``
    this hypothesis addresses, and the exactly-one ``selected`` flag the
    RC5/RC6 gates already require. Writing stays fail-closed v2; reading old
    v1 CAS rows goes through :func:`hypothesis_rows_v1` (display only).
    """

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    prediction: str = Field(min_length=1)
    rank: int
    question_ref: str = Field(min_length=1)
    selected: bool


class HypothesisRowsV1(AgentModel):
    """Legacy v1 hypothesis row (id/statement/rationale only) — read-compat.

    Never a plan input: RC5/RC6 gates keep refusing v1 rows (missing
    prediction/rank/selected). For display / diagnostics of old CAS artifacts.
    """

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


def hypothesis_rows_v1(raw: object) -> tuple[HypothesisRowsV1, ...] | None:
    """Recognize legacy v1 hypothesis rows in a HYP artifact ``values`` blob.

    Returns v1 rows only when the shape is exactly legacy v1 (rows carry
    id/statement/rationale and none carries a v2 field). Anything else —
    including v2 payloads and malformed blobs — returns ``None`` (callers
    fall back to strict v2 parsing, which stays fail-closed).
    """
    if not isinstance(raw, dict):
        return None
    rows = raw.get("hypotheses")
    if not isinstance(rows, list) or not rows:
        return None
    v2_fields = ("prediction", "rank", "question_ref", "selected")
    out: list[HypothesisRowsV1] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if any(f in row for f in v2_fields):
            return None
        try:
            out.append(HypothesisRowsV1.model_validate(row))
        except ValidationError:
            return None  # audit-ok[except_silent_return]: not-a-v1-shape → caller falls back to strict v2 parsing
    return tuple(out)


class HypothesisOut(AgentModel):
    """hypothesizer structured output for the wired HYP stage (DESIGN §7.1, v2).

    Floor of three and exactly one ``selected`` mirror the RC5 artifact gate;
    unique ranks keep the competing set a total order for foreach selection.
    """

    hypotheses: tuple[HypothesisDraft, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _unique_ids(self) -> HypothesisOut:
        ids = [h.id for h in self.hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("hypotheses ids must be unique (no duplicate references)")
        return self

    @model_validator(mode="after")
    def _unique_ranks(self) -> HypothesisOut:
        ranks = [h.rank for h in self.hypotheses]
        if len(ranks) != len(set(ranks)):
            raise ValueError("hypotheses ranks must be unique (total competing-set order)")
        return self

    @model_validator(mode="after")
    def _exactly_one_selected(self) -> HypothesisOut:
        selected = [h for h in self.hypotheses if h.selected]
        if len(selected) != 1:
            raise ValueError(
                f"exactly one hypothesis must be selected=True, got {len(selected)}"
            )
        return self


class PaperRef(AgentModel):
    """One paper ref from LIT tool outputs (evidence-style, tool-backed).

    ``identity_key`` is the explicit paper identity key (spec-lit SL6/SL7: no
    DOI/URL normalization heuristics). ``source_tool`` must be the exact
    journaled ``server:tool`` tool_call that surfaced this paper — the record
    path refuses papers that do not trace to a tool result (no invented
    citations).
    """

    title: str = Field(min_length=1)
    identity_key: str = Field(min_length=1)
    source_tool: str = Field(min_length=1)
    doi: str | None = None  # audit-ok[field_default]: optional metadata; identity gate is identity_key, never doi/url
    url: str | None = None  # audit-ok[field_default]: optional metadata; identity gate is identity_key, never doi/url


class LiteratureOut(AgentModel):
    """researcher structured output for the wired LIT stage (DESIGN §7.1)."""

    papers: tuple[PaperRef, ...]


# Receipt identity fields the live literature tools actually surface
# (papers-search:search_papers → article_id; paper-analysis → doi; the
# spec-lit canonical name). Only a fact-extraction aid — the binding gate
# itself stays substring containment, never a second path.
_IDENTITY_FIELDS = ("identity_key", "article_id", "doi")
_RECEIPT_FACTS_LIMIT = 20


def _identity_field_values(node: object, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _IDENTITY_FIELDS and isinstance(value, str) and value:
                found.append(value)
            else:
                _identity_field_values(value, found)
    elif isinstance(node, list):
        for item in node:
            _identity_field_values(item, found)
    elif isinstance(node, str):
        # MCP tool envelopes carry the payload as a JSON-encoded string
        # ({"result":{"content":[{"type":"text","text":"<json>"}]}}) — unwrap
        # any string that parses so nested identity fields surface.
        try:
            parsed = json.loads(node)
        except ValueError:
            return  # audit-ok[except_silent_return]: not JSON-encoded — an ordinary leaf, nothing to unwrap
        _identity_field_values(parsed, found)


def receipt_identity_keys(tool_results: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Identity facts per tool: string values of ``_IDENTITY_FIELDS`` parsed
    from the journaled ok result bodies. Best effort — non-JSON bodies
    contribute nothing (the gate is substring containment, unchanged)."""
    facts: dict[str, list[str]] = {}
    for tool_name, bodies in tool_results.items():
        values: list[str] = []
        for body in bodies:
            # Best-effort facts: a non-JSON receipt body contributes nothing —
            # the binding gate is substring containment, never parse-dependent.
            try:
                parsed = json.loads(body)
            except ValueError:
                continue  # audit-ok[except_silent_continue]: best-effort facts; gate is substring containment
            _identity_field_values(parsed, values)
        facts[tool_name] = values[:_RECEIPT_FACTS_LIMIT]
    return facts


def literature_binding_error(
    out: LiteratureOut, *, tool_results: Mapping[str, Sequence[str]]
) -> str | None:
    """Refuse detail for papers that do not trace to journaled tool results,
    or None when every paper binds. One gate, two consumers: the automatic
    loop feeds it back to the SAME agent as a repair message (owner law, bor7
    st_1a17e98a04 LIT#1: a final-output validation error must not fall the
    run), and ``journal.record_literature`` keeps it as the final fail-closed
    refuse. The detail carries receipt facts (identity fields the cited tools
    actually surfaced + the cited keys) so the repair round can fix the
    output targeted."""
    problems: list[str] = []
    for i, paper in enumerate(out.papers):
        bodies = tool_results.get(paper.source_tool)
        if not bodies:
            problems.append(
                f"paper[{i}] cites {paper.source_tool!r} but no journaled tool result "
                f"carries that name (tools={sorted(tool_results)!r}) — no invented citations"
            )
            continue
        if not any(paper.identity_key in body for body in bodies):
            problems.append(
                f"paper[{i}] identity_key not found in the {paper.source_tool!r} result — "
                "papers only from tool outputs (no invented citations)"
            )
    if not problems:
        return None
    return (
        "; ".join(problems)
        + " Receipt facts: identity fields actually surfaced by journaled tool results: "
        + json.dumps(receipt_identity_keys(tool_results), ensure_ascii=False)
        + "; cited identity_keys: "
        + json.dumps([paper.identity_key for paper in out.papers], ensure_ascii=False)
        + "."
    )
