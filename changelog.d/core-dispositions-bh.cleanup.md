core/claims/dispositions.py: exact types and direct Task attribute access; all
27 `Any` occurrences in the module replaced.

Смена поведения: было → стало (coscientist/core/claims/dispositions.py)
- 423-428 (runtime-task cover union): getattr-with-default reads replaced by
  direct `t.spec` / `spec.addresses` / `spec.task_key` / `t.task_id` —
  required pydantic fields on Task/TaskSpec. Для каждого валидированного Task
  результат объединения идентичен байтово; для объекта, обошедшего валидацию
  без `spec` (или с пустым `task_key` и отсутствующим `task_id`), чтение
  теперь поднимает AttributeError внутри total-обёртки dispositions() →
  честный not_attempted-fallback по всем вопросам (раньше объект тихо
  пропускался / добавлял пустой ключ cover; задача без spec и раньше падала
  строкой ниже в `_task_by_key`, с тем же типом ошибки и тем же fallback).
- 519-522 (artifact-path needs_more_data): `reason_detail` наполняется из
  `refs` только когда `refs` — список (было: `list(refs or [])` принимал
  любой iterable — например, строка разворачивалась посимвольно);
  не-список → [] (fail-closed). Для валидированных QuestionDisposal-полезных
  нагрузок (refs: list[str]) значения те же.
- 519-520 и 714-716 (вызовы design_artifact_ok): `design_artifact_id`
  передаётся только если это str (валидированные полезные нагрузки несут
  str | None); не-str id теперь не проходит проверку дизайна →
  not_attempted / missing_experiment_design вместо str()-приведения внутри
  design_artifact_ok.

Типизация (runtime-идентична, аннотации — строки): планы/результаты/строки
disposition-отчётов — Any заменён на object (совпадает с producers в
claims.py); `by_target: dict[str, list[RuntimeClaim]]`;
`question_disposals: Mapping[str, Mapping[str, object]]` (producer
`fold_question_disposed` пишет dict[str, dict[str, object]]).

Marker occurrences (grep -o | wc -l, dispositions.py), base → head:
Any 27 → 0, getattr 4 → 0, type: ignore 0 → 0, cast( 0 → 0, audit-ok 0 → 0.

Call chains verified with CodeGraph 1.6.2 (`codegraph callers` для
dispositions, apply_loop_cap_dispositions, stamp_honest_terminal_verdict,
report_hypothesis_verdicts: production callers finalize.py, machine.py,
experimental/runtime.py принимают object-строки через Mapping/Any-совместимые
параметры) и LeanCTX 3.11.4 (signature/lines-чтения domain.py — Task/TaskSpec,
claims.py, rubric.py, question_tasks.py, rollup.py, decomposes.py,
experiment_design.py, dispose.py, study_view.py, finalize.py).

Tests: tests/claims/test_dispositions_failclosed.py — 2 детерминированных
fail-closed сценария; существующие тесты не ослаблены.
