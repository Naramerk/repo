"""Journal schema version constants (writers must not hard-code literals).

Every envelope ``schema_version`` is derived from the ``UPCASTERS`` registry via
``writer_schema_version``. After the schema v1 baseline collapse, all production
types are writer v1 (empty chains).
"""

from __future__ import annotations

from coscientist.core.events.upcasters import writer_schema_version

# Plan / study writers
PLAN_EXPANDED_SCHEMA_VERSION = writer_schema_version("PlanExpanded")
STUDY_CREATED_SCHEMA_VERSION = writer_schema_version("StudyCreated")
PLAN_ACCEPTED_SCHEMA_VERSION = writer_schema_version("PlanAccepted")
TASK_CREATED_SCHEMA_VERSION = writer_schema_version("TaskCreated")
STUDY_FINALIZED_SCHEMA_VERSION = writer_schema_version("StudyFinalized")

# Attempt / result path
ATTEMPT_STARTED_SCHEMA_VERSION = writer_schema_version("AttemptStarted")
ATTEMPT_CLOSED_SCHEMA_VERSION = writer_schema_version("AttemptClosed")
TASK_DONE_SCHEMA_VERSION = writer_schema_version("TaskDone")
RESULT_RECORDED_SCHEMA_VERSION = writer_schema_version("ResultRecorded")
RESULT_PENDING_SCHEMA_VERSION = writer_schema_version("ResultPending")
ROUTE_RETURNED_SCHEMA_VERSION = writer_schema_version("RouteReturned")
LATE_WRITE_REJECTED_SCHEMA_VERSION = writer_schema_version("LateWriteRejected")
DOWNGRADE_REJECTED_SCHEMA_VERSION = writer_schema_version("DowngradeRejected")
VERDICT_SNAPSHOTS_RECORDED_SCHEMA_VERSION = writer_schema_version(
    "VerdictSnapshotsRecorded"
)

# Graph / artifacts / fetch
ARTIFACT_REGISTERED_SCHEMA_VERSION = writer_schema_version("ArtifactRegistered")
GRAPH_NODE_UPSERTED_SCHEMA_VERSION = writer_schema_version("GraphNodeUpserted")
GRAPH_EDGE_ADDED_SCHEMA_VERSION = writer_schema_version("GraphEdgeAdded")
SOURCE_FETCHED_SCHEMA_VERSION = writer_schema_version("SourceFetched")
ATTEMPT_TAINTED_SCHEMA_VERSION = writer_schema_version("AttemptTainted")
FETCH_INTEGRITY_RAISED_SCHEMA_VERSION = writer_schema_version("FetchIntegrityRaised")

# Plan lifecycle / loops / expand
STUDY_REFUSED_SCHEMA_VERSION = writer_schema_version("StudyRefused")
STUDY_CANCELLED_SCHEMA_VERSION = writer_schema_version("StudyCancelled")
PLAN_REFUSED_SCHEMA_VERSION = writer_schema_version("PlanRefused")
EXPANSION_REFUSED_SCHEMA_VERSION = writer_schema_version("ExpansionRefused")
LOOP_CAPPED_SCHEMA_VERSION = writer_schema_version("LoopCapped")
LOOP_REFUSED_SCHEMA_VERSION = writer_schema_version("LoopRefused")
LOOP_COMPLETED_SCHEMA_VERSION = writer_schema_version("LoopCompleted")

# Budget / HITL / reviews / dispose / proposed checks
BUDGET_EXCEEDED_SCHEMA_VERSION = writer_schema_version("BudgetExceeded")
BUDGET_DEBITED_SCHEMA_VERSION = writer_schema_version("BudgetDebited")
HITL_OPENED_SCHEMA_VERSION = writer_schema_version("HitlOpened")
HITL_TIMED_OUT_SCHEMA_VERSION = writer_schema_version("HitlTimedOut")
REVIEW_SCHEDULED_SCHEMA_VERSION = writer_schema_version("ReviewScheduled")
REVIEW_RECORDED_SCHEMA_VERSION = writer_schema_version("ReviewRecorded")
QUESTION_DISPOSED_SCHEMA_VERSION = writer_schema_version("QuestionDisposed")
QUESTION_DISPOSE_REJECTED_SCHEMA_VERSION = writer_schema_version(
    "QuestionDisposeRejected"
)
CHECK_PROPOSED_SCHEMA_VERSION = writer_schema_version("CheckProposed")
CHECK_APPROVED_SCHEMA_VERSION = writer_schema_version("CheckApproved")
CHECK_REJECTED_SCHEMA_VERSION = writer_schema_version("CheckRejected")
JUDGE_CAS_CORRUPT_SCHEMA_VERSION = writer_schema_version("JudgeCasCorrupt")

# InputBinding digest formula version (PR19 JCS rows). Not an event envelope version.
INPUTS_DIGEST_VERSION = 1
