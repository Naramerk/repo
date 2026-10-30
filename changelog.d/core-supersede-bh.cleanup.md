core/plan/supersede.py: typed sources and direct attribute access instead of
Any/getattr probes. All production callers pass StudyRuntime (plan/expand.py
_expand_plan, plan/expand_critic.py critic_gate; verified with codegraph
callers), so the runtime parameter is now typed by a local read-only protocol
SupersedeRuntime (study / graph / results / catalog / tasks — exactly the
surface StudyRuntime.__init__ creates unconditionally). The protocol keeps
claims.py's lazy import of this module free of a new import-linter edge
(agents must use invoke_tool). graph is GraphStore | None (claims.py
runtime_claim_from_node), task is Task, specs are TaskSpec, catalog rows are
RequirementTemplate. Duck-typed probes are replaced with direct typed
attribute access; the JSON-dict branches in tasks_needed_by_active_questions
/ should_skip_for_question_supersede and the bare-spec fallback
(`spec = getattr(task, "spec", task)`) are removed.

Marker occurrences (вхождения, grep -o | wc -l) in coscientist/core/plan/supersede.py,
base → head: Any 22 → 0, getattr 27 → 0, cast( 0 → 0, type: ignore 0 → 0,
audit-ok 0 → 0. Left markers: none in this file.

Behavior changes (was → now), each covered by a deterministic test in
tests/test_plan_supersede_types.py (file:line → test; every test fails when
the old probe is restored, verified per-hunk by the coordinator's mutation
harness):
- supersede.py:83-84,90 (countable_refutation_n): a runtime object without
  study/graph/results silently returned 0 → direct reads; such an object now
  raises AttributeError ("'object' object has no attribute 'study'"). The
  real StudyRuntime sets all three unconditionally (machine.py __init__).
  → test_countable_refutation_requires_runtime_attributes (83-84),
  test_countable_refutation_requires_results_attribute (90).
- supersede.py:105-116 (_criteria_prediction_capable): missing catalog,
  templates, criteria, role or predicts_null probes silently meant "no
  prediction role" → direct reads (runtime.catalog.templates, spec.criteria,
  ref.template_id, ref.role, tmpl.role, tmpl.predicts_null); a criterion or
  template without the declared attribute raises AttributeError instead of a
  silent default.
  → test_task_protection_requires_catalog_attribute (105),
  test_criteria_prediction_requires_ref_role_attribute (110),
  test_criteria_prediction_requires_template_role_attribute (113),
  test_criteria_prediction_requires_predicts_null_attribute (115).
- supersede.py:126-137,152-157,175-177 (_task_links_to_target,
  _has_refute_tests_edge, task_protected_from_agent_skip): a bare TaskSpec
  was accepted via the spec-fallback probe → the task must be Task;
  task.state / task.spec are read directly ("'TaskSpec' object has no
  attribute 'state'"), and a spec/runtime without task_key or study raises
  AttributeError instead of a silent None default.
  → test_task_protected_requires_task_object (175-177),
  test_task_links_requires_task_spec_attribute (126),
  test_task_links_requires_spec_task_key_attribute (137),
  test_refute_tests_edge_requires_task_spec_attribute (157),
  test_refute_tests_edge_requires_study_attribute (153-154).
- supersede.py:185 (task_protected_from_agent_skip): the planner tests_intent
  probe (`getattr(spec, "tests_intent", None)`) defaulted silently to None, so
  a spec without the attribute silently lost planner-refute protection →
  spec.tests_intent is read directly; a spec without the declared field
  raises AttributeError ("'_SpecNoTestsIntent' object has no attribute
  'tests_intent'"). TaskSpec declares tests_intent (execution/domain.py:410),
  so every typed input keeps protection.
  → test_task_protection_requires_spec_tests_intent_attribute (185).
- supersede.py:231-236,248 (tasks_needed_by_active_questions): task entries
  could be dicts (JSON branch) → entries must be TaskSpec; a dict raises
  AttributeError ("'dict' object has no attribute 'task_key'"). Production
  callers pass validated TaskSpec lists (plan/expand.py model_validate of
  prev_tasks plus added_tasks).
  → test_tasks_needed_rejects_dict_tasks.
- supersede.py:265,270 (should_skip_for_question_supersede): same dict
  branch removed; the task must be TaskSpec.
  → test_should_skip_rejects_dict_task (265),
  test_should_skip_requires_task_addresses_attribute (270).
- supersede.py:268 (should_skip_for_question_supersede): kept byte-for-byte
  at the base (origin/cleanup/refactor): the refute_keys membership retains
  the ``str(key)`` coercion. key is TaskSpec.task_key (str), so this is a
  no-op for every typed input; no behavior change is declared on this line.

CodeGraph 1.6.2 (worktree-local index, codegraph init -i) call-chain checks:
codegraph callers -p . -f plan/supersede.py for question_is_superseded,
criterion_is_superseded, countable_refutation_n,
_criteria_prediction_capable, _task_links_to_target, _has_refute_tests_edge,
task_protected_from_agent_skip, task_has_refute_intent, has_open_refute_task,
target_blocks_agent_supersede, tasks_needed_by_active_questions,
should_skip_for_question_supersede; codegraph node StudyRuntime (typed
surface source, coscientist/core/execution/machine.py); codegraph query
TaskSpec RequirementTemplate criteria (declared field surface). Code read via
lean-ctx read --fresh -m signatures|lines:N-M.
