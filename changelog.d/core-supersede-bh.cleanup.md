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

Marker occurrences (grep -o | wc -l) in coscientist/core/plan/supersede.py,
base → head: Any 22 → 0, getattr 27 → 0, cast( 0 → 0, type: ignore 0 → 0,
audit-ok 0 → 0. Left markers: none in this file.

Behavior changes (was → now), each covered by a deterministic test in
tests/test_plan_supersede_types.py:
- supersede.py:83-84,90 (countable_refutation_n): a runtime object without
  study/graph/results silently returned 0 → direct reads; such an object now
  raises AttributeError ("'object' object has no attribute 'study'"). The
  real StudyRuntime sets all three unconditionally (machine.py __init__).
- supersede.py:105 (_criteria_prediction_capable): missing catalog/templates
  or criteria silently meant "no prediction role" → direct reads
  (runtime.catalog.templates, spec.criteria, ref.template_id, tmpl.role,
  tmpl.predicts_null).
- supersede.py:123,154,172-174 (_task_links_to_target,
  _has_refute_tests_edge, task_protected_from_agent_skip): a bare TaskSpec
  was accepted via the spec-fallback probe → the task must be Task;
  task.state / task.spec are read directly ("'TaskSpec' object has no
  attribute 'state'").
- supersede.py:228-233,245 (tasks_needed_by_active_questions): task entries
  could be dicts (JSON branch) → entries must be TaskSpec; a dict raises
  AttributeError ("'dict' object has no attribute 'task_key'"). Production
  callers pass validated TaskSpec lists (plan/expand.py model_validate of
  prev_tasks plus added_tasks).
- supersede.py:262,267 (should_skip_for_question_supersede): same dict
  branch removed; the task must be TaskSpec.

CodeGraph 1.6.2 (worktree-local index, codegraph init -i) call-chain checks:
codegraph callers -p . -f plan/supersede.py for question_is_superseded,
criterion_is_superseded, countable_refutation_n,
task_protected_from_agent_skip, task_has_refute_intent, has_open_refute_task,
target_blocks_agent_supersede, tasks_needed_by_active_questions,
should_skip_for_question_supersede; codegraph node StudyRuntime (typed
surface source, coscientist/core/execution/machine.py). Code read via
lean-ctx read --fresh -m signatures|lines:N-M.
