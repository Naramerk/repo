core/claims/dispositions.py: exact types and direct Task attribute access; all
27 `Any` occurrences in the module replaced.

Смена поведения: было → стало (coscientist/core/claims/dispositions.py);
каждая смена покрыта детерминированным тестом (файл:строка → тест в
tests/claims/test_dispositions_failclosed.py; покрытие проверено мутационной
проверкой координатора — тест падает при откате соответствующего ханка):
- 526-527 (artifact-path needs_more_data): `reason_detail` наполняется из
  `refs` только когда `refs` — список; не-список → [] (fail-closed). Было
  (`list(refs or [])`): непустая str разворачивалась посимвольно
  ("abc" → ['a','b','c']); прочий iterable (tuple/dict/set) давал свои
  элементы/ключи; не-итерируемое truthy-значение (например int 5) поднимало
  TypeError на месте, его ловила публичная обёртка dispositions (except
  Exception → все вопросы not_attempted), а через finalize — last-resort
  payload: весь результат становился not_attempted. Стало: вопрос
  обрабатывается индивидуально. Для валидированных QuestionDisposal-полезных
  нагрузок (refs: list[str]) значения те же.
  → test_artifact_disposal_refs_non_list_stays_empty (str);
  test_artifact_disposal_refs_garbage_types_fail_closed (ids
  int-not-iterable/str/tuple/dict; сценарий «весь результат»: соседний
  вопрос остаётся needs_more_data со своим reason_detail).
- 719-723 и 521-525 (вызовы design_artifact_ok): `design_artifact_id`
  передаётся только если это str (валидированные полезные нагрузки несут
  str | None); не-str id теперь не проходит проверку дизайна →
  not_attempted / missing_experiment_design вместо str()-приведения внутри
  design_artifact_ok (раньше int-id, чья строковая форма была в
  journal_kinds, ложно открывал needs_more_data).
  → test_artifact_disposal_non_str_design_artifact_id_fails_closed (521-525),
  test_verdict_disposal_non_str_design_artifact_id_fails_closed (719-723).

Типизация (runtime-идентична, аннотации — строки): планы/результаты/строки
disposition-отчётов — Any заменён на object (совпадает с producers в
claims.py); `by_target: dict[str, list[RuntimeClaim]]`;
`question_disposals: Mapping[str, Mapping[str, object]]` (producer
`fold_question_disposed` пишет dict[str, dict[str, object]]).
- 423-429: чтение spec/addresses/task_key напрямую через типизированный
  Task/TaskSpec (обязательные pydantic-поля). Для валидированных Task
  поведение идентично байтово; для объекта без `spec` (обход валидации)
  AttributeError поднимается раньше на один оператор (раньше — в
  `_task_by_key`), тип ошибки и fail-closed fallback те же — не является
  сменой наблюдаемого поведения.

Marker occurrences (вхождения, grep -o | wc -l, dispositions.py), base → head:
Any 27 → 0, getattr 4 → 1, type: ignore 0 → 0, cast( 0 → 0, audit-ok 0 → 0.

Оставленный маркер, строка: маркер: почему —
- 430 `getattr(t, "task_id", "")`: строка с `cover.setdefault(...)` оставлена
  байт-в-байт как в базе: её изменение даёт хит `set_default` эвристики
  scripts/audit_heuristics.py на изменённой строке, снимаемый только
  запрещённым новым audit-ok, а перенос setdefault в другую форму был бы
  обходом эвристики.

Call chains verified with CodeGraph 1.6.2 (`codegraph callers` для
dispositions, apply_loop_cap_dispositions, stamp_honest_terminal_verdict,
report_hypothesis_verdicts: production callers finalize.py, machine.py,
experimental/runtime.py принимают object-строки через Mapping/Any-совместимые
параметры) и LeanCTX 3.11.4 (signature/lines-чтения domain.py — Task/TaskSpec,
claims.py, rubric.py, question_tasks.py, rollup.py, decomposes.py,
experiment_design.py, dispose.py, study_view.py, finalize.py).

Tests: tests/claims/test_dispositions_failclosed.py — 5 детерминированных
теста (8 кейсов с учётом parametrize ids): падают на базе 7 (в т.ч. все
кейсы refs-теста — int-кейс падает именно из-за деградации всего результата
в not_attempted), 1 фиксирует fail-closed тотальность и проходит на обеих
сторонах; существующие тесты не ослаблены.
