"""Execution domain models (DESIGN §2, §3.1). Graph ontology lives in graph.py + registry."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class HitlPolicy(Frozen):
    mode: Literal["interactive", "auto", "headless"] = "headless"
    timeout_s: dict[str, int] = Field(
        default_factory=lambda: {
            "spec_approval": 120,
            "plan_approval": 180,
            "result_acceptance": 1800,
            "skip_task": 1800,
        }
    )
    defaults: dict[str, str] = Field(
        default_factory=lambda: {
            "plan_approval": "human_timeout",
            "result_acceptance": "accept_computed",
            "skip_task": "deny",
        }
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
    simulate: Literal["schema_stub", "llm_stub", "forbid"] = "schema_stub"


class RequirementRef(Frozen):
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


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


class Study(Frozen):
    study_id: str
    profile: str
    request: str
    catalog_version: str
    hitl_policy: HitlPolicy
    deadline: datetime
    created_at: datetime
    parent_study_id: str | None = None


class Plan(Frozen):
    plan_id: str
    study_id: str
    revision: int
    hypothesis_id: str | None
    tasks: list[TaskSpec]


class Task(Frozen):
    task_id: str
    kind: TaskKind
    spec: TaskSpec
    state: TaskState
    outcome: TaskOutcome | None = None
    reason: str | None = None
    deadline: datetime
    attempts: list[str] = Field(default_factory=list)
    inherited_from: str | None = None


class InputBinding(Frozen):
    input: str
    artifact_id: str
    source: Literal["real", "partial", "simulated"]


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
    close_reason: str | None = None
    simulated: bool = False
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
    simulated: bool = False
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
    kind: Literal["plan_approval", "result_acceptance", "skip_task", "spec_approval"]
    payload_ref: str
    deadline: datetime
    study_id: str
    task_id: str
    open: bool = True


class HitlDecision(Frozen):
    request_id: str
    decision: Literal["accept", "revise", "reject"]
    by: Literal["human", "policy"]
    reason: str


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
    phase: Literal["running", "closed"]
    progress: float = 0.0
    awaiting_human: bool = False
    verdict: Literal["fulfilled", "partial", "simulated", "failed"] | None = None
    stub_share: float = 0.0


class Budget(Frozen):
    max_llm_calls: int = 100
    max_tokens: int = 1_000_000
    max_cost_rub: Decimal | None = None
    max_tool_calls: int = 500
    deadline: datetime


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
