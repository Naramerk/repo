core/claims/dispositions.py: exact types and direct Task attribute access; all
27 `Any` occurrences in the module replaced.

Смена поведения: было → стало (coscientist/core/claims/dispositions.py)
- 526-527 (artifact-path needs_more_data): `reason_detail` наполняется из
  `refs` только когда `refs` — список (было: `list(refs or [])` принимал
  любой iterable — например, строка разворачивалась посимвольно);
  не-список → [] (fail-closed). Для валидированных QuestionDisposal-полезных
  нагрузок (refs: list[str]) значения те же.
- 719-723 и 521-525 (вызовы design_artifact_ok): `design_artifact_id`
  передаётся только если это str (валидированные полезные нагрузки несут
  str | None); не-str id теперь не проходит проверку дизайна →
  not_attempted / missing_experiment_design вместо str()-приведения внутри
  design_artifact_ok (раньше int-id, чья строковая форма была в
  journal_kinds, ложно открывал needs_more_data).

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

Marker occurrences (grep -o | wc -l, dispositions.py), base → head:
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

Tests: tests/claims/test_dispositions_failclosed.py — 3 детерминированных
сценария (два падают на базе, один фиксирует fail-closed тотальность);
существующие тесты не ослаблены.
