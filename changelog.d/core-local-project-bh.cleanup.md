core/execution/local_project.py: LocalProjectMixin declares its runtime
attributes and folds read them directly instead of defensive
`getattr(self, ...)` fallbacks. StudyRuntime.__init__ (machine.py) already
assigned every attribute the mixin touches — executor, artifacts (both now
declared on the mixin alongside dep_outputs, dep_provenance, max_attempts,
_task_budget_alloc, _task_spent_rub) and the existing fold state — before any
event fold runs, so those isinstance/lazy-init fallbacks were dead code on the
only production host. Mixin annotations now name the full set; the three test
fakes that call fold methods (tests/core/test_hyp_v2_registration.py,
tests/test_pr15_needs_more_data.py, tests/test_pr8_verdict_runtime.py)
already assign everything they use and needed no changes. In
`_fold_finalized_graph` the payload parameter type Any is replaced with
object (all callers — the StudyFinalized fold via EventEnvelope.payload and
both fold fakes — pass dict values).

Смена поведения: было → стало (все на
coscientist/core/execution/local_project.py): было — отсутствующий атрибут
хоста молча заменялся дефолтом/lazy-init (getattr + isinstance +
пересоздание); стало — прямое чтение: хост без инициализации атрибутов
падает AttributeError. Хост в тестах — LocalProjectMixin со всеми
атрибутами ветки, кроме проверяемого; fold — настоящий EventEnvelope этого
типа (для хелперов — прямой вызов хелпера). Каждый тест:
tests/core/test_local_project_contract.py,
pytest.raises(AttributeError, match=re.escape полного текста). На
StudyRuntime (единственный продовый хост) поведение не изменилось.
Место (строка после правок) → тест-id:
- StudyCreated L132 executor → study-created-executor
- BudgetDebited L151 _task_spent_rub → budget-debited-task-spent
- PlanExpanded: L173 _human_expansions → plan-expanded-human-expansions;
  L175 _agent_expansions → plan-expanded-agent-expansions; L178
  _replan_used_s → plan-expanded-replan-used; L181 _expand_pending →
  plan-expanded-expand-pending; L189 _hitl_expand_used →
  plan-expanded-hitl-expand-used; L199 _foreach_children →
  plan-expanded-foreach-children
- ExpansionRefused L218 _expansion_refusals → expansion-refused-refusals
- HitlOpened L230 _hitl_requests → hitl-opened-open-requests
- HitlDecided: L233 _hitl_decisions → hitl-decided-decisions; L237
  _hitl_requests → hitl-decided-open-requests
- ArtifactRegistered: L264 artifacts → artifact-registered-artifacts; L267
  _pending_kind_stamps → artifact-registered-pending-stamps
- QuestionDisposed L277 question_disposals → question-disposed-store
- QuestionDisposeRejected L282 _disposal_rejects →
  question-dispose-rejected-notes
- StudyFinalized L293 _duplicate_study_finalized →
  study-finalized-duplicates
- TaskCreated: L312 _replan_used_s → task-created-replan-used; L317
  _expand_requests → task-created-expand-requests; L322 _expand_pending →
  task-created-expand-pending
- AttemptStarted L347 _task_budget_alloc → attempt-started-budget-alloc
- ResultRecorded: L391 dep_outputs → result-recorded-dep-outputs; L392
  dep_provenance → result-recorded-dep-provenance
- _apply_kind_stamp_soft: L82 artifacts → apply-kind-stamp-soft; L88
  artifact_kinds → apply-kind-stamp-soft-artifact-kinds; L94
  _stamp_meta_missing → apply-kind-stamp-soft-stamp-meta-missing
- _flush_pending_kind_stamps: L107 _pending_kind_stamps →
  flush-pending-kind-stamps; L113 artifact_kinds →
  flush-pending-kind-stamps-artifact-kinds
- _clear_expand_request L117 _expand_requests → clear-expand-request
Мутационная проверка: для каждого места базовый getattr/lazy-init
временно возвращался — падает ровно перечисленный тест (DID NOT RAISE);
правка возвращена (git diff d5d2349 по файлу пуст до отката трёх мест
ниже). Плюс test_study_runtime_initializes_every_mixin_attribute
(StudyRuntime.__init__ создаёт каждое имя из аннотаций миксина).
НЕ входит в смену поведения — возвращено байт-в-байт к merge-base
07902eb7: тело elif в ArtifactRegistered (L268-274, базовые
getattr + isinstance + пересоздание artifact_kinds) и isinstance-охраны
dep_outputs/dep_provenance в ResultRecorded (L396, L407). Мутационная
проверка показала, что там мутация неотличима от поведения: до этих строк
AttributeError поднимает покрытое место выше, а не-dict значение атрибута
хоста недостижимо при проверке типов — тесты остаются зелёными.

Marker occurrences (grep -o | wc -l) in the file, merge-base 07902eb7 →
head: getattr 31 → 2, Any 17 → 16, cast( 0 → 0, type: ignore 0 → 0,
audit-ok 0 → 0 — это вхождения (occurrences), не строки. Против
origin/cleanup/refactor (те же 31/17/0/0/0) роста нет: getattr 2 ≤ 31,
Any 16 ≤ 17.

Left markers, line: marker: why —
- 194 `getattr(t, "task_key", "")`: not self — a PlanExpanded payload item
  reader. Producers write dicts via `model_dump(mode="json")`
  (core/plan/expand.py), but narrowing the reader would change tolerance for
  unexpected legacy payload shapes; journal payload semantics need a separate
  owner decision (follow-up).
- 268-274 (ArtifactRegistered elif-body) and 396/407 (ResultRecorded
  isinstance guards): restored byte-identical to merge-base — mutation
  testing could not distinguish the direct reads there (see above), so the
  base defensive forms stay.
- 36-72 annotation block + 9 import: dict/list value types stay Any because
  their rows are consumed across machine.py, attempts.py, plan/loop.py,
  plan/share_alloc.py, plan/proposed_check.py and claims/dispose.py —
  replacing them ripples into files outside this PR's bounds (follow-up).
- 92-93 and 151-152: kept byte-identical to base/main (`isinstance(kinds,
  dict)` guard with `kinds.pop(artifact_id, None)`, and
  `raw_spent.get(str(tid), 0)`): the dict-default audit heuristics
  (dict_pop_default / dict_get_default) fire on lines changed vs main, and
  rewriting those forms only to silence the heuristic is prohibited; the
  getattr reads above them are still removed (`kinds = self.artifact_kinds`
  moved before the try so the except block stays assignment-free).

Call chains verified with CodeGraph (this round: `codegraph callers
_project_local`, `codegraph query LocalProjectMixin`; earlier rounds:
`codegraph callers LocalProjectMixin`, `codegraph callers
_apply_kind_stamp_soft`, `codegraph callers _flush_pending_kind_stamps`,
`codegraph callers _clear_expand_request`, `codegraph impact
LocalProjectMixin`, `codegraph query StudyRuntime`, `codegraph query
apply_kind_stamp`, `codegraph query graph_rows_from_finalized`,
`codegraph query added_tasks`) and LeanCTX reads
(`lean-ctx read --fresh`, modes signatures / lines:N-M) of
coscientist/core/execution/{local_project,machine,attempts,artifact_stamp,artifacts}.py,
coscientist/core/events/{events,thresholds}.py,
coscientist/core/claims/dispose.py, coscientist/core/plan/expand.py and the
three fake tests.
