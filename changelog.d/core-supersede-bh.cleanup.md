core/plan/supersede.py: typed sources and direct attribute access instead of
Any/getattr probes. All production callers pass StudyRuntime (plan/expand.py
_expand_plan, plan/expand_critic.py critic_gate; verified with codegraph
callers), so the runtime parameter is now typed by a local read-only protocol
SupersedeRuntime (study / graph / results / catalog / tasks — exactly the
surface StudyRuntime.__init__ creates unconditionally). The protocol keeps
claims.py's lazy import of this module free of a new import-linter edge
(agents must use invoke_tool). graph is GraphStore in the SupersedeRuntime
protocol (StudyRuntime sets it unconditionally; question_is_superseded keeps
its own graph: GraphStore | None parameter), task is Task, specs are TaskSpec,
catalog rows are RequirementTemplate. Duck-typed probes are replaced with
direct typed attribute access; the JSON-dict branches in
tasks_needed_by_active_questions / should_skip_for_question_supersede and the
bare-spec fallback (`spec = getattr(task, "spec", task)`) are removed.

Marker occurrences (вхождения, grep -o | wc -l), base → head:
- coscientist/core/plan/supersede.py: Any 22 → 0, getattr 27 → 0, cast( 0 → 0,
  type: ignore 0 → 0, audit-ok 0 → 0. Left markers: none in this file.
- tests/test_plan_supersede_types.py (new in this PR): 0 → 0 for all five.

Declared behavior changes (was → now), each covered by a deterministic test in
tests/test_plan_supersede_types.py (pytest.raises(AttributeError,
match=re.escape(...))):
- supersede.py:83-84,90 (countable_refutation_n): a runtime object without
  study/graph/results silently returned 0 → direct reads; such an object now
  raises AttributeError ("'_RuntimeNoReads' object has no attribute 'study'").
  The real StudyRuntime sets all three unconditionally (machine.py __init__).
  → test_countable_refutation_requires_runtime_attributes (83),
  test_countable_refutation_requires_results_attribute (90).
  Graph slot with a real study present: было — the getattr probe defaulted to
  None and the count silently returned 0; стало — AttributeError.
  → test_countable_refutation_requires_graph_attribute (84; "'_RuntimeStudyNoGraph'
  object has no attribute 'graph'").
- supersede.py:105 (_criteria_prediction_capable): a runtime without the
  declared catalog slot silently meant "no prediction role" →
  runtime.catalog.templates is read directly; a runtime stub declaring the
  slot without assignment raises AttributeError ("'_RuntimeNoCatalog' object
  has no attribute 'catalog'").
  → test_task_protection_requires_catalog_attribute (105).
- supersede.py:133-134 (_task_links_to_target): a runtime without ``study``
  (or with a real study but without ``graph``) reaching the non-addresses
  path: было — the getattr probes defaulted both slots to None and the helper
  returned bool(addrs) / False (an unaddressed task silently unlinked, so
  agent-skip protection was lost); стало — AttributeError.
  → test_task_protection_requires_study_attribute (133 via
  task_protected_from_agent_skip; "'_RuntimeNoStudy' object has no attribute
  'study'"), test_task_links_requires_study_attribute (133; direct call),
  test_task_protection_requires_graph_attribute_unaddressed (134 via
  task_protected_from_agent_skip; "'_RuntimeStudyNoGraph' object has no
  attribute 'graph'"), test_task_links_requires_graph_attribute (134; direct
  call). The direct-call pair exists for the mutation check: on a tree with
  only 133-134 reverted the wrapper still raises the identical
  AttributeError one helper later (153-154), so the helper-boundary tests
  are what fails there.
- supersede.py:153-154 (_has_refute_tests_edge): a runtime without ``study``
  silently treated the refute tests edge as absent → runtime.study /
  runtime.graph are read directly before the task spec; such a runtime raises
  AttributeError ("'_RuntimeNoStudy' object has no attribute 'study'").
  Graph slot with a real study, reached via linked addresses (the addresses
  branch returns True without touching study/graph, and a real empty catalog
  keeps _criteria_prediction_capable False): было — the graph probe defaulted
  to None and the edge was treated as absent; стало — AttributeError.
  → test_refute_tests_edge_requires_study_attribute (153-154),
  test_task_protection_requires_graph_attribute_addressed (154;
  "'_RuntimeStudyNoGraph' object has no attribute 'graph'").

No change for typed inputs (hunks re-declared; the previous tests asserting
them fed inputs production cannot produce and were removed — precedent #458):
- 109-115 (_criteria_prediction_capable): было → стало for an untyped
  object (validation bypass) without criteria / template_id / role /
  predicts_null: было — getattr default (None / [] / False), so the function
  returned False (no prediction role, no exception); стало — AttributeError.
  For typed inputs the behavior is identical: these are declared fields of
  the pydantic TaskSpec / RequirementRef / RequirementTemplate models, and all
  production sources are typed (codegraph callers).
- 126,137,157,175-177 (_task_links_to_target, _has_refute_tests_edge,
  task_protected_from_agent_skip): было → стало for an untyped object
  without spec / state / addresses / task_key: было — the bare-spec fallback
  (`getattr(task, "spec", task)`) used the object itself as the spec, missing
  state compared unequal to DONE, missing addresses / task_key defaulted to
  [] / None, so the result was False (not linked, not protected), no
  exception; стало — AttributeError. For typed inputs the behavior is
  identical: spec / state / task_key / addresses are fields of pydantic Task
  / TaskSpec, and production always passes Task (codegraph callers).
- No test covers the untyped path for these hunks: such an input cannot be
  built without model_construct, which is a marker. Runtime-stub
  reachability re-check (review blocker on 133-134): a SupersedeRuntime
  protocol stub reaches a hunk only where the hunk itself reads a runtime
  slot. That is true for 83-84, 90, 105, 133-134 and 153-154 — all are now
  declared behavior changes with tests above. The remaining hunks
  109-115, 126, 137, 157, 175-177, 185, 231-236, 248, 265, 270 read only
  fields of pydantic Task / TaskSpec / RequirementRef / RequirementTemplate
  (109-115: ref.template_id / ref.role / tmpl.role / tmpl.predicts_null off
  the validated spec.criteria; 231-236, 248, 265, 270: tasks_needed_by_active_questions
  and should_skip_for_question_supersede take the TaskSpec list / TaskSpec
  parameter directly and read no runtime slot at all), so a runtime stub
  cannot reach them with diverging behavior; a diverging input would need a
  non-pydantic Task/TaskSpec/RequirementRef/RequirementTemplate stub, i.e.
  model_construct.
- 185: reading spec.tests_intent directly (declared TaskSpec field,
  execution/domain.py:410, default None). For validated TaskSpec the behavior
  is identical; planner-refute protection is unchanged.
- 231-236,248 (tasks_needed_by_active_questions) and 265,270
  (should_skip_for_question_supersede): the dict branch is removed; entries
  must be TaskSpec. Both are called only from plan/expand.py _expand_plan
  (codegraph callers) with prev_tasks + added_tasks. plan/expand.py:277-280
  validates only the dict elements of prev_tasks (TaskSpec.model_validate).
  A dict in added_tasks never reaches supersede, same as on base:
  expand_validate.py:147 (`{t.task_key for t in merged}`) raises
  AttributeError, validate_expansion (expand_validate.py:109-110) returns it
  as the error detail, and plan/expand.py:304-309 emits ExpansionRefused
  (reason "invalid") before line 313. For typed input the behavior is
  identical.
- 268: kept byte-for-byte at the base (origin/cleanup/refactor): the
  refute_keys membership retains the ``str(key)`` coercion. key is
  TaskSpec.task_key (str), so this is a no-op for every typed input; no
  behavior change is declared on this line.

Removed tests (impossible inputs; hunks re-declared above as no change for
typed inputs): test_tasks_needed_rejects_dict_tasks,
test_should_skip_rejects_dict_task (dict instead of TaskSpec);
test_task_protected_requires_task_object,
test_task_links_requires_task_spec_attribute,
test_refute_tests_edge_requires_task_spec_attribute,
test_task_links_requires_spec_task_key_attribute,
test_task_protection_requires_spec_tests_intent_attribute,
test_should_skip_requires_task_addresses_attribute,
test_criteria_prediction_requires_ref_role_attribute,
test_criteria_prediction_requires_template_role_attribute,
test_criteria_prediction_requires_predicts_null_attribute (Task/TaskSpec/
RequirementRef/RequirementTemplate-shaped stubs missing required pydantic
fields). The runtime AttributeError tests are kept: runtime stubs declare the
missing SupersedeRuntime slot as a class annotation without assignment
(type-checks against the protocol, raises AttributeError on access); the two
task-taking tests now pass real Task objects.

Mutation verification (re-run on the review-blocker commit, 18 tests): every
-U0 hunk of the supersede.py diff vs origin/cleanup/refactor was reverted on
its own (__pycache__ cleared) and tests/test_plan_supersede_types.py was run
against the reverted file; probe = line numbers where ``getattr(`` occurs
after the revert. Declared behavior hunks CAUGHT: 5/5 —
- 83: probes land at 83-84 (own place); caught by
  test_countable_refutation_requires_runtime_attributes and
  test_countable_refutation_requires_graph_attribute.
- 90: caught by test_countable_refutation_requires_results_attribute.
- 105: caught by test_task_protection_requires_catalog_attribute.
- 152: probes land at 153-154 (own place); caught by
  test_refute_tests_edge_requires_study_attribute and
  test_task_protection_requires_graph_attribute_addressed.
- 133: the revert tool applies this hunk at its byte-identical twin 83-84
  (probe: probes sit at 83-84 afterwards), so its tool CAUGHT is hunk 83's
  coverage. A faithful single-position revert of exactly 133-134 was applied
  manually and IS CAUGHT by test_task_links_requires_study_attribute and
  test_task_links_requires_graph_attribute; the other 16 tests stay green on
  that intermediate tree because the wrapper path then raises the identical
  AttributeError from the 153-154 reads.
MISSED outside that set is expected and honest:
- no-change-for-typed-inputs hunks (109, 115, 126, 137, 157, 175, 177, 185,
  231, 236, 248, 265, 270): their only coverage was the removed
  impossible-input tests. Faithful single-position getattr-probe reverts of
  185, 236 and 248 were applied manually and leave all 18 tests green —
  behavior is identical for typed inputs.
- imports, signatures, annotations and the SupersedeRuntime protocol class
  (5, 8, 15, 27, 56, 69, 81, 87, 103, 120, 167, 191, 195, 209, 218, 224,
  229, 257): no runtime read surface involved.
- observed CAUGHT that is NOT behavior coverage, verified artifacts: 5
  (reverting the typing import removes ``Protocol``, so the module fails to
  import), 236 and 248 (the base lines call the helpers ``_addrs``/``_deps``
  which this PR removes elsewhere, so the intermediate state raises
  NameError; faithful reverts are green as noted above), 245 (base lines
  rename ``dep`` back to ``t`` and break the sibling line).

CodeGraph 1.6.2 (worktree-local index, codegraph init -i) call-chain checks:
codegraph callers for question_is_superseded, criterion_is_superseded,
countable_refutation_n, _criteria_prediction_capable, _task_links_to_target,
_has_refute_tests_edge, task_protected_from_agent_skip, task_has_refute_intent,
has_open_refute_task, target_blocks_agent_supersede,
tasks_needed_by_active_questions, should_skip_for_question_supersede;
codegraph node StudyRuntime (typed surface source,
coscientist/core/execution/machine.py); codegraph query TaskSpec
RequirementTemplate criteria (declared field surface). Code read via
lean-ctx read --fresh -m signatures|lines:N-M.
