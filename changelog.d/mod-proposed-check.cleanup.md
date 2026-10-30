`proposed_check`: убраны обёртки динамического доступа там, где вызывающий
типизирован `StudyRuntime`, и сужены типы приватных хелперов
(`coscientist/core/plan/proposed_check.py`). API, коды ошибок, сообщения и
журнальные форматы не менялись.

Убрано 3 getattr — только в функциях, чей `runtime` приходит от
типизированного вызывающего (`StudyRuntime`), где атрибут объявлен в
`__init__` (проверено CodeGraph callers/impact, включая тесты и фейки):
- `propose_check`: `runtime.study` — поле `Study | None` базового `__init__`;
  None по-прежнему обрабатывается следующей строкой (fail-closed ValueError);
- `propose_check`: `study.max_proposals` — объявленное поле модели `Study`
  (pydantic, default 3); `study` здесь уже сужен проверкой на None;
- `_close_task_proposal_quota`: `runtime.attempts` — объявленный словарь;
  функция приватная, единственный вызывающий — `propose_check`.

Оставлены байт-в-байт как в базе (ленивая инициализация / мягкий дефолт для
duck-typed runtime):
- `_proposals_map` getattr `_check_proposals`: через него идут публичные
  `proposal_count`, `get_proposal`, `fold_proposed_check_event`,
  `custom_checks_report` с `runtime: Any`; для объекта без атрибута база
  создаёт его лениво — снятие getattr дало бы AttributeError;
- `_lookup_hitl_decision` getattr `_hitl_decisions`: вызывается из
  `reject_check` и `_verify_human_principal` с `runtime: Any`; база
  возвращает None для объекта без атрибута — снятие дало бы AttributeError.

Сужены типы двух приватных хелперов с обобщённой аннотации до
`StudyRuntime` (минус 2 `Any`): `_task_has_result`, `_task_has_started_attempt`
— единственный вызывающий у обоих `propose_check` (CodeGraph), тела не
менялись (байт-в-байт как в базе).

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
