# HITL-гейты: карта системы (read-only документация)

- Репо: `/Users/nargiza/CoScientist-next`, ветка `main` = `origin/main` = `196917a2f1a03301b4c640e4f79b4380fe9a36b1` (2026-10-27).
- Только то, что в коде; ссылки `файл:строка` даны по этому SHA.
- Слои: политика (`core/execution/domain.py`) → рантайм-гейты (`experimental/hitl.py`, `experimental/hitl_costly.py`, `core/plan/proposed_check.py`, `tools/gate.py`) → API/веб (`api/hitl.py`, `api/hitl_routes.py`, `api/web/js/hitl.js`).

## 1. Где живёт механика

| Слой | Файл | Что делает |
|---|---|---|
| Политика | `coscientist/core/execution/domain.py:110-191` | `HitlGateKind` (12 видов), `GateConfig` (enabled/timeout_s/on_timeout), `_default_gates()`, `HitlPolicy` (mode, gates, defaults, `gate(kind)` c alias plan↔plan_approval), `HitlPrompt` / `HitlRequest` / `HitlDecision` |
| Рантайм | `coscientist/experimental/hitl.py:52-513` | `open_hitl` / `answer_hitl` / `_hitl_timeout` / `_critic_decide`; только experimental-рантайм умеет HITL |
| Costly | `coscientist/experimental/hitl_costly.py:89-270` + `experimental/costly.py:49` | `costly_or_nonreal`: approve/reject/delegate/timeout против `DelegatedBudget` |
| Proposed check | `coscientist/core/plan/proposed_check.py:495-530` | `propose_check` пишет `CheckProposed` + `HitlOpened kind=proposed_check` (`payload_ref=proposal_id`) |
| Side-effect gate | `coscientist/tools/gate.py` + `tools/effects.py:791-807` | статический gate по политике, журнал-гейт не открывает |
| API-команды | `coscientist/api/hitl.py` | `HitlOwner` (inbox владельца запуска), `AnswerBody`, интерактивный plan_approval + его timeout → cancel |
| API-роуты | `coscientist/api/hitl_routes.py` | `GET /console/studies/{sid}/hitl`, `POST /studies/{sid}/hitl/{rid}` |
| Веб | `coscientist/api/web/index.html:25,121-123`, `api/web/js/hitl.js`, `api/web/js/routes.js` | вкладка «Решения человека» |
| Guard | `coscientist/core/execution/hitl_guard.py` + `recover.py:32`, `runtime_factory.py:67,96` | core-рантайм отказывается открывать журнал с открытыми HITL-гейтами → `HitlUnsupported` |

## 2. Таблица гейтов (12 видов из `HitlGateKind`)

| Gate | Когда открывается | Кто отвечает | timeout (дефолт) | Молчание (timeout) | Отказ (reject) | Открытие в prod-коде |
|---|---|---|---|---|---|---|
| `plan_approval` | После planner-цикла, до `accept_plan`, только если есть control (HitlOwner) — интерактивный API-запуск (`api/run.py:288`, `cli/run.py:2200-2203`) | Человек (вкладка «Решения человека») | 180 s | Interactive: `HitlTimedOut` + **cancel_study("hitl-plan-timeout")** — исследование отменяется, делегирования нет (`api/hitl.py:115-125`). Experimental-путь (не owner): `on_timeout=delegate` → critic принимает, `delegated_by=timeout` | `HitlDecided(reject, by=human)` → `cancel_study("human-plan-rejected")` (`api/hitl.py:148-149`) | `api/hitl.py:98` (`review_plan`, с `prompt`) |
| `plan` | Альтернативное имя plan_approval (alias в `HitlPolicy.gate`, `domain.py:183`); открытие — только через `runtime.open_hitl(..., kind="plan")` | Человек | 180 s | как plan_approval (делегирующий список включает) | → `ExpansionRefused(invalid_hitl)` при чужом kind | нет prod-вызова (API рантайма) |
| `plan_expansion` | Offer расширения плана; `expand_after_task` биндится в `open_hitl` (`experimental/hitl.py:74-75`) | Человек | 180 s | delegate → critic accept (`delegated_by=timeout`) | план-правка → `PlanExpanded` / `ExpansionRefused` | нет prod-вызова (API рантайма) |
| `spec_approval` | Утверждение спецификации (нет вызова в prod; виден в delegation-списке и legacy `timeout_s`) | Человек | 120 s | delegate → critic accept | — | нет prod-вызова |
| `hypotheses` | Утверждение гипотез (API рантайма; в delegation-списке) | Человек | 1800 s | delegate → critic accept | — | нет prod-вызова |
| `costly_or_nonreal` | `runtime.open_hitl(..., kind="costly_or_nonreal", cost_rub=...)` перед дорогим/нереальным действием; в prod-рантайме открывается только явно (сценарии/тесты: `tests/experimental/test_costly.py`) | Человек | 1800 s | `on_timeout=fail` → `TaskDone(FAILED, human_timeout)` + зависимые задачи отменяются `input_unavailable` (`experimental/hitl_costly.py:243-270`); `delegate` → capped делегированным бюджетом (нет бюджета → `WAIT/keep_open` с `human_message`) | `answer=reject` → `SKIPPED(human_rejected)`; gate остаётся в журнале | `experimental/hitl.py:52` (API рантайма) |
| `report` | Утверждение отчёта (API рантайма; в delegation-списке) | Человек | 1800 s | delegate → critic accept | — | нет prod-вызова |
| `result_acceptance` | Приёмка результата (нет prod-вызова; `defaults: accept_computed`) | Человек | 1800 s | Только `HitlTimedOut` — kind НЕ в delegation-списке `experimental/hitl.py:457-465`, side-эффекта нет (заявленный `accept_computed` этим путём не применяется) | — | нет prod-вызова |
| `skip_task` | Пропуск задачи (нет prod-вызова; `defaults: deny`) | Человек | 1800 s | Только `HitlTimedOut` (kind не в delegation-списке) | — | нет prod-вызова |
| `budget` | Чекпоинт долей бюджета, `expand_after_task`; «реши сам» → critic (`experimental/hitl.py:140-151`); в prod открывается только явно (`tests/test_pr12_budget_share.py:472`) | Человек (или critic при delegate) | 1800 s | Только `HitlTimedOut` (kind не в delegation-списке) — side-эффекта нет | accept → `BudgetSharesAdjusted`; невалидные доли → `BudgetSharesRefused(critic_rejected)` (`experimental/hitl.py:651-699`) | API рантайма |
| `side_effect` | Журнал-гейт НЕ открывается. Статический gate: write/external_effect tool вызов проверяет `side_effect_allowed(ctx.hitl_policy)` (`tools/effects.py:791`) | Политика (не человек в M1) | 1800 s (не тикает) | не применимо — запрещено до выполнения: `EffectDenied(reason="side_effect_deny")` + `ToolError(local)` | allow только как операторский `--coder-backend` для coder-ctx (`cli/run.py:1333-1344`, scope локальный, в политику исследования не пишется) | нет (`tools/gate.py:12-33`) |
| `proposed_check` | Tool `propose_check` пишет `CheckProposed` + `HitlOpened(kind=proposed_check, payload_ref=proposal_id)` (`core/plan/proposed_check.py:495-530`) | Человек (accept/delegate/reject) | 1800 s | Timeout ≡ **reject**: `reject_check(source="timeout", reason="human_timeout")` под per-proposal lock — никогда не авто-approve (`experimental/hitl.py:399-439`) | `CheckRejected`; `delegate` → отдельный reviewer (`schedule_proposed_check_review`, `effect=WAIT_REVIEWER`); ретаргет на другой proposal → ValueError | `core/plan/proposed_check.py:518` |

Дополнительно: `HitlDecision` (`domain.py:778-785`) — `accept|revise|reject|delegate`, `by: human|policy`, `approved_by: human|critic`, `delegated_by: human|timeout`.

## 3. Дедлайны и тикер

- Дедлайн запроса: `min(now + gate.timeout_s, task.deadline, study.effective_deadline())` (`experimental/hitl.py:78-82`); то же для proposed_check (`proposed_check.py:495-502`).
- Тикер: `ExperimentalRuntime.tick()` — по каждому открытому гейту past deadline → `_hitl_timeout(h)`; также `reject_stale_proposed_check_reviews` (делегированный reviewer timeout ≡ reject) (`experimental/runtime.py:130-143`).
- Инвариант: `HitlRequest.deadline <= study.effective_deadline()` (`experimental/runtime.py:121-127`).
- Продление при простое: `recover_after_idle(grace_s=60)` пишет `HitlDeadlineExtended` в пределах task/study дедлайнов (`experimental/hitl.py:720-752`).
- `HitlTimedOut` идемпотентен (`make_idem`), повторный тик не дублирует эффект.

## 4. Семантика on_timeout (из кода)

- `OnTimeout = delegate | delegate_capped | fail` (`domain.py:109`).
- `experimental/hitl.py:_hitl_timeout`: пишет `HitlTimedOut(reason=human_timeout)`; затем только для kinds `(plan, plan_approval, plan_expansion, spec_approval, hypotheses, report)`: `delegate`/`delegate_capped` → `_critic_decide(decision="accept", delegated_by="timeout")` (событие `HitlDecided(by=policy, approved_by=critic)` + пометка plan-ноды `delegated_by/approved_by` — требует записанный plan с Need по hypotheses и literature, иначе `HitlPlanDelegateRefuse`); `fail` → `TaskDone(FAILED, human_timeout)` + отмена зависимых.
- Для `result_acceptance` / `skip_task` / `budget` timeout-путь ограничивается `HitlTimedOut` (без side-эффектов).
- Два особых пути: `costly_or_nonreal` (costly_gate, см. таблицу) и `proposed_check` (reject под локом, idем-сверка журнала перед эффектом).

## 5. Зависимость от режима (`HitlPolicy.mode`)

- `mode` — обязательный `--hitl-mode {interactive,auto,headless}` в CLI (`cli/run.py:461-467`), без дефолта (`cli/run.py:592-593`); в API — `RunStudyBody.hitl_mode` (`api/run.py:104`).
- **interactive**: API-сервер создаёт `HitlOwner` (`api/run.py:288-289`) → при план-ревью открывается `plan_approval` с `prompt`; timeout = отказ → cancel (не делегирование: «interactive silence is refusal, never implicit human approval», `api/hitl.py:116-117`). CLI с `control=None` гейт не открывает вовсе (план принимается без человека).
- **auto / headless**: `HitlOwner` не создаётся → интерактивного plan_approval нет; critic-путь плана продолжается без человека. `side_effect`: `ask` схлопывается в `deny` (`tools/gate.py:26-27`); дефолт политики `side_effect: deny` — любой write/external_effect tool → `ToolError(local)`.
- `_coder_hitl_policy` (`cli/run.py:1333`): для coder-диспетча боксовая копия политики с `side_effect: allow` — операторское решение через `--coder-backend`, действует только в этом ctx.
- Ядро vs experimental: core-рантайм (`recover.py:32`, `runtime_factory.py:67,96`) при открытом HITL-гейте в журнале поднимает `HitlUnsupported`; HITL-журналы обслуживает только experimental-рантайм (`hitl_guard.py:38-44`; `keep_open`/`effect=WAIT` оставляют гейт открытым).

## 6. Веб: где видно (8487 /ui/)

- Сервер: `python -m coscientist.api.serve --port 8487 ...` (порт — обязательный флаг; 8487 использовался в `docs/plan/handover.md:25-28`). UI смонтирован на `/ui` (`api/console.py:240`), `/` → redirect `/ui/`.
- Вкладка **«Решения человека»** (`data-tab="hitl"`, `api/web/index.html:25,121-123`) — пер-study таб (`api/web/js/routes.js:2`).
- Данные: `GET /console/studies/{sid}/hitl` (`hitl_routes.py:38-47`) — фолд журнала: `request_id`, `kind`, `status: pending|answered|timed_out|study_closed`, **`opened_seq`** (верхнеуровневое поле, seq закоммиченного `HitlOpened`), `deadline`, `answer`, `answer_study_id`, `can_answer`, и вложенный **`prompt`**: `{question, options, context_json, context_sha256, attempt_id}` — **context_sha256 лежит внутри prompt, не в корне строки**.
- Карточки: вопрос из `prompt.question`, статус («Ожидает ответа» / «Решение записано» / «Время ожидания истекло» / «Исследование закрыто»), раскрываемая точная запись (context_json + SHA-256 + attempt_id), сводка плана (`api/web/js/hitl.js:37-77`).
- Ответ: кнопки по `prompt.options` («Принять план»/«Отклонить план»), активны только при `can_answer` (живой interactive-владелец ждёт); `POST /studies/{answer_study_id}/hitl/{request_id}` с телом `{decision, opened_seq: row.opened_seq, context_sha256: row.prompt.context_sha256}` (`api/web/js/hitl.js:82-99`). Нет владельца → «Владелец интерактивного запуска недоступен. Ответ не отправлен.»
- Автообновление: WS-поток событий — панель рефрешится на любых `Hitl*`/`StudyFinalized` (`api/web/js/app.js:191`).

## 7. API-эндпоинты и защита от устаревшего контекста

- `GET /console/studies/{study_id}/hitl` — строки из журнала (только чтение, `hitl_routes.py:38`).
- `POST /studies/{study_id}/hitl/{request_id}` — `AnswerBody` strict/frozen: `decision: accept|reject`, `opened_seq: int>0`, `context_sha256: ^[a-f0-9]{64}$` (`api/hitl.py:26-31`); 409 при `HitlRefuse`, 400 при прочем `ApiRefuse`.
- Проверки владельца (`api/hitl.py:_apply_answer`): `opened_seq` == seq закоммиченного `HitlOpened` и `context_sha256` == `prompt.context_sha256`, иначе 409 `hitl_stale_context`; дедлайн прошёл → `hitl_request_expired`; нет ожидающего владельца → `hitl_live_owner_not_waiting`/`hitl_owner_not_waiting`; закрытый owner → `hitl_owner_closed`. Ответ применяется корутиной владельца (единственный писатель), отключение браузера не отменяет уже поставленную в очередь команду (`asyncio.shield`).
- Целостность `HitlPrompt` (`domain.py:747-760`): валидатор пересчитывает `sha256(context_json)` и сверяет с `context_sha256` (расхождение → `hitl_prompt_context_digest_mismatch`); options — непустой набор без дублей из `accept|reject`.
- Ответы идёт только в lease-owning run (`hitl_routes.py:1`, `api/run.py:341-355`); replan-ребёнок адресуется через `answer_study_id`.

## 8. Дефолты политики (текущие, из кода)

`GateConfig` по умолчанию: `enabled=True, timeout_s=1800, on_timeout="delegate"` (`domain.py:126-132`).

| gate | timeout_s | on_timeout | Примечание |
|---|---|---|---|
| spec_approval | 120 | delegate | legacy `timeout_s`: 120 |
| plan | 180 | delegate | alias plan_approval |
| plan_approval | 180 | delegate | legacy: 180; интерактивный owner переопределяет исход (cancel) |
| plan_expansion | 180 | delegate | |
| hypotheses | 1800 | delegate | |
| costly_or_nonreal | 1800 | **fail** | + `HitlPolicy.defaults`-независимый fail |
| report | 1800 | delegate | |
| result_acceptance | 1800 | delegate | `defaults: accept_computed` (декларация; timeout-путь её не применяет) |
| skip_task | 1800 | delegate | `defaults: deny` (декларация) |
| budget | 1800 | delegate | |
| side_effect | 1800 | **fail** | `defaults: deny`; ask→deny вне interactive |
| proposed_check | 1800 | **fail** | timeout ≡ reject |

`HitlPolicy.defaults` (`domain.py:168-178`): `plan_approval: human_timeout`, `result_acceptance: accept_computed`, `skip_task: deny`, `side_effect: deny`, `proposed_check: reject`. Реально потребляется кодом только `side_effect` (`tools/gate.py:22`) и coder-переопределение (`cli/run.py:1342`); остальные — декларативные метаданные политики.

`HitlPolicy.gate(kind)` (`domain.py:180-191`): берёт gates-таблицу → alias plan↔plan_approval → иначе `GateConfig(timeout_s=legacy timeout_s или 1800, on_timeout=fail для costly_or_nonreal, иначе policy.on_timeout)`.

## 9. Что открывается в проде сегодня (сводка)

1. **plan_approval** — единственный гейт, который открывает прод-флоу с человеком (интерактивный API-запуск), с `prompt` и защитой opened_seq/context_sha256; timeout → cancel исследования.
2. **proposed_check** — открывается самим tool `propose_check`; timeout ≡ reject; делегирование → отдельный reviewer.
3. **side_effect** — журнала-гейта нет, статическое deny/allow по политике (+ coder-allow через `--coder-backend`).
4. Прочие kinds (`spec_approval`, `plan`, `plan_expansion`, `hypotheses`, `costly_or_nonreal`, `report`, `result_acceptance`, `skip_task`, `budget`) — конфигурируемые и обрабатываемые рантаймом (open/answer/timeout/expand), но в prod-коде не открываются (только API рантайма / тесты).

## 5. v2 — цепочка HITL как в старом (требование хозяина 2026-10-28)

Статус: дизайн утверждён хозяином в разговоре («хитлы в самом начале: на ТЗ,
на корзины (гипотезы/эксперимент/литература), мб даже на репорт + писать что
не так, а не только accept/decline»). Реализация — ветка lab/hitl-stage-gates.

Гейт-цепочка интерактивного API-прогона (hitl_mode=interactive):

| # | Гейт | Момент открытия | Содержимое prompt | Ответы человека |
|---|------|-----------------|-------------------|-----------------|
| 1 | `spec_approval` | после SPEC-стадии, до PLAN | ТЗ: вопросы/задачи-черновики ResearchSpecOut, needs | accept / revise(+текст) / reject(+текст) |
| 2 | `hypotheses` | после HYP-стадии (когда need.needed=true) | гипотезы HYP-n с предсказаниями/ранками | accept / revise(+текст) / reject(+текст) |
| 3 | `plan_approval` | как сейчас, после PLAN | план | accept / reject(+текст) — reject с текстом = replan с учётом текста |
| 4 | `literature` | после LIT-стадии (когда need.needed=true) | найденные статьи + привязки к приёмкам | accept / revise(+текст) / reject(+текст) |
| 5 | `report` | после черновика отчёта, до StudyFinalized | научный отчёт (report.science.md) | accept / revise(+текст) / reject(+текст) |

Семантика текста («писать что не так»):
- `revise` + feedback: стадия получает РЕМОНТ-КОЛБЭК — повторный запуск роли с
  текстом человека как фактом (как schema-repair, bounded rounds: 1 ремонт,
  потом честный typed отказ). Новая попытка НЕ сжигает attempt задачи стадии
  сверх лимита (тот же attempt, новые messages).
- `reject` + feedback: то же, что revise, но человек вправе после исчерпания
  ремонта получить reject всего исследования (status quo для plan).
- `accept` + feedback: принимается, текст пишется в HitlDecided как
  замечание-комментарий (не блокирует, попадает в отчёт как «замечания человека»).
- Таймауты/делегирование — как в §2 (по умолчанию spec/hyp/lit/report:
  enabled, timeout 1800s, on_timeout=delegate → критик).

AnswerBody: + `feedback: str | None` (0..4000), обязателен при revise; API
`POST /studies/{sid}/hitl/{rid}`; веб-форма: textarea под кнопками.

Порядок работ: (1) контракт AnswerBody+HitlDecision.feedback и ремонт-механика
стадий; (2) открытие гейтов в API-пути (spec → plan → hyp → lit → report);
(3) веб-форма. Всё на одной ветке, один автор.
