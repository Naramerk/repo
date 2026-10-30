core/execution/local_project.py: LocalProjectMixin declares its runtime
attributes and folds read them directly instead of defensive
`getattr(self, ...)` fallbacks. StudyRuntime.__init__ (machine.py) already
assigned every attribute the mixin touches — executor, artifacts (both now
declared on the mixin alongside dep_outputs, dep_provenance, max_attempts,
_task_budget_alloc, _task_spent_rub) and the existing fold state — before any
event fold runs, so the isinstance/lazy-init fallbacks were dead code on the
only production host. Mixin annotations now name the full set; the three test
fakes that call fold methods (tests/core/test_hyp_v2_registration.py,
tests/test_pr15_needs_more_data.py, tests/test_pr8_verdict_runtime.py)
already assign everything they use and needed no changes. New contract tests:
tests/core/test_local_project_contract.py asserts StudyRuntime.__init__
assigns every name declared on the mixin, and that a host skipping
initialization fails loudly (AttributeError) instead of silently
self-healing. In `_fold_finalized_graph` the payload parameter type Any is
replaced with object (all callers — the StudyFinalized fold via
EventEnvelope.payload and both fold fakes — pass dict values).

Смена поведения: было → стало (все на
coscientist/core/execution/local_project.py):
- _apply_kind_stamp_soft (L75), _flush_pending_kind_stamps (L105),
  _clear_expand_request (L115), _project_local folds StudyCreated/BudgetDebited/
  PlanExpanded/ExpansionRefused/HitlOpened/HitlDecided/ArtifactRegistered/
  QuestionDisposed/QuestionDisposeRejected/StudyFinalized/TaskCreated/
  AttemptStarted/ResultRecorded (L126-466): было — отсутствующий атрибут хоста
  молча заменялся дефолтом/lazy-init (getattr + isinstance + пересоздание);
  стало — прямое чтение: хост без инициализации атрибутов падает
  AttributeError. На StudyRuntime (единственный продовый хост, все атрибуты
  создаёт __init__) поведение не изменилось. Тест:
  test_fold_without_initialized_host_fails_fast (AttributeError,
  match=re.escape полного текста) + test_study_runtime_initializes_every_
  mixin_attribute.

Marker occurrences (grep -o | wc -l) in the file, merge-base → head:
getattr 31 → 1, Any 17 → 16, cast( 0 → 0, type: ignore 0 → 0, audit-ok 0 → 0.

Left markers, line: marker: why —
- 194 `getattr(t, "task_key", "")`: not self — a PlanExpanded payload item
  reader. Producers write dicts via `model_dump(mode="json")`
  (core/plan/expand.py), but narrowing the reader would change tolerance for
  unexpected legacy payload shapes; journal payload semantics need a separate
  owner decision (follow-up).
- 36-72 annotation block + 9 import: dict/list value types stay Any because
  their rows are consumed across machine.py, attempts.py, plan/loop.py,
  plan/share_alloc.py, plan/proposed_check.py and claims/dispose.py —
  replacing them ripples into files outside this PR's bounds (follow-up).
- 90-92 and 148-149: kept byte-identical to base/main (`isinstance(kinds,
  dict)` guard with `kinds.pop(artifact_id, None)`, and
  `raw_spent.get(str(tid), 0)`): the dict-default audit heuristics
  (dict_pop_default / dict_get_default) fire on lines changed vs main, and
  rewriting those forms only to silence the heuristic is prohibited; the
  getattr reads above them are still removed.

Call chains verified with CodeGraph (`codegraph callers LocalProjectMixin`,
`codegraph callers _apply_kind_stamp_soft`, `codegraph callers
_flush_pending_kind_stamps`, `codegraph callers _clear_expand_request`,
`codegraph impact LocalProjectMixin`, `codegraph query StudyRuntime`,
`codegraph query apply_kind_stamp`, `codegraph query graph_rows_from_finalized`,
`codegraph query added_tasks`) and LeanCTX reads of
coscientist/core/execution/{local_project,machine,attempts}.py,
coscientist/core/plan/expand.py and the three fake tests.
