"""Execution domain models (DESIGN §2, §3.1). Graph ontology lives in graph.py + registry."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.claims.rubric_types import FinalType, Rubric
from coscientist.core.plan.decomposes import DecomposeOrigin, RollupRule


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskKind(StrEnum):
    """Stages of the execution DAG — not the scientific research ontology."""

    SPEC = "spec"
    HYPOTHESES = "hypotheses"
    LITERATURE = "literature"
    PLAN = "plan"
    EXPERIMENT = "experiment"
    REPORT = "report"


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"


class TaskOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


OUTCOME_RANK = {Outcome.FAILED: 0, Outcome.PARTIAL: 1, Outcome.SUCCESS: 2}


class ReqStatus(StrEnum):
    PENDING = "PENDING"
    MET = "MET"
    FAILED = "FAILED"
    WAIVED = "WAIVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNASSESSED = "UNASSESSED"
    MIGRATION_ERROR = "MIGRATION_ERROR"
    STALE = "STALE"


Provenance = Literal["real", "partial", "stub"]
VALID_PROVENANCE: frozenset[str] = frozenset({"real", "partial", "stub"})
PROVENANCE_RANK: dict[str, int] = {"real": 0, "partial": 1, "stub": 2}


def worst_provenance(*values: Provenance) -> Provenance:
    """Least favorable under real < partial < stub. Empty → stub."""
    if not values:
        return "stub"
    return max(values, key=lambda p: PROVENANCE_RANK[p])


OnTimeout = Literal["delegate", "delegate_capped", "fail"]
HitlGateKind = Literal[
    "spec_approval",
    "plan",
    "plan_approval",
    "plan_expansion",
    "hypotheses",
    "costly_or_nonreal",
    "report",
    "result_acceptance",
    "skip_task",
    "budget",  # M1 PR12: budget_share checkpoint («реши сам» → critic)
    "side_effect",  # PR18: write/external_effect tools (DESIGN_M1 §8)
    "proposed_check",  # M1 PR20: approve agent-proposed catalog meta-check
]


class GateConfig(Frozen):
    """Per-gate HITL config (DESIGN §7.3 / RESEARCH_MODEL §5.2)."""

    enabled: bool = True
    timeout_s: int = 1800
    # costly_or_nonreal defaults to fail; others to delegate (via HitlPolicy.gate).
    on_timeout: OnTimeout = "delegate"


def _default_gates() -> dict[str, GateConfig]:
    return {
        "spec_approval": GateConfig(timeout_s=120, on_timeout="delegate"),
        "plan": GateConfig(timeout_s=180, on_timeout="delegate"),
        "plan_approval": GateConfig(timeout_s=180, on_timeout="delegate"),
        "plan_expansion": GateConfig(timeout_s=180, on_timeout="delegate"),
        "hypotheses": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "costly_or_nonreal": GateConfig(timeout_s=1800, on_timeout="fail"),
        "report": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "result_acceptance": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "skip_task": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "budget": GateConfig(timeout_s=1800, on_timeout="delegate"),
        # Headless default decision is deny (HitlPolicy.defaults); gate itself stays enabled.
        "side_effect": GateConfig(timeout_s=1800, on_timeout="fail"),
        # PR20: HITL timeout ≡ reject (DESIGN_M1 §16); never auto-approve.
        "proposed_check": GateConfig(timeout_s=1800, on_timeout="fail"),
    }


class HitlPolicy(Frozen):
    mode: Literal["interactive", "auto", "headless"] = "headless"
    gates: dict[str, GateConfig] = Field(default_factory=_default_gates)
    # Legacy flat map kept for callers/tests; gate() prefers gates then these.
    timeout_s: dict[str, int] = Field(
        default_factory=lambda: {
            "spec_approval": 120,
            "plan_approval": 180,
            "result_acceptance": 1800,
            "skip_task": 1800,
        }
    )
    # Policy-level default for gates not listed; costly_or_nonreal still defaults to fail.
    on_timeout: OnTimeout = "delegate"
    defaults: dict[str, str] = Field(
        default_factory=lambda: {
            "plan_approval": "human_timeout",
            "result_acceptance": "accept_computed",
            "skip_task": "deny",
            # K4/K5: write|external_effect tools require an explicit allow (DESIGN_M1 §8).
            "side_effect": "deny",
            # PR20: headless / timeout → reject proposed check (never counts).
            "proposed_check": "reject",
        }
    )

    def gate(self, kind: str) -> GateConfig:
        if kind in self.gates:
            return self.gates[kind]
        alias = {"plan": "plan_approval", "plan_approval": "plan"}.get(kind)
        if alias and alias in self.gates:
            return self.gates[alias]
        default_ot: OnTimeout = "fail" if kind == "costly_or_nonreal" else self.on_timeout
        return GateConfig(
            enabled=True,
            timeout_s=self.timeout_s.get(kind, 1800),
            on_timeout=default_ot,
        )


ArtifactRole = Literal["paper", "data", "code", "figure", "table", "log", "report"]
# M1 PR15: subtype on report artifacts (core-stamped from OutputSpec.kind).
ArtifactKind = Literal["experiment_design", "contradiction_map"]
# M1 PR15 §6.6: explicit claim evidence kind on OutputSpec (never inferred from rubric ids).
EvidenceKind = Literal[
    "computational",
    "experimental",
    "literature",
    "expert",
    "derivation",
]
# Who stored the artifact bytes. effect.trusted is a reporting tag (M1 verdicts
# gate on countable). Sandbox puts are trusted only when both net and fs
# isolation are hard (soft/none ⇒ trusted=False). ``tests_pass`` is
# observation-only (countable=False); other executable kinds stay countable.
# ``tool`` = is_fetch tool via FetchCapability (SourceFetched); not agent put / sandbox.
# ``user`` = POST /studies inputs[] task data (DESIGN_M1 §9 / PR19) — never a
# quote_in_source source (PR14 ``put_fetched`` / SourceFetched only).
ArtifactProducer = Literal["agent", "core", "sandbox", "tool", "user"]
NetIsolation = Literal["hard", "soft", "none"]
FsIsolation = Literal["hard", "soft", "none"]


class OutputSpec(Frozen):
    name: str
    media_type: str
    schema_ref: str | None = None
    required: bool = True
    # Plan-accepted role (core-owned after accept_plan); independence code-vs-data.
    role: ArtifactRole | None = None
    # M1 PR13: qualitative acceptance (literature review, design, …). Frozen at PLAN.
    rubric: Rubric | None = None
    # M1 PR15: explicit deliverable kind (PlanValidator-enforced; not inferred).
    kind: ArtifactKind | None = None
    # M1 PR15 §6.6: explicit evidence_kind (PlanValidator; never inferred from rubric ids).
    evidence_kind: EvidenceKind | None = None


class InputSpec(Frozen):
    """Dependency input (DESIGN §3.4 + M1 PR7 scientific kinds).

    ``kind=artifact`` (default): ``from_task`` + ``output`` resolve an artifact.
    ``kind=claim``: ``ref`` is a claim node id; core adds ``uses_claim`` at P2.
    ``kind=verdict``: ``ref`` is a hypothesis/question id; P2 binds a persisted
    verdict snapshot digest (runtime ``verdict()``, M1 PR 8).

    ``quorum`` (M1 PR10 fan-in): task starts when ≥ quorum inputs bind as
    real/partial; unbound / failed / stub inputs become experiment limitations.
    ``None`` = require every input (legacy). ``"all"`` = same as len(inputs).
    """

    from_task: str = ""
    output: str = ""
    schema_ref: str | None = None
    # M1 PR5: default forbid (was use_partial). Old journals restored via upcaster.
    mode: Literal["use_partial", "simulate", "forbid"] = "forbid"
    kind: Literal["artifact", "claim", "verdict"] = "artifact"
    ref: str | None = None
    quorum: int | Literal["all"] | None = None


class ForEachSpec(Frozen):
    """Expand one template task into N tasks at source DONE (M1 PR10 / S9).

    Example: ``selected hypotheses of HYP-0`` → kind=hypothesis, of_task=HYP-0,
    selector=selected. Core assigns EXP-<n>; no planner; no expansion quota.
    """

    kind: Literal["hypothesis", "question"]
    of_task: str
    selector: Literal["selected", "all"] = "selected"


class Limitation(Frozen):
    """experiment.limitations[] entry (fan-in quorum gaps, partial inputs)."""

    text: str
    ref: str | None = None


CheckRole = Literal["prediction", "validity", "unknown"]


class RequirementRef(Frozen):
    """Plan criterion reference.

    ``role`` is optional plan-side annotation only. The authoritative role comes
    from the catalog ``RequirementTemplate``. If ``role`` is set and disagrees
    with the template, ``accept_plan`` rejects the plan. Runtime evaluation
    never trusts this field over the catalog.
    """

    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    role: CheckRole | None = None


class QuestionSpec(Frozen):
    """Research question carried in PlanAccepted (from SPEC output)."""

    id: str
    text: str
    # Optional parent for decomposes / max_depth (DESIGN_M1 §10).
    parent_id: str | None = None
    # M1 PR16: decomposes edge attrs.origin (explicit|implicit|emergent).
    origin: DecomposeOrigin | None = None
    # M1 PR13: verdict (claims) | artifact (rubric) | both (DESIGN_M1 §6.1).
    final_type: FinalType = "verdict"


class ExperimentMethod(StrEnum):
    """Matches graph_schema experiment.method (simulation = scientific, not stub)."""

    COMPUTATIONAL = "computational"
    STATISTICAL = "statistical"
    LABORATORY = "laboratory"
    LITERATURE_SEARCH = "literature_search"
    EXPERT = "expert"
    SIMULATION = "simulation"


def _strict_real(v: object, *, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a real number, got {type(v).__name__}")
    fv = float(v)
    if not math.isfinite(fv) or fv <= 0.0:
        raise ValueError(f"{name} must be finite and > 0, got {v!r}")
    return fv


def _strict_pos_int(v: object, *, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{name} must be int (not bool/str), got {type(v).__name__}")
    if v <= 0:
        raise ValueError(f"{name} must be > 0, got {v!r}")
    return v


class FrozenCheckLimits(Frozen):
    """One catalog-frozen sandbox row (``TaskSpec.sandbox_check_limits[template_id]``).

    Every key is required; ``None`` only for budgets the check does not carry
    (non-sandbox kinds). Both security toggles are always explicit bools.
    """

    timeout_s: float | None
    memory_mb: int | None
    max_output_bytes: int | None
    pids_max: int | None
    rlimit_nproc: int | None
    reap_timeout_s: float | None
    output_drain_join_s: float | None
    allow_network: bool
    allow_cgroup_degraded: bool

    @field_validator("timeout_s", mode="before")
    @classmethod
    def _opt_seconds(cls, v: object) -> float | None:
        return None if v is None else _strict_real(v, name="timeout_s")

    @field_validator("reap_timeout_s", "output_drain_join_s", mode="before")
    @classmethod
    def _opt_host_seconds(cls, v: object) -> float | None:
        return None if v is None else _strict_real(v, name="host budget")

    @field_validator(
        "memory_mb", "max_output_bytes", "pids_max", "rlimit_nproc", mode="before"
    )
    @classmethod
    def _opt_positive_int(cls, v: object) -> int | None:
        return None if v is None else _strict_pos_int(v, name="limit")

    @field_validator("allow_network", "allow_cgroup_degraded", mode="before")
    @classmethod
    def _toggle_bool(cls, v: object) -> object:
        if not isinstance(v, bool):
            raise ValueError(
                f"security toggle must be bool, got {type(v).__name__}"
            )
        return v


class TaskSpec(Frozen):
    task_key: str
    title: str
    goal: str
    route: str
    required: bool = True
    tools_hint: list[str] = Field(default_factory=list)
    expected_outputs: list[OutputSpec] = Field(default_factory=list)
    criteria: list[RequirementRef] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[InputSpec] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    # Plan-side tests.intent (graph edge materializes later); PENDING refute protection.
    tests_intent: Literal["confirm", "refute"] | None = None
    # H-c: allow start when a depends_on parent is FAILED/SKIPPED (no InputSpec).
    tolerate_failed_dep: bool = False
    # Scientific method; simulation does not upgrade stub/partial input provenance.
    method: ExperimentMethod | None = None
    # M1 PR10: template expanded at of_task DONE (selector → N concrete tasks).
    foreach: ForEachSpec | None = None
    # Bound hypothesis/question id after foreach materialization (None on template).
    foreach_item: str | None = None
    # M1 PR11: concrete loop body task (None on non-loop / template keys in LoopSpec.body).
    loop_id: str | None = None
    loop_iter: int | None = None
    # Original body template key (for depends remapping / reporting).
    loop_template_key: str | None = None
    # Deprecated aggregate slots — always stripped at freeze (catalog is sole source).
    sandbox_timeout_s: float | None = None
    sandbox_memory_mb: int | None = None
    # Per-check limits frozen from catalog at TaskCreated (template_id → typed row).
    sandbox_check_limits: dict[str, FrozenCheckLimits] | None = None
    # Audit when agent-provided limit fields were ignored.
    sandbox_limits_note: str | None = None
    # M1 PR12: fractions of study budget / wall time (None = equal remainder).
    budget_share: float | None = Field(default=None, ge=0.0, le=1.0)
    deadline_share: float | None = Field(default=None, ge=0.0, le=1.0)


class LoopPredicate(Frozen):
    """Until-predicate for ``LoopSpec`` (M1 PR11 / S10).

    Built-ins: ``survivors_le``, ``criterion_met``, ``no_change``. Plugins register
    via ``coscientist.core.plan.loop_predicates.register_loop_predicate``.
    """

    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class LoopSpec(Frozen):
    """Bounded plan loop: core materializes body iters until ``until`` or max_iter.

    ``max_iter`` is mandatory (no default). PlanValidator rejects missing /
    ``max_iter > Study.max_loop_iter``. DAG stays acyclic: each iter gets fresh
    ``EXP-<n>`` keys with ``loop_id`` / ``loop_iter``. Cap → ``LoopCapped``, not
    study ``terminal_reason``.
    """

    loop_id: str
    body: list[TaskSpec]
    until: LoopPredicate
    max_iter: int = Field(ge=1)
    scope_hypotheses: list[str] = Field(default_factory=list)
    scope_questions: list[str] = Field(default_factory=list)
    # When empty, iter 1 starts after accept_plan (same tick as ready foreach).
    depends_on: list[str] = Field(default_factory=list)


class Budget(Frozen):
    max_llm_calls: int = 100
    max_tokens: int = 1_000_000
    max_cost_rub: Decimal | None = None
    max_tool_calls: int = 500
    deadline: datetime | None = None
    # RUB cap for critic decisions on costly_or_nonreal (delegate_capped).
    delegated_costly: Decimal | None = None


def validate_budget_for_create(budget: Budget | None) -> None:
    """Reject new studies where ``delegated_costly > max_cost_rub``.

    Enforced at ``create_study`` only — not on ``Budget`` / ``Study.model_validate`` —
    so replaying an old ``StudyCreated`` with a looser ratio cannot raise and block
    ``StudyFinalized``.
    """
    if budget is None:
        return
    if (
        budget.delegated_costly is not None
        and budget.max_cost_rub is not None
        and budget.delegated_costly > budget.max_cost_rub
    ):
        raise ValueError("delegated_costly must be <= max_cost_rub")


class Study(Frozen):
    study_id: str
    profile: str
    request: str
    catalog_version: StrictInt
    hitl_policy: HitlPolicy
    deadline: datetime
    created_at: datetime
    parent_study_id: str | None = None
    budget: Budget | None = None
    finalize_reserve_s: int = Field(default=30, ge=0)
    # PR 6 / DESIGN_M1 §3: fixed at create (like catalog_version); change only via fork.
    # PR8: confirmations_needed is snapshot ``k`` (digest / bind); not the severe gate.
    confirmations_needed: int = Field(default=1, ge=1)
    refutations_needed: int = Field(default=1, ge=1)  # k_ref
    refutation_priority: bool = False
    # PR8b: severe-support threshold (|con| ≥ supported_needed with capable refute).
    supported_needed: int = Field(default=2, ge=2)
    # M1: count only; e-value aggregation deferred (rejected at StudyCreated).
    aggregate: Literal["count"] = "count"
    evalue_alpha: float = Field(default=0.05, gt=0.0, le=1.0)
    # PR9: plan expansion quotas + replan time reserve (DESIGN_M1 §4–5).
    # max_expansions replaces profile max_replans (migration: value = max_replans).
    max_expansions: int = Field(default=2, ge=0)
    max_human_expansions: int = Field(default=10, ge=0)
    replan_reserve: float = Field(default=0.1, ge=0.0, le=1.0)
    max_tasks: int = Field(default=64, ge=1)
    max_depth: int = Field(default=8, ge=1)  # decomposes depth (DESIGN_M1 §4 / §10)
    plan_timeout_s: int = Field(default=600, ge=1)
    # PR10: cap foreach children; excess → report limitation (sorted order kept).
    max_foreach_children: int = Field(default=32, ge=1)
    # PR11: hard ceiling for LoopSpec.max_iter (PlanValidator enforces ≤ this).
    max_loop_iter: int = Field(default=8, ge=1)
    # PR12: plan critic — no single task may claim more than this budget_share.
    max_task_budget_share: float = Field(default=1.0, ge=0.0, le=1.0)
    # PR19 / DESIGN_M1 §9: time-sensitive questions; shown in the report when set.
    as_of: datetime | None = None
    # Task-data artifact ids from inputs[] before SPEC (role=data, producer=user).
    # Not quote_in_source sources (PR14 SourceFetched / put_fetched only).
    user_input_ids: list[str] = Field(default_factory=list)
    # PR20: max agent CheckProposed per study (default 3); excess → proposal_quota.
    max_proposals: int = Field(default=3, ge=0)

    def effective_deadline(self) -> datetime:
        """Last instant usable by tasks/attempts/HITL (deadline − finalize reserve)."""
        return self.deadline - timedelta(seconds=self.finalize_reserve_s)

    def replan_budget_s(self) -> float:
        """Total seconds reserved for PLAN-n / expansions (fixed at create)."""
        span = (self.effective_deadline() - self.created_at).total_seconds()
        return max(0.0, float(self.replan_reserve) * span)


class Need(Frozen):
    """Planner Need (DESIGN §2): reason required; strict bool/str (no coerce)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    # No default: dict without kind must fail; construct with kind="need" or parse_plan_need.
    kind: Literal["need"]
    needed: bool
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _nonempty_reason(self) -> Need:
        if not self.reason.strip():
            raise ValueError("Need.reason must be non-empty")
        return self


class NeedUnrecorded(Frozen):
    """Legacy PlanAccepted had no Need field — not a planner decision."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    # No default: dict without kind must fail; construct with kind="unrecorded" or parse_plan_need.
    kind: Literal["unrecorded"]
    source_schema_version: StrictInt


class Plan(Frozen):
    plan_id: str
    study_id: str
    revision: int
    hypothesis_id: str | None
    tasks: list[TaskSpec]
    questions: list[QuestionSpec] = Field(default_factory=list)
    # H-g: task_keys that trigger planner after TaskDone (PlanOut.expand_after).
    expand_after: list[str] = Field(default_factory=list)
    # M1 PR11 / S10: bounded loops (body not in tasks[]; core materializes iters).
    loops: list[LoopSpec] = Field(default_factory=list)
    # M1 PR16 / PlanOut: named subquestions (parent_id required) + rollup rule.
    subquestions: list[QuestionSpec] = Field(default_factory=list)
    rollup: RollupRule = "all"
    # G4: planner Need, or NeedUnrecorded when legacy journal omitted the field.
    hypotheses: Annotated[Need | NeedUnrecorded, Field(discriminator="kind")]
    literature: Annotated[Need | NeedUnrecorded, Field(discriminator="kind")]


class Task(Frozen):
    task_id: str
    kind: TaskKind
    spec: TaskSpec
    state: TaskState
    outcome: TaskOutcome | None = None
    reason: TaskReason | None = None
    deadline: datetime
    attempts: list[str] = Field(default_factory=list)
    inherited_from: str | None = None


class InputBinding(Frozen):
    input: str
    artifact_id: str = ""
    source: Provenance
    kind: Literal["artifact", "claim", "verdict"] = "artifact"
    ref: str | None = None
    # P2 content snapshot (DESIGN_M1 §9): artifact CAS sha256 / verdict JCS / claim meta.
    digest: str | None = None
    # Formula version for ``inputs_digest``; None/absent = legacy (drift unknown).
    digest_version: int | None = None
    retrieved_at: datetime | None = None
    # Client / retrieval URI — never the local CAS ``file://`` path.
    source_uri: str | None = None


class ToolCallRef(Frozen):
    call_id: str
    tool: str
    args_digest: str
    started_at: datetime
    ended_at: datetime | None = None


class Attempt(Frozen):
    attempt_id: str
    n: int
    dispatch_id: str
    route_returned: bool = False
    closed: bool = False
    close_reason: CloseReason | None = None
    provenance: Provenance = "real"
    agent_output_digest: str | None = None
    deadline: datetime
    task_id: str
    study_id: str


class EvidenceRef(Frozen):
    artifact_id: str
    json_pointer: str | None = None
    digest: str


class SourceQuote(Frozen):
    """Literature evidence locator (DESIGN §2 / DESIGN_M1 §6.3).

    ``quote_in_source`` matches ``quote`` in the CAS text of ``artifact_id``
    after normalization. ``translation`` is never checked. OCR text is a
    separate text artifact (own sha256); callers set attempt provenance=partial.

    ``source_sha256`` is required and must equal a journaled ``SourceFetched``
    digest (and the artifact's CAS sha256). Agent ``put`` / ``put_sandbox``
    sources are rejected.
    """

    artifact_id: str  # artifact(role=paper) or OCR text artifact
    locator: str  # page / section, e.g. "p.6" / "§3.2"
    quote: str
    digest: str  # sha256(JCS(quote))
    source_sha256: str  # required; must match SourceFetched + artifact.sha256
    lang: str | None = None
    normalization_version: str | None = None
    translation: str | None = None  # not checked by quote_in_source


class Downgrade(Frozen):
    from_: Outcome = Field(alias="from_")
    to: Outcome
    reason: str

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class DowngradeClaim(Frozen):
    to: Outcome
    refs: list[str | EvidenceRef]
    reason: str = ""


class CheckResult(Frozen):
    check_id: str
    template_id: str
    status: ReqStatus
    is_code: bool
    required: bool
    role: CheckRole  # from catalog template; never defaulted to validity
    evidence: list[EvidenceRef] = Field(default_factory=list)
    detail: str | None = None
    # Core-only numeric effect from evaluation (never agent claim attrs / free detail).
    effect: dict[str, Any] | None = None
    used_sesoi: bool = False


class Result(Frozen):
    result_id: str
    attempt_id: str
    outcome: Outcome
    inputs: list[InputBinding] = Field(default_factory=list)
    provenance: Provenance = "real"
    agent_claim: Outcome | None = None
    downgrade: Downgrade | None = None
    computed_outcome: Outcome
    artifacts: list[str] = Field(default_factory=list)
    check_results: list[CheckResult] = Field(default_factory=list)
    unassessed: list[str] = Field(default_factory=list)
    payload_digest: str
    reason: str | None = None


class Artifact(Frozen):
    artifact_id: str
    name: str
    media_type: str
    sha256: str
    size: int
    uri: str
    produced_by: ToolCallRef
    schema_ref: str | None = None
    role: ArtifactRole | None = None
    # M1 PR15: subtype stamped by core from OutputSpec.kind
    # (experiment_design | contradiction_map); not agent-settable.
    kind: ArtifactKind | None = None
    # Core-stamped; agent ``put`` always ``agent``. Never settable by AgentTaskOutput.
    producer: ArtifactProducer = "agent"
    # Sandbox only: isolation actually achieved (PR17). trusted only if both hard.
    net_isolation: NetIsolation | None = None
    fs_isolation: FsIsolation | None = None
    # PR18 / K5: web/tool-output artifacts are tainted; blocks write tools in context.
    tainted: bool = False


class Clarification(Frozen):
    target: str | None
    text: str


class HitlRequest(Frozen):
    request_id: str
    kind: HitlGateKind
    payload_ref: str
    deadline: datetime
    study_id: str
    task_id: str
    open: bool = True
    cost_rub: float = 0.0
    # Bound after_task / PLAN-n key: when set on plan/plan_approval/plan_expansion,
    # a HitlDecided plan change may produce PlanExpanded / ExpansionRefused.
    expand_after_task: str | None = None


class HitlDecision(Frozen):
    request_id: str
    decision: Literal["accept", "revise", "reject", "delegate"]
    by: Literal["human", "policy"]
    reason: str
    approved_by: Literal["human", "critic"] | None = None
    delegated_by: Literal["human", "timeout"] | None = None
    spent_rub: float = 0.0


class RuleRef(Frozen):
    rule_id: str
    source: str
    excerpt: str
    digest: str


class CodeCheck(Frozen):
    kind: Literal[
        "artifact_exists",
        "schema_valid",
        "json_pointer",
        "source_link",
        "metric_threshold",
        "value_matches",
        # M1 PR14 (DESIGN_M1 §6.3 / K9)
        "quote_in_source",
        # M1 PR17 executable kinds (DESIGN_M1 §7)
        "exec_runs",
        "tests_pass",
        "metric_on_holdout",
        "reproducible_rerun",
        # M1 PR13 qualitative rubric (DESIGN_M1 §6.2)
        "rubric",
    ]
    output: str | None = None
    schema_ref: str | None = None
    pointer: str | None = None
    metric: str | None = None
    op: Literal["<", "<=", "==", ">=", ">", "in"] | None = None
    target: Any = None
    tolerance: float | None = None
    # exec_runs / reproducible_rerun
    command: str | None = None
    argv: list[str] | None = None
    timeout_s: float | None = None
    memory_mb: int | None = None
    max_output_bytes: int | None = None
    # cgroup pids.max + wrapper RLIMIT_NPROC (required by limits_from_check).
    pids_max: int | None = None
    rlimit_nproc: int | None = None
    # Required bools — no defaults; ``mode="before"`` rejects str/int.
    allow_network: bool
    # Opt-in: continue when cgroup limits cannot be applied (else fail-closed).
    allow_cgroup_degraded: bool
    # Host post-exit budgets — required by the catalog loader for executable kinds.
    reap_timeout_s: float | None = None
    output_drain_join_s: float | None = None
    # tests_pass — catalog/task tests (not agent tests), mounted RO into verifier
    # Required when kind=tests_pass (model_validator); no silent default.
    min_tests: int | None = None
    tests: dict[str, str] | None = None  # relative path → source text
    # Permitted agent input filenames (allowlist; empty → mount nothing).
    input_allowlist: list[str] | None = None
    # metric_on_holdout — labels from catalog/task store only (never agent slots)
    holdout: dict[str, Any] | None = None
    holdout_digest: str | None = None  # sha256 of canonical_json(holdout)
    holdout_output: str | None = None  # legacy; migrated away in load_catalog
    # reproducible_rerun — required when kind=reproducible_rerun (sole source).
    rerun_tol: float | None = None
    # kind=rubric: items frozen in catalog or filled from OutputSpec.rubric at eval
    rubric: Rubric | None = None

    @field_validator(
        "reap_timeout_s",
        "output_drain_join_s",
        "timeout_s",
        mode="before",
    )
    @classmethod
    def _host_budget_seconds(cls, v: object) -> float | None:
        if v is None:
            return None
        # bool is a subclass of int — reject before numeric coercion.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(
                f"host budget must be a real number, got {type(v).__name__}"
            )
        fv = float(v)
        if not math.isfinite(fv) or fv <= 0.0:
            raise ValueError(f"host budget must be finite and > 0, got {v!r}")
        return fv

    @field_validator("rerun_tol", mode="before")
    @classmethod
    def _rerun_tol_non_negative(cls, v: object) -> float | None:
        """Always reject negative ``rerun_tol`` (any kind); None stays optional."""
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(
                f"rerun_tol must be a real number, got {type(v).__name__}"
            )
        fv = float(v)
        if not math.isfinite(fv) or fv < 0.0:
            raise ValueError(f"rerun_tol must be finite and >= 0, got {v!r}")
        return fv

    @field_validator(
        "pids_max",
        "rlimit_nproc",
        "memory_mb",
        "max_output_bytes",
        "min_tests",
        mode="before",
    )
    @classmethod
    def _positive_int_limits(cls, v: object) -> int | None:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(
                f"limit must be int (not bool/str), got {type(v).__name__}"
            )
        if v <= 0:
            raise ValueError(f"limit must be > 0, got {v!r}")
        return v

    @field_validator("allow_network", "allow_cgroup_degraded", mode="before")
    @classmethod
    def _security_toggle_bool(cls, v: object) -> object:
        if not isinstance(v, bool):
            raise ValueError(
                f"security toggle must be bool, got {type(v).__name__}"
            )
        return v

    @model_validator(mode="after")
    def _kind_required_fields(self) -> CodeCheck:
        if self.kind == "tests_pass":
            if self.min_tests is None:
                raise ValueError("tests_pass requires min_tests")
        elif self.kind == "reproducible_rerun":
            if self.rerun_tol is None:
                raise ValueError("reproducible_rerun requires rerun_tol")
            if self.tolerance is not None:
                raise ValueError(
                    "reproducible_rerun: rerun_tol only (tolerance forbidden)"
                )
        elif self.kind == "metric_on_holdout":
            if not self.output:
                raise ValueError("metric_on_holdout requires output")
            if self.op is None:
                raise ValueError("metric_on_holdout requires op")
        return self


class ReviewCheck(Frozen):
    reviewer_role: Literal["reviewer", "critic", "human"]
    question: str
    max_calls: int = 1


PredictedDirection = Literal["positive", "negative", "two_sided"]


class RequirementTemplate(Frozen):
    """One source: human-readable text_ru + machine check together.

    ``role`` (Duhem–Quine) is catalog-authored only:
    - ``prediction`` — FAILED → ``not_met`` → counting refute
    - ``validity`` — FAILED → claim invalid; experiment capped at inconclusive

    SESOI / ``predicts_null`` are catalog-only (never ``RequirementRef.params``).
    """

    id: str
    title: str
    text_ru: str
    method_sources: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    applies_to: list[str]  # registered node kinds (strings), not a hard-coded ontology
    check: CodeCheck | ReviewCheck
    role: Literal["prediction", "validity"]
    required: bool = True
    version: StrictInt
    # Severe / TOST threshold — catalog-authored only.
    uses_sesoi: bool = False
    sesoi: float | None = None
    sesoi_min: float | None = None
    sesoi_max: float | None = None
    predicts_null: bool = False
    predicted_direction: PredictedDirection = "two_sided"
    # Catalog-authored freeze skip (#41 sandbox_limits). Agent refs cannot set this.
    # Required — no default (catalog loader supplies it explicitly).
    exempt_from_freeze: bool

    @field_validator("exempt_from_freeze", mode="before")
    @classmethod
    def _tmpl_exempt_must_be_bool(cls, v: object) -> object:
        if not isinstance(v, bool):
            raise ValueError(
                f"exempt_from_freeze must be bool, got {type(v).__name__}"
            )
        return v


class Catalog(Frozen):
    # Integer generation (catalog loader allow-lists); same StrictInt as Study.
    catalog_version: StrictInt
    edition: str
    sources_sha256: dict[str, str] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)
    templates: list[RequirementTemplate] = Field(default_factory=list)
    defaults: dict[str, list[str]] = Field(default_factory=dict)  # node_kind -> template ids
    # Independence code-vs-data: names listed here are code; else data.
    # ResultRecorded.output_kinds is filled from this list (planner role ignored).
    code_output_names: list[str] = Field(default_factory=list)


class NodeRequirement(Frozen):
    id: str
    template_id: str
    node_id: str
    text_ru: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    required: bool
    status: ReqStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)
    verdict: dict[str, Any] | None = None
    waived_by: str | None = None
    check_version: StrictInt
    is_code: bool = True


class ArtifactHandle(Frozen):
    artifact_id: str
    name: str


# Catalog meta-template id for agent-proposed checks (DESIGN_M1 §16 / PR20).
PROPOSED_CHECK_TEMPLATE_ID = "proposed_check"


class RejectedTemplateNote(Frozen):
    """Why a nearest catalog template does not fit (required on CheckProposal)."""

    template_id: str
    reason: str


class CheckProposal(Frozen):
    """Preregistered agent-proposed check (hash fixed before any result).

    Exactly one of ``check`` (executable CodeCheck kinds) or ``rubric`` (text
    qualitative) must be set. Role / SESOI / threshold are frozen at propose time.
    """

    proposal_id: str
    target: str
    target_kind: Literal["question", "hypothesis"] = "question"
    role: Literal["prediction", "validity"]
    measures: str
    check: CodeCheck | None = None
    rubric: Rubric | None = None
    threshold: Any = None
    sesoi: float | None = None
    uses_sesoi: bool = False
    predicts_null: bool = False
    predicted_direction: PredictedDirection = "two_sided"
    justification: str
    rejected_templates: list[RejectedTemplateNote] = Field(default_factory=list)
    # Filled by core at propose (sha256 of canonical preregistration body).
    proposal_digest: str = ""
    study_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    proposer_attempt_id: str = ""


class QuestionSupersede(Frozen):
    """New question formulation replaces an old one (PR9 / H-d / C10)."""

    new_id: str
    old_id: str


class ExpansionRequest(Frozen):
    """Typed agent request to expand the plan after this task (PR9 / H-g)."""

    tasks: list[TaskSpec] = Field(default_factory=list)
    questions: list[QuestionSpec] = Field(default_factory=list)
    superseded_tasks: list[str] = Field(default_factory=list)
    # Each new_id must appear in ``questions``; old_id is already known to the study.
    superseded_questions: list[QuestionSupersede] = Field(default_factory=list)
    reason: str = ""


class AgentTaskOutput(Frozen):
    claim: Outcome | None = None
    summary: str = ""
    outputs: dict[str, ArtifactHandle] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    downgrade: DowngradeClaim | None = None
    # PR9: typed request; core enqueues PLAN-n → critic (never direct PlanExpanded).
    expansion_request: ExpansionRequest | None = None
    # PR12: actual spend → BudgetDebited(task_id) from Executor.record.
    cost_rub: Decimal | None = None
    # M1 PR15: explicit needs_more_data|infeasible (validated → QuestionDisposed).
    question_disposals: list[dict[str, Any]] = Field(default_factory=list)


class StudyView(Frozen):
    phase: Literal["running", "finalizing", "closed"]
    progress: float = 0.0
    awaiting_human: bool = False
    verdict: Literal["fulfilled", "partial", "stubbed", "failed"] | None = None
    stub_share: float = 0.0


class Usage(Frozen):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_rub: Decimal | None = None
    model: str = ""


# Backward-compatible alias used by projections / API sketches
class Node(Frozen):
    """Projection row for graph/metamodel/web — kind is a registry key."""

    node_id: str
    kind: str
    parent_id: str | None
    ref_id: str
