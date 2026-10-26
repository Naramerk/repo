"""Typed role I/O for planner and executor-route (DESIGN §7.1 / §7.2)."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from coscientist.core.execution.domain import Need
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


class StudyContext(AgentModel):
    """Planner / executor shared study snapshot (no journal state)."""

    study_id: str = Field(min_length=1)
    request: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    # Exact check-catalog template ids the planner may reference in task
    # ``criteria`` (empty tuple: criteria must be empty). Passed explicitly so
    # the planner never has to guess valid ids.
    criterion_template_ids: tuple[str, ...] = Field(default=())
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


class PlanOut(AgentModel):
    """Planner structured output (DESIGN §7.1)."""

    tasks: tuple[TaskSpecDraft, ...]
    questions: tuple[QuestionDraft, ...]
    hypotheses: Need
    literature: Need


class TaskIntent(AgentModel):
    """Planner typed intent for one task (frozen-plan internals are derived).

    Question references are 1-based positions, not model-invented core ids.
    Criteria reference catalogue templates; frozen check objects stay core-owned.
    """

    goal: str = Field(min_length=1)
    route: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    tools_hint: tuple[str, ...] = ()
    question_indices: tuple[int, ...] = ()
    criteria: tuple[str, ...] = ()
    # Optional planner slug; system assigns ``t{N}`` (1-based) when unset.
    key: str | None = Field(default=None)
    title: str | None = Field(default=None)


class QuestionIntent(AgentModel):
    """Question meaning and requested answer form, preserved by derivation."""

    text: str = Field(min_length=1)
    final_type: Literal["verdict", "artifact", "both"]


class PlanIntent(AgentModel):
    """Minimal planner structured output (typed intent, not a frozen plan).

    Core assigns ids/output names and freezes selected catalogue checks.
    It never guesses scientific acceptance or question coverage.
    """

    tasks: tuple[TaskIntent, ...]
    questions: tuple[QuestionIntent, ...]
    hypotheses: Need
    literature: Need


class _ToolsHintEnumJsonSchemaMixin(AgentModel):
    """Normalize a single-id ``tools_hint`` schema to ``enum`` (not ``const``).

    ``Literal[one_id]`` serializes as ``{"const": id}``; some providers ignore
    ``const`` in response_format schemas. Review finding (LOW): always emit
    ``{"enum": [id]}``.
    """

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> dict[str, Any]:
        js = handler(schema)
        props = js.get("properties")
        hint = props.get("tools_hint") if isinstance(props, dict) else None
        if isinstance(hint, dict):
            items = hint.get("items")
            if isinstance(items, dict) and "const" in items:
                hint["items"] = {"enum": [items["const"]], "type": "string"}
        return js


def plan_intent_model_for(tool_ids: tuple[str, ...], criterion_ids: tuple[str, ...] = ()) -> type[PlanIntent]:
    """Context-typed ``PlanIntent`` request schema (schema-checked tools_hint).

    When the study surface has tool ids, ``tools_hint`` becomes an enum of
    exactly those ids in the response_format JSON schema — the "tools_hint
    must be ids from the context's tool_ids" rule is enforced by the schema
    itself (no prompt sentence). With no tools, ``tools_hint`` must be empty.
    """
    criteria_type: Any = tuple[Literal[*criterion_ids], ...] if criterion_ids else tuple[str, ...]  # type: ignore[valid-type]
    task_base = create_model(
        "PlanIntentTaskCriteria",
        __base__=TaskIntent,
        criteria=(criteria_type, Field(default=())),
    )
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
            tasks=(tuple[task_cls, ...], Field(default=())),  # type: ignore[valid-type]
        )
    no_tools = create_model(
        "PlanIntentTaskNoTools",
        __base__=task_base,
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
    """Minimal executor structured output for M1 wiring (maps toward AgentTaskOutput)."""

    summary: str = Field(min_length=1)
    values: tuple[ToolArg, ...]
    tool_found: tuple[str, ...]


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


# --- Wired optional roles (DESIGN §7.1 hypothesizer / researcher) -------------
# Additive block: new typed outputs for the HYP / LIT need stages. Kept at the
# end of the module so concurrent schema edits elsewhere do not conflict.


class HypothesisDraft(AgentModel):
    """One hypothesis statement + rationale (DESIGN §7.1 HypothesesOut rows)."""

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class HypothesisOut(AgentModel):
    """hypothesizer structured output for the wired HYP stage (DESIGN §7.1)."""

    hypotheses: tuple[HypothesisDraft, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> HypothesisOut:
        ids = [h.id for h in self.hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("hypotheses ids must be unique (no duplicate references)")
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
