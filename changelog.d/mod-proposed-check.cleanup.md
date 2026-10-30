`proposed_check`: убраны доказуемо мёртвые `getattr`-обёртки и сужены типы
приватных хелперов (`coscientist/core/plan/proposed_check.py`). Поведение,
API, сообщения об ошибках и журнальные форматы не менялись.

Убрано (атрибут гарантирован типом у всех объектов, приходящих в функцию —
проверено CodeGraph callers/impact по продакшен- и тестовым вызовам):
`_check_proposals`, `results`, `attempts` (объявлены в
`StudyRuntime.__init__`), `study` (`Study | None`), `study.max_proposals`
(поле модели `Study`), `task.deadline` (обязательное поле модели `Task`),
`tasks`, `_hitl_requests`, `_hitl_decisions` (объявлены), `h.kind` /
`h.payload_ref` (обязательные поля `HitlRequest`; не-dict `h` приходит только
из `runtime.hitl: dict[str, HitlRequest]`). `_task_has_result` /
`_task_has_started_attempt` типизированы `StudyRuntime` (единственный
вызывающий — `propose_check`).

Оставлены маркеры (строка: маркер: почему нельзя снять без смены поведения):
- `_reviews_map` getattr `_proposed_check_reviews`: атрибут НЕ объявлен в
  `__init__` ни одного runtime — ленивая инициализация здесь и есть поведение;
  дефолт getattr достижим при первом вызове.
- `fold_proposed_check_event` getattr `ev.type/payload/seq`: `ev: Any`, fold —
  публичная функция свёртки, тело сознательно толерантно к произвольным
  event-like объектам (isinstance-guard по payload — контракт fail-closed).
- `proposed_check_decision_lock` / `_decision_lock_owners` getattr
  `_hitl_decision_locks` / `_hitl_decision_lock_owners`: карты создаются
  только лениво этим же модулем (нигде не объявлены) — дефолт достижим всегда
  при первом обращении.
- `_append_events` getattr `append_events` / `_project_local`: осознанный
  duck-typing fallback для runtime с голым `writer` (ветка
  `writer.append` + ручная свёртка); гарантии наличия методов нет.
- `_lookup_hitl_request` getattr `hitl`: атрибут есть ТОЛЬКО у experimental
  runtime (`HitlForkMixin`); на API/core runtime дефолт достижим — это
  переключатель между двумя хранилищами.
- `is_delegated_proposed_claim` / `is_proposed_claim` getattr
  `claim.effect`: `claim: Any`, вызовов в репо нет (CodeGraph) — гарантии
  атрибута нет ни для одного объекта.
- `propose_check` `threshold: Any`: поле `CheckProposal.threshold: Any`
  (произвольные JSON-значения) — Any здесь точный тип.
- Сигнатуры `runtime: Any` публичных функций: осознанный duck-typing
  (тест `test_fold_rejects_rejected_to_approved_transition` передаёт
  `SimpleNamespace`; experimental/API/core — три разных класса).
- `reject_check` `# type: ignore[arg-type]` на `source=source`: живой — mypy
  не сужает `Literal['hitl','api','timeout']` (после проверки `in`) до
  `ApprovalSource`; снятие ломает mypy, смена сигнатуры — смена API.
- `propose_check` `# type: ignore[arg-type]` на `predicted_direction`:
  живой — параметр публичного API аннотирован `str`, поле модели —
  `Literal["positive","negative","two_sided"]`; сужение параметра — смена API.
