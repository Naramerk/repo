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
  → test_countable_refutation_requires_runtime_attributes (83-84),
  test_countable_refutation_requires_results_attribute (90).
- supersede.py:105 (_criteria_prediction_capable): a runtime without the
  declared catalog slot silently meant "no prediction role" →
  runtime.catalog.templates is read directly; a runtime stub declaring the
  slot without assignment raises AttributeError ("'_RuntimeNoCatalog' object
  has no attribute 'catalog'").
  → test_task_protection_requires_catalog_attribute (105).
- supersede.py:153-154 (_has_refute_tests_edge): a runtime without ``study``
  silently treated the refute tests edge as absent → runtime.study /
  runtime.graph are read directly before the task spec; such a runtime raises
  AttributeError ("'_RuntimeNoStudy' object has no attribute 'study'").
  → test_refute_tests_edge_requires_study_attribute (153-154).

No change for typed inputs (hunks re-declared; the previous tests asserting
them fed inputs production cannot produce and were removed — precedent #458):
- 109-115: reading spec.criteria / ref.template_id / ref.role / tmpl.role /
  tmpl.predicts_null directly via typed TaskSpec / RequirementRef /
  RequirementTemplate (declared pydantic fields). For validated inputs the
  behavior is identical byte-for-byte; for an object bypassing validation
  AttributeError is raised one statement earlier (previously a silent
  default), same error type — not an observable behavior change.
- 126,137,157,175-177: reading task.spec / task.state / spec.task_key
  directly via typed Task / TaskSpec (required pydantic fields). For
  validated Task the behavior is identical byte-for-byte; for an object
  without ``spec``/``state`` (validation bypass) AttributeError is raised one
  statement earlier (previously in ``_task_links_to_target`` / the probe),
  same error type and fail-closed fallback — not an observable behavior
  change. The bare-spec fallback is gone; codegraph callers shows production
  always passes Task.
- 185: reading spec.tests_intent directly (declared TaskSpec field,
  execution/domain.py:410, default None). For validated TaskSpec the behavior
  is identical; planner-refute protection is unchanged.
- 231-236,248 (tasks_needed_by_active_questions) and 265,270
  (should_skip_for_question_supersede): the dict branch is removed; entries
  must be TaskSpec. Production passes validated TaskSpec lists
  (plan/expand.py:277-280 TaskSpec.model_validate of prev_tasks plus
  added_tasks), so a dict entry is impossible past that boundary; for typed
  input the behavior is identical.
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

Mutation verification: every -U0 hunk of the supersede.py diff vs
origin/cleanup/refactor was reverted on its own (__pycache__ cleared) and
tests/test_plan_supersede_types.py was run against the reverted file.
Declared behavior hunks CAUGHT: 4/4 — 83, 90, 105, 152. MISSED outside that
set is expected and honest:
- no-change-for-typed-inputs hunks (109, 115, 126, 133, 137, 157, 175, 177,
  185, 231, 236, 248, 265, 270): their only coverage was the removed
  impossible-input tests. Faithful single-position getattr-probe reverts of
  133-134, 236, 248 and 185 were applied manually and leave all 12 tests
  green — behavior is identical for typed inputs.
- imports, signatures, annotations and the SupersedeRuntime protocol class
  (8, 15, 27, 56, 69, 81, 87, 103, 120, 167, 191, 195, 209, 218, 224, 229,
  257): no runtime read surface involved.
- observed CAUGHT that is NOT behavior coverage, verified artifacts: 5
  (reverting the typing import removes ``Protocol``, so the module fails to
  import), 133 (a second hunk with a byte-identical body exists at 83-84;
  the revert tool applied it at 83-84, and the faithful revert at 133-134 is
  green as noted above), 236 and 248 (the base lines call the helpers
  ``_addrs``/``_deps`` which this PR removes elsewhere, so the intermediate
  state raises NameError; faithful reverts are green as noted above), 245
  (base lines rename ``dep`` back to ``t`` and break the sibling line).

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
