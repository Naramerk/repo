`proposed_check`: сужены типы двух приватных хелперов
(`coscientist/core/plan/proposed_check.py`). API, коды ошибок, сообщения и
журнальные форматы не менялись. getattr не удалялись.

Оставлены байт-в-байт как в базе (getattr с дефолтом на duck-typed runtime):
- `propose_check`: `getattr(runtime, "study", None)` и
  `getattr(study, "max_proposals", 3)`, `_close_task_proposal_quota`:
  `getattr(runtime, "attempts", {})` — `propose_check` вызывается из
  `coscientist/tools/propose_check_tool.py`, где `runtime` берётся из
  `ctx.extra` (`dict[str, Any]`), поэтому наличие атрибутов не доказано;
- `_proposals_map` getattr `_check_proposals`: через него идут публичные
  `proposal_count`, `get_proposal`, `fold_proposed_check_event`,
  `custom_checks_report` с `runtime: Any`; для объекта без атрибута база
  создаёт его лениво — снятие getattr дало бы AttributeError;
- `_lookup_hitl_decision` getattr `_hitl_decisions`: вызывается из
  `reject_check` и `_verify_human_principal` с `runtime: Any`; база
  возвращает None для объекта без атрибута — снятие дало бы AttributeError.


Сужены типы двух приватных хелперов с обобщённой аннотации до
`StudyRuntime` (минус 2 `Any`): `_task_has_result`, `_task_has_started_attempt`;
тела не менялись (байт-в-байт как в базе).

Оставлены маркеры (строка: маркер: почему нельзя снять без смены поведения):
- `_reviews_map` getattr `_proposed_check_reviews`: атрибут НЕ объявлен в
  `__init__` ни одного runtime — ленивая инициализация здесь и есть
  поведение; дефолт достижим при первом вызове.
- `fold_proposed_check_event` getattr `ev.type/payload/seq`: `ev` без точного
  типа, fold —
  публичная свёртка, тело сознательно толерантно к произвольным event-like
  объектам (isinstance-guard по payload — контракт fail-closed).
- `proposed_check_decision_lock` / `_decision_lock_owners` getattr
  `_hitl_decision_locks` / `_hitl_decision_lock_owners`: карты создаются
  только лениво этим же модулем — дефолт достижим всегда при первом
  обращении.
- `_append_events` getattr `append_events` / `_project_local`: осознанный
  duck-typing fallback для runtime с голым writer; гарантий наличия нет.
- `_lookup_hitl_request` getattr `hitl`: атрибут есть ТОЛЬКО у experimental
  runtime; на API/core дефолт достижим — это переключатель хранилищ.
  Fallback getattr `_hitl_requests` оставлен: строка несёт форму or-default,
  и её касание подсвечивается эвристикой --vs-main без допустимого способа
  обоснования (новые audit-ok запрещены); тело и обработка не менялись.
- `is_delegated_proposed_claim` / `is_proposed_claim` getattr
  `claim.effect`: `claim` без точного типа, вызовов в репо нет (CodeGraph) —
  гарантии
  атрибута нет ни для одного объекта.
- `propose_check` `threshold` с обобщённой аннотацией: поле
  `CheckProposal.threshold` тоже обобщённое (произвольные JSON-значения) —
  это точный тип поля.
- Сигнатуры `runtime: Any` публичных функций: осознанный duck-typing
  (тест fold передаёт SimpleNamespace; experimental/API/core — три класса).
- `reject_check` ignore[arg-type] на `source=source`: живой — mypy не сужает
  Literal-union после проверки вхождения до ApprovalSource (проверено: снятие
  ломает mypy), смена сигнатуры — смена API.
- `propose_check` ignore[arg-type] на `predicted_direction`: живой — параметр
  публичного API аннотирован str, поле модели — Literal; сужение — смена API.
- Строки с формами or-default/тернарников (`_task_has_result`,
  `_task_has_started_attempt`, `_validate_reviewer_attempt`, bind,
  `_lookup_hitl_request`, `_verify_human_principal`, schedule) оставлены
  байт-в-байт как в базе: их правка подсвечивает эвристику --vs-main
  (or-default/ternary на тронутых строках), а новые audit-ok и переписывание
  or-форм запрещены правилами чистого рефакторинга.
