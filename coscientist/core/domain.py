"""Execution domain models (DESIGN §2, §3.1). Graph ontology lives in graph.py + registry."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.reasons import CloseReason, TaskReason


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
OnTimeout = Literal["delegate", "delegate_capped", "fail"]
HitlGateKind = Literal[
    "spec_approval",
    "plan",
    "plan_approval",
    "hypotheses",
    "costly_or_nonreal",
    "report",
    "result_acceptance",
    "skip_task",
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
        "hypotheses": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "costly_or_nonreal": GateConfig(timeout_s=1800, on_timeout="fail"),
        "report": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "result_acceptance": GateConfig(timeout_s=1800, on_timeout="delegate"),
        "skip_task": GateConfig(timeout_s=1800, on_timeout="delegate"),
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
        }
    )

    def gate(self, kind: str) -> GateConfig:
        if kind in self.gates:
            return self.gates[kind]
        alias = {"plan": "plan_approval", "plan_approval": "plan"}.get(kind)
        if alias and alias in self.gates:
            return self.gates[alias]
        default_ot: OnTimeout = (
            "fail" if kind == "costly_or_nonreal" else self.on_timeout
        )
        return GateConfig(
            enabled=True,
            timeout_s=self.timeout_s.get(kind, 1800),
            on_timeout=default_ot,
        )


class OutputSpec(Frozen):
    name: str
    media_type: str
    schema_ref: str | None = None
    required: bool = True


class InputSpec(Frozen):
    from_task: str
    output: str
    schema_ref: str | None = None
    # M1 PR5: default forbid (DESIGN was schema_stub / use_partial).
    mode: Literal["use_partial", "simulate", "forbid"] = "forbid"


class RequirementRef(Frozen):
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class QuestionSpec(Frozen):
    """Research question carried in PlanAccepted (from SPEC output)."""

    id: str
    text: str


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
    # H-c: allow start when a depends_on parent is FAILED/SKIPPED (no InputSpec).
    tolerate_failed_dep: bool = False
    # experiment.method; "simulation" is scientific (provenance=real), not a stub.
    method: str | None = None


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
    catalog_version: str
    hitl_policy: HitlPolicy
    deadline: datetime
    created_at: datetime
    parent_study_id: str | None = None
    budget: Budget | None = None
    finalize_reserve_s: int = Field(default=30, ge=0)

    def effective_deadline(self) -> datetime:
        """Last instant usable by tasks/attempts/HITL (deadline − finalize reserve)."""
        return self.deadline - timedelta(seconds=self.finalize_reserve_s)


class Plan(Frozen):
    plan_id: str
    study_id: str
    revision: int
    hypothesis_id: str | None
    tasks: list[TaskSpec]
    questions: list[QuestionSpec] = Field(default_factory=list)


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
    artifact_id: str
    source: Provenance


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
    evidence: list[EvidenceRef] = Field(default_factory=list)
    detail: str | None = None


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
    ]
    output: str | None = None
    schema_ref: str | None = None
    pointer: str | None = None
    metric: str | None = None
    op: Literal["<", "<=", "==", ">=", ">", "in"] | None = None
    target: Any = None
    tolerance: float | None = None


class ReviewCheck(Frozen):
    reviewer_role: Literal["reviewer", "critic", "human"]
    question: str
    max_calls: int = 1


class RequirementTemplate(Frozen):
    """One source: human-readable text_ru + machine check together."""

    id: str
    title: str
    text_ru: str
    method_sources: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    applies_to: list[str]  # registered node kinds (strings), not a hard-coded ontology
    check: CodeCheck | ReviewCheck
    required: bool = True
    version: str = "1"


class Catalog(Frozen):
    catalog_version: str
    edition: str
    sources_sha256: dict[str, str] = Field(default_factory=dict)
    rules: list[RuleRef] = Field(default_factory=list)
    templates: list[RequirementTemplate] = Field(default_factory=list)
    defaults: dict[str, list[str]] = Field(default_factory=dict)  # node_kind -> template ids


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
    check_version: str
    is_code: bool = True


class ArtifactHandle(Frozen):
    artifact_id: str
    name: str


class AgentTaskOutput(Frozen):
    claim: Outcome | None = None
    summary: str = ""
    outputs: dict[str, ArtifactHandle] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    downgrade: DowngradeClaim | None = None


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
