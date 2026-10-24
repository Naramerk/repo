"""Per-EventType payload schemas from journal wire shapes. No silent defaults."""
from __future__ import annotations

from typing import Any

from coscientist.core.events.errors import JournalPayloadError

_TAG_CHECK = {
    "none": lambda v: v is None,
    "bool": lambda v: type(v) is bool,
    "int": lambda v: type(v) is int and not isinstance(v, bool),
    "float": lambda v: type(v) is float,
    "str": lambda v: type(v) is str,
    "list": lambda v: type(v) is list,
    "dict": lambda v: type(v) is dict,
}

PAYLOAD_SPECS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    'ArtifactRegistered': {'required': {'artifact_id':('str',),'kind':('none','str',),'name':('str',),'role':('str',),'sha256':('str',)}, 'optional': {'fs_isolation':('none',),'media_type':('str',),'net_isolation':('none',),'produced_by':('dict',),'producer':('str',),'schema_ref':('none',),'size':('int',),'source_uri':('none','str',),'stamped':('bool',),'tainted':('bool',),'uri':('str',)}},
    'AttemptClosed': {'required': {'attempt_id':('str',),'close_reason':('str',)}, 'optional': {'charged':('bool',),'closed':('bool',),'retry_at':('str',),'retryable':('bool',),'task_id':('str',)}},
    'AttemptStarted': {'required': {'attempt_id':('str',),'n':('int',),'provenance':('str',),'study_id':('str',),'task_id':('str',)}, 'optional': {'agent_output_digest':('none',),'allocated_budget_rub':('none','str',),'attempt_role':('str',),'budget_limit_rub':('none','str',),'close_reason':('none',),'closed':('bool',),'deadline':('str',),'dispatch_id':('str',),'inputs':('list',),'inputs_digest':('none','str',),'limitations':('list',),'route_returned':('bool',),'share_renorm':('bool',),'tainted_context':('bool',)}},
    'AttemptTainted': {'required': {'attempt_id':('str',),'call_id':('str',),'is_fetch':('bool',),'reason':('str',),'tool':('str',)}, 'optional': {}},
    'BudgetDebited': {'required': {'request_id':('str',),'spent_rub':('str',)}, 'optional': {'actual_rub':('int',),'budget':('dict',),'cost_estimate_rub':('float',),'decided_by':('str',),'delegated_by':('str',),'reason':('str',),'task_id':('str',)}},
    'BudgetExceeded': {'required': {'scope':('str',)}, 'optional': {}},
    'BudgetSharesAdjusted': {'required': {'approved_by':('str',),'budget_shares':('dict',),'deadline_shares':('dict',),'plan_id':('str',),'request_id':('str',),'tasks':('list',)}, 'optional': {}},
    'BudgetSharesRefused': {'required': {'approved_by':('str',),'detail':('str',),'reason':('str',),'request_id':('str',)}, 'optional': {}},
    'CatalogErrorRaised': {'required': {'error':('str',),'reason':('str',)}, 'optional': {}},
    'CheckApproved': {'required': {'approved_by':('str',),'bound_target':('str',),'bound_task_id':('str',),'by':('str',),'delegated':('bool',),'hitl_request_id':('none','str',),'human_principal':('none','str',),'proposal_digest':('str',),'proposal_id':('str',),'reviewer_attempt_id':('none','str',),'source':('str',)}, 'optional': {}},
    'CheckProposed': {'required': {'attempt_id':('str',),'check':('dict',),'justification':('str',),'measures':('str',),'predicted_direction':('str',),'predicts_null':('bool',),'proposal_digest':('str',),'proposal_id':('str',),'proposer_attempt_id':('str',),'rejected_templates':('list',),'role':('str',),'rubric':('none',),'sesoi':('none',),'study_id':('str',),'target':('str',),'target_kind':('str',),'task_id':('str',),'threshold':('none',),'uses_sesoi':('bool',)}, 'optional': {}},
    'CheckRejected': {'required': {'by':('str',),'delegated':('bool',),'hitl_request_id':('none','str',),'human_principal':('none','str',),'proposal_digest':('str',),'proposal_id':('str',),'reason':('str',),'rejected_by':('str',),'reviewer_attempt_id':('none',),'source':('str',)}, 'optional': {}},
    'DowngradeRejected': {'required': {'attempt_id':('str',),'reason':('str',)}, 'optional': {}},
    'EffectDone': {'required': {'attempt_id':('str',),'key':('str',),'result_ref':('none','str',),'status':('str',),'tool':('str',)}, 'optional': {'detail':('str',),'error_kind':('str',)}},
    'EffectIntended': {'required': {'args_digest':('str',),'attempt_id':('str',),'effect':('str',),'key':('str',),'study_id':('str',),'task_key':('str',),'tool':('str',)}, 'optional': {'fingerprint':('str',)}},
    'ExpansionRefused': {'required': {'after_task':('none','str',),'detail':('str',),'plan_key':('none','str',),'reason':('str',)}, 'optional': {'loop':('bool',),'loop_id':('str',),'loop_reason':('str',),'report_line':('str',)}},
    'FetchIntegrityRaised': {'required': {'call_id':('str',),'internal_error':('bool',),'kind':('str',),'tool':('str',)}, 'optional': {}},
    'ForkCreated': {'required': {'parent_study_id':('str',),'study_id':('str',)}, 'optional': {'clarifications':('list',),'digest':('str',)}},
    'GraphEdgeAdded': {'required': {'attrs':('dict',),'dst_id':('str',),'edge_id':('str',),'kind':('str',),'src_id':('str',),'study_id':('str',)}, 'optional': {'created_at':('none','str',)}},
    'GraphNodeUpserted': {'required': {'attrs':('dict',),'kind':('str',),'node_id':('str',),'ref_id':('str',),'study_id':('str',)}, 'optional': {'created_at':('none','str',),'parent_id':('none',)}},
    'HitlDeadlineExtended': {'required': {'deadline':('str',),'request_id':('str',)}, 'optional': {}},
    'HitlDecided': {'required': {'by':('str',),'decision':('str',),'reason':('str',),'request_id':('str',),'spent_rub':('float',)}, 'optional': {'approved_by':('str',),'cost_estimate_rub':('float',),'decided_by':('none','str',),'delegated_by':('none','str',),'effect':('str',),'human_message':('none','str',),'keep_open':('bool',),'proposal_id':('str',)}},
    'HitlOpened': {'required': {'cost_rub':('float',),'kind':('str',),'open':('bool',),'request_id':('str',),'task_id':('str',)}, 'optional': {'deadline':('str',),'expand_after_task':('none','str',),'payload_ref':('str',),'study_id':('str',)}},
    'HitlTimedOut': {'required': {'reason':('str',),'request_id':('str',),'spent_rub':('float',)}, 'optional': {'proposal_id':('str',)}},
    'JudgeCasCorrupt': {'required': {'detail':('str',),'report_line':('str',)}, 'optional': {}},
    'LateWriteRejected': {'required': {'attempt_id':('str',),'reason':('str',)}, 'optional': {}},
    'LeaseAcquired': {'required': {'epoch':('int',),'owner':('str',)}, 'optional': {'owner_nonce':('str',),'ttl_s':('float','int',)}},
    'LoopCapped': {'required': {'iter':('int',),'loop_id':('str',),'max_iter':('int',),'reason':('str',),'scope_hypotheses':('list',),'scope_questions':('list',),'survivors':('none','int',),'until':('dict',)}, 'optional': {'detail':('str',),'refuse_detail':('str',)}},
    'LoopCompleted': {'required': {'iter':('int',),'loop_id':('str',),'max_iter':('int',),'reason':('str',),'scope_hypotheses':('list',),'scope_questions':('list',),'survivors':('int',),'until':('dict',)}, 'optional': {}},
    'LoopRefused': {'required': {'detail':('str',),'iter':('int',),'loop_id':('str',),'max_iter':('int',),'reason':('str',),'scope_hypotheses':('list',),'scope_questions':('list',),'until':('dict',)}, 'optional': {}},
    'PlanAccepted': {'required': {'expand_after':('list',),'hypothesis_id':('none',),'loops':('list',),'plan_id':('str',),'questions':('list',),'revision':('int',),'study_id':('str',),'tasks':('list',)}, 'optional': {'rollup':('str',),'subquestions':('list',)}},
    'PlanExpanded': {'required': {'added_questions':('list',),'added_tasks':('list',),'after_task':('none','str',),'delegated_by':('none','str',),'expand_after':('list',),'hitl_request_id':('none','str',),'hypothesis_id':('none',),'loops':('list',),'origin':('str',),'plan_id':('str',),'plan_key':('none','str',),'plan_rev':('int',),'prev_plan_id':('str',),'questions':('list',),'reason':('str',),'replan_allocated_s':('float',),'revision':('int',),'rollup':('str',),'study_id':('str',),'subquestions':('list',),'superseded_questions':('list',),'superseded_tasks':('list',),'tasks':('list',)}, 'optional': {'foreach':('bool',),'foreach_cap':('int',),'foreach_capped':('bool',),'foreach_dropped':('list',),'foreach_items':('list',),'foreach_kind':('str',),'foreach_rank_notes':('list',),'foreach_refute':('bool',),'foreach_template':('str',),'foreach_total':('int',),'loop':('bool',),'loop_id':('str',),'loop_iter':('int',),'loop_max_iter':('int',),'loop_template_keys':('list',),'loop_until':('dict',)}},
    'PlanRefused': {'required': {'detail':('str',),'reason':('str',),'report_line':('str',)}, 'optional': {}},
    'QuestionDisposeRejected': {'required': {'detail':('str',),'disposition':('str',),'question_id':('str',),'reason':('str',),'report_line':('str',)}, 'optional': {'row_key':('str',)}},
    'QuestionDisposed': {'required': {'design_artifact_id':('none','str',),'disposition':('str',),'question_id':('str',),'reason':('str',),'refs':('list',)}, 'optional': {}},
    'ResultPending': {'required': {'agent_output_digest':('str',),'attempt_id':('str',),'check_results':('list',),'reason':('str',),'task_id':('str',)}, 'optional': {}},
    'ResultRecorded': {'required': {'agent_claim':('none',),'artifacts':('list',),'attempt_id':('str',),'charged':('bool',),'check_results':('list',),'computed_outcome':('str',),'inputs':('list',),'outcome':('str',),'payload_digest':('str',),'provenance':('str',),'reason':('none','str',),'result_class':('str',),'result_id':('str',),'task_id':('str',),'unassessed':('list',)}, 'optional': {'budget_limit_rub':('str',),'budget_overrun':('bool',),'claims_dropped':('list',),'downgrade':('none',),'expansion_request':('none','dict',),'output_kinds':('dict',),'output_map':('dict',)}},
    'ReviewRecorded': {'required': {'attempt_id':('str',),'check_id':('str',),'failure_kind':('none','str',),'item_id':('str',),'output_digest':('str',),'retry':('int','dict',),'review_key':('str',),'status':('str',),'task_id':('str',)}, 'optional': {}},
    'ReviewScheduled': {'required': {'kind':('str',),'task_id':('str',)}, 'optional': {'attempt_id':('str',),'check_id':('str',),'deadline':('str',),'hitl_request_id':('none','str',),'item_id':('str',),'output_digest':('str',),'proposal_id':('str',),'proposer_attempt_id':('str',),'review_key':('str',),'reviewer_attempt_id':('str',),'reviewer_task_id':('none','str',),'rubric_digest':('str',),'status':('str',)}},
    'RouteReturned': {'required': {'agent_output_digest':('str',),'attempt_id':('str',)}, 'optional': {}},
    'SameDigestRetryRejected': {'required': {'error':('str',),'reason':('str',)}, 'optional': {}},
    'SandboxInfraRaised': {'required': {'error':('str',),'reason':('str',)}, 'optional': {}},
    'SideEffectDenied': {'required': {'attempt_id':('str',),'effect':('str',),'key':('str',),'reason':('str',),'task_key':('str',),'tool':('str',)}, 'optional': {}},
    'SourceFetched': {'required': {'attempt_id':('str',),'call_id':('str',),'digest':('str',),'tool':('str',),'url':('str',)}, 'optional': {}},
    'StudyCancelled': {'required': {'by':('str',),'study_id':('str',)}, 'optional': {'at':('str',)}},
    'StudyCreated': {'required': {'catalog_version':('int',),'study_id':('str',)}, 'optional': {'aggregate':('str',),'as_of':('none','str',),'budget':('none','dict',),'confirmations_needed':('int',),'created_at':('str',),'deadline':('str',),'evalue_alpha':('float',),'finalize_reserve_s':('int',),'hitl_policy':('dict',),'max_depth':('int',),'max_expansions':('int',),'max_foreach_children':('int',),'max_human_expansions':('int',),'max_loop_iter':('int',),'max_proposals':('int',),'max_task_budget_share':('float',),'max_tasks':('int',),'parent_study_id':('none',),'plan_timeout_s':('int',),'profile':('str',),'refutation_priority':('bool',),'refutations_needed':('int',),'replan_reserve':('float',),'request':('str',),'supported_needed':('int',),'user_input_ids':('list',),'x':('int',)}},
    'StudyFinalized': {'required': {'conclusion_ids':('list',),'contributing_reasons':('list',),'fallback_reason':('none','str',),'minimal_report':('none','dict',),'question_dispositions':('dict',),'report_artifact_id':('none','str',),'terminal_reason':('str',),'trigger':('str',),'verdict':('str',)}, 'optional': {'custom_checks':('list',),'hypotheses_unavailable':('none',),'hypothesis_report':('list',),'hypothesis_verdicts':('dict',),'observations':('list',),'refusal':('none','str',)}},
    'StudyRefused': {'required': {'classifier_error':('bool',),'kind':('str',),'reason':('str',)}, 'optional': {'catalog_version':('none','int',)}},
    'TaskCreated': {'required': {'attempts':('list',),'deadline':('str',),'inherited_from':('none',),'kind':('str',),'outcome':('none',),'reason':('none',),'spec':('dict',),'state':('str',),'task_id':('str',)}, 'optional': {'expand_after_task':('str',),'pending_expansion':('dict',),'replan_allocated_s':('float',)}},
    'TaskDone': {'required': {'outcome':('str',),'reason':('none','str',),'task_id':('str',)}, 'optional': {'budget_overrun':('bool',),'detail':('str',)}},
    'TaskInherited': {'required': {'attempts':('list',),'deadline':('str',),'inherited_from':('str','none',),'kind':('str',),'outcome':('str','none',),'reason':('str','none',),'spec':('dict',),'state':('str',),'task_id':('str',)}, 'optional': {}},
    'VerdictSnapshotsRecorded': {'required': {'attempt_id':('str',),'scope':('str',),'snapshots':('dict',),'study_id':('str',)}, 'optional': {}},
}

def validate_event_payload(event_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed typed check; returns ``raw`` unchanged (no defaults)."""
    spec = PAYLOAD_SPECS.get(event_type)
    if spec is None:
        raise JournalPayloadError(
            f"{event_type}: no payload schema registered",
            expected_store_format=None, found_store_format=None,
            loc=("payload",), error_type="untyped_payload",
        )
    if type(raw) is not dict:
        raise JournalPayloadError(
            f"{event_type}: payload must be object",
            expected_store_format=None, found_store_format=None,
            loc=("payload",), error_type="dict_type",
        )
    for key, tags in spec['required'].items():
        if key not in raw:
            raise JournalPayloadError(
                f"{event_type}: missing required {key!r}",
                expected_store_format=None, found_store_format=None,
                loc=(key,), error_type='missing',
            )
        _check(event_type, key, raw[key], tags)
    for key, tags in spec['optional'].items():
        if key in raw:
            _check(event_type, key, raw[key], tags)
    return raw

def _check(event_type: str, key: str, value: Any, tags: tuple[str, ...]) -> None:
    for tag in tags:
        if _TAG_CHECK[tag](value):
            return
    raise JournalPayloadError(
        f"{event_type}.{key}: expected {'|'.join(tags)}, got {type(value).__name__}",
        expected_store_format=None, found_store_format=None,
        loc=(key,), error_type='type_error',
    )

