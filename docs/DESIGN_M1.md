# CoScientist-next: дельта дизайна M1 относительно v0.4

База: `docs/DESIGN.md` (D) и `docs/RESEARCH_MODEL.md` v0.4 (RM), ветка main, c65f680. Обоснование дыр: `STRESS_MATRIX.md`. Ничего не запушено. Изменения в `graph_schema.yaml` даны как поля; таблицы RM регенерирует `gen_model_tables.py`.
Ревизия 5: источник вопросов и адресов для PR 2 (Plan.questions, TaskSpec.addresses), база main a55b0a1. Ревизия 4 (ревью брифа rev 3): порядок P5/триггер (б), проекция question→tasks, InputBound убран, факт PR 1 (#5). Ревизия 3 (после ревью rev 2): блокеры PR 2/3 №1–5, (a) terminal_reason от триггера, (b) синхронизация study_view↔D §3.4, (c) LoopSpec убран из PR 3, мелочи. Ревизия 2: условия 1–7, рёбра, B1–B5. **База кода — main 49c7dd7** (не be6310d).

Принцип: минимальная дельта, которая закрывает дыры. Каждое изменение: поля/события → инварианты → тесты. Порядок PR — в конце. Пометка **[спорно]** означает, что я расхожусь с рекомендацией ревью или считаю пункт избыточным.

---

## 0. Блокеры (B1–B5)

### 0.1 TaskReason (B1)

```python
class TaskReason(StrEnum):
    FAILED = "failed"                    # исчерпаны попытки / не retryable
    DEADLINE = "deadline"                # task.deadline (P5), outcome FAILED
    STUDY_DEADLINE = "study_deadline"    # study.deadline − reserve (P6), outcome SKIPPED
    CRASHED = "crashed"                  # close_reason попытки при recover (не TaskDone)
    CANCELLED = "cancelled"
    BUDGET = "budget"
    INPUT_UNAVAILABLE = "input_unavailable"
    HUMAN = "human"                      # P7 skip_task по решению человека
    HUMAN_REJECTED = "human_rejected"
    CRITIC_REJECTED = "critic_rejected"
    HUMAN_TIMEOUT = "human_timeout"
    CATALOG_ERROR = "catalog_error"
    SCHEMA_INVALID = "schema_invalid"
    INVALID_STATUS = "invalid_status"
    RETRY_SCHEDULED = "retry_scheduled"  # только у PENDING (P3); не терминальная
    SUPERSEDED = "superseded"; EXPANSION_LIMIT = "expansion_limit"; ITERATION_CAP = "iteration_cap"; REFUSED = "refused"  # PR 9/11; в PR 1 только объявить
class CloseReason(StrEnum):  # Attempt.close_reason
    RESULT = "result"; FAILED = "failed"; CRASHED = "crashed"; DEADLINE = "deadline"; STUDY_DEADLINE = "study_deadline"
    CANCELLED = "cancelled"; INPUT_UNAVAILABLE = "input_unavailable"; HUMAN_TIMEOUT = "human_timeout"; BUDGET = "budget"
```

Сопоставление со строками в main 49c7dd7 (найдены по `coscientist/**`):

| Строка | Где | Поле | → enum |
|---|---|---|---|
| `"failed"` | core/attempts.py:126,163 | close_reason, TaskDone.reason | CloseReason.FAILED, TaskReason.FAILED |
| `"deadline"` / `"study_deadline"` | core/machine.py:140–174 | оба | DEADLINE / STUDY_DEADLINE |
| `"crashed"` | core/recover.py:56,65 | close_reason | CloseReason.CRASHED |
| `"retry_scheduled"` | core/projections.py:61; recover.py:79 | Task.reason (PENDING) | RETRY_SCHEDULED |
| `"cancelled"` | core/attempts.py:195; experimental/hitl_costly.py:30 | оба | CANCELLED |
| `"input_unavailable"` | core/attempts.py:87; core/inputs.py:37; experimental/hitl_costly.py:264 | оба | INPUT_UNAVAILABLE |
| `"catalog_error"` | core/executor.py:163 | Result.reason, TaskDone.reason | CATALOG_ERROR |
| `"result"` | core/executor.py:300 | close_reason | CloseReason.RESULT |
| `"human_timeout"` | experimental/hitl.py:125; costly.py:81; hitl_costly.py:249 | TaskDone.reason | HUMAN_TIMEOUT |
| `"budget"` / `"human_rejected"` / `"critic_rejected"` | experimental/hitl_costly.py:211–229 | TaskDone.reason | BUDGET / HUMAN_REJECTED / CRITIC_REJECTED |
| `"attempt_closed"` | core/executor.py:110 | payload `LateWriteRejected` | не TaskReason (оставить строкой события) |
| `"required_checks_pending"` | core/executor.py:179 | payload события | не TaskReason |
| `"downgrade requires reason and refs"` | core/executor.py:142 | текст ошибки | не TaskReason |
| `"answered"`, `"delegation_disabled"`, `"delegated_costly_exhausted"`, `"critic:{mode}"`, `"timeout→agent (…)"`, `"delegate→agent (…)"` | experimental/hitl.py:103,161; costly.py:69–111 | `HitlDecided.reason` | **не** TaskReason: отдельный `HitlReason` (свободная строка с префиксом из enum `answered/delegation_disabled/delegated_costly_exhausted/critic/timeout_agent/delegate_agent`), PR 1 не трогает experimental |
| `"human"` (P7), `"schema_invalid"`, `"invalid_status"` | в D §3.1, §3.3, §4.1, в коде main не встречаются | — | объявить в enum |

Агенту: перед PR 1 повторить `grep -rnE 'reason|close_reason' coscientist/` на базе и дописать в таблицу всё, чего в ней нет.

Миграция: `Task.reason: TaskReason | None`, `Attempt.close_reason: CloseReason | None`. Старые журналы: upcaster `TaskDone` v1→v2 и `AttemptClosed` v1→v2 — строка → enum; неизвестная строка → `ValueError` при replay (тест на все строки из таблицы).

### 0.2 TerminalReason — от триггера финализации (B2, ревизия 3)

Приоритет по флагам (rev 2) заменён. `terminal_reason` определяется **триггером**, который запустил finalize, и записывается в payload `StudyFinalized` вместе с `trigger`:

| trigger | terminal_reason |
|---|---|
| `cancel` — событие `StudyCancelled` (новое, PR 2, §1) | `cancelled` |
| `budget` — P6 по `BudgetExceeded(study)` закрыл задачи | `budget` |
| `deadline` — `now ≥ study.deadline − finalize_reserve_s` | `deadline` |
| `all_done` — все задачи DONE естественно | по порядку: `human_timeout` (≥1 required DONE(FAILED, human_timeout)) → `infeasible` (≥1 question с disposition ∈ {infeasible, ill_posed} и ни одного answered/refuted/survived/inconclusive; **not_attempted не считается** — исправление M1:70) → `completed` |
| (intake, PR 4) | `refused` |

- `contributing_reasons: list[TaskReason]` — все прочие причины, встретившиеся в study (budget, deadline, human_timeout, expansion_limit…), без повторов, **по seq первого появления**.
- Тай-брейк, если в одном тике сработали несколько триггеров: `cancelled > budget > deadline` (all_done проигрывает любому).
- `expansion_limit` не триггер: остаётся только в contributing_reasons (и в disposition reason), пока PR 9 не решит иначе.
- `iteration_cap` в terminal_reason не входит (§4).
- Тесты (вместо 2^5): `test_terminal_reason_trigger_cancel`, `…_budget`, `…_deadline`, `…_all_done_completed`, `…_all_done_human_timeout`, `…_all_done_infeasible`, `test_all_done_not_attempted_is_completed_not_infeasible`, `test_same_tick_tiebreak` (cancel+budget+deadline в одном тике → cancelled), `test_contributing_reasons_ordered_by_seq`.

### 0.3 Минимальный отчёт (B3)

JSON Schema (draft 2020-12), хранится в `coscientist/schema/minimal_report.schema.json`:
```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,
 "required":["study_id","terminal_reason","trigger","contributing_reasons","summary","fallback_reason","verdict","tasks","questions","sections","truncated"],
 "properties":{
  "study_id":{"type":"string"},
  "terminal_reason":{"enum":["refused","human_timeout","budget","deadline","infeasible","cancelled","completed"]},
  "trigger":{"enum":["cancel","budget","deadline","all_done","intake"]},
  "contributing_reasons":{"type":"array","items":{"type":"string"},"maxItems":20},
  "summary":{"type":"string","maxLength":1000},
  "fallback_reason":{"type":["string","null"],"maxLength":200},
  "refusal":{"type":["string","null"]},
  "verdict":{"enum":["fulfilled","partial","stubbed","failed"]},
  "tasks":{"type":"array","items":{"type":"object","additionalProperties":false,
     "required":["task_key","kind","outcome","reason","provenance"],
     "properties":{"task_key":{"type":"string"},"kind":{"type":"string"},
       "outcome":{"enum":["SUCCESS","PARTIAL","FAILED","SKIPPED"]},
       "reason":{"type":["string","null"]},"provenance":{"enum":["real","partial","stub"]}}}},
  "questions":{"type":"array","items":{"type":"object","additionalProperties":false,
     "required":["question_id","formulation","disposition","reason"],
     "properties":{"question_id":{"type":"string"},"formulation":{"type":"string","maxLength":500},
       "disposition":{"enum":["answered","refuted","survived","inconclusive","needs_more_data","infeasible","ill_posed","not_attempted"]},
       "reason":{"type":"string","maxLength":200}}}},
  "sections":{"type":"object","additionalProperties":false,"required":["real","stub","human"],
     "properties":{"real":{"type":"array","items":{"type":"string"}},
       "stub":{"type":"array","items":{"type":"string"}},
       "human":{"type":"array","items":{"type":"string"}}}},
  "truncated":{"type":"boolean"}}}
```
`summary` — детерминированная строка (шаблон: «Study закрыто: {terminal_reason}; задач {n} (SUCCESS a, PARTIAL b, FAILED c, SKIPPED d); вопросов {m}, not_attempted {k}»), без LLM. `fallback_reason` — класс исключения основного пути или null. `real` — task_key с SUCCESS/PARTIAL и provenance=real; `stub` — provenance ∈ {partial, stub}; `human` — FAILED/SKIPPED с reason (D §3.4 три раздела). Сериализация — JCS; размер ≤ 10 240 байт; при превышении усекаются `formulation`, затем хвосты списков `tasks`, `questions`, затем `sections`, `truncated=true`; `summary` и счётчики в нём не усекаются, поэтому покрытие вопросов (I2) сохраняется числом.

### 0.4 Conclusion на каждый question (B4)

Каждый question получает ровно один conclusion (`answers` in-кардинальность question = 1..1 после финала), включая not_attempted. Плюс один conclusion → study. Исправлено в §1 (было M1:15 «по одному на корневой question»).

### 0.5 Инвариант дедлайнов и резерв (B5)

- `Study.finalize_reserve_s: int` — из профиля, по умолчанию 30 с. В `create_study` (core/lifecycle.py): `duration = deadline − now`; если `duration ≤ finalize_reserve_s` → `ValueError` (до любых событий). Intake превращает это в Refuse.
- `effective_deadline = study.deadline − finalize_reserve_s`. Инвариант (расширяет `invariant_deadlines` в StudyRuntime): каждый deadline задачи, попытки, HITL-запроса, job ≤ `effective_deadline`.
- Задача plan: сейчас `min(now + 600 s, dl)` (core/lifecycle.py:96) → `min(now + 600 s, effective_deadline)`. Задача report (lifecycle.py:118 `dl`) → `effective_deadline`.
- HITL: `deadline = min(now + timeout, task.deadline, effective_deadline)` (experimental/hitl.py:38); `HitlDeadlineExtended` (hitl.py:217) обрезается тем же. В PR 1 core-изменение; experimental меняется отдельным коммитом того же PR (core его не импортирует).
- M0-тесты: property-тест рисует duration 20–120 с (tests/test_deadline_property.py:73). Он передаёт `finalize_reserve_s=5` при create_study; `check_finally_closed` (+35 с) сохраняется. Тесты, где явно сравнивается deadline задачи с study.deadline, переписать на `effective_deadline`.
- Тест: `test_create_study_rejects_duration_le_reserve`, `test_all_deadlines_le_effective`, `test_plan_deadline_clipped_by_reserve`.

## 1. Финализатор ядра (H-a, H-h, X2) — главное

**Что.** Финализатор — функция ядра `finalize(study_id)`, а не задача DAG. Она пишет событие `StudyFinalized`. Без LLM, Budget и HITL.

**Фазы study** (`StudyView.phase`): `running | finalizing | closed`.
- `running` — есть не-DONE задачи и `now < study.deadline − finalize_reserve_s`.
- `finalizing` — все задачи DONE (или наступил порог дедлайна), а `StudyFinalized` ещё нет. Промежуточного «closed без финала» нет.
- `closed ⇔ ∃ StudyFinalized`. Старое правило D §3.2 («closed ⇔ все задачи DONE»; в коде `coscientist/core/study_view.py`, ветка `phase="closed"`) удаляется в PR 2.

**Отмена study (новое, PR 2).** В core на 49c7dd7 нет отмены уровня study. Добавляется команда `cancel_study(study_id, by)` → событие `StudyCancelled{by, at}` (idem_key `StudyCancelled:<study_id>`). P6-подобное правило закрывает все не-DONE задачи `DONE(SKIPPED, cancelled)` (открытые попытки — `AttemptClosed(cancelled)`), затем finalize с trigger=cancel. HTTP-ручка — вне M1 (api не трогаем).

**Триггер** (код, через очередь писателя D §3.5a; проверяется в `tick()` и в `recover`):
(0) `StudyCancelled` → trigger=cancel; (0') P6 по budget → trigger=budget; (а) все задачи DONE → trigger=all_done;
(б) `now ≥ study.deadline − finalize_reserve_s` → P6 закрывает все не-DONE задачи `DONE(SKIPPED, study_deadline)` (открытые попытки — `AttemptClosed(study_deadline)`), затем finalize с trigger=deadline.
**Порядок в одном тике (rev 4).** Дедлайны задач обрезаны до `effective_deadline`, поэтому при `now ≥ effective_deadline` одновременно срабатывают P5 (task.deadline) и (б). **(б) обрабатывается первым**: задачи с `deadline == effective_deadline` получают `DONE(SKIPPED, study_deadline)`, а не `DONE(FAILED, deadline)`. P5 срабатывает только для задач с `deadline < effective_deadline`. Тест `test_p5_vs_study_deadline_same_tick`.
После долгого простоя recover на первом тике делает (б), и отчёт пишется даже тогда, когда все дедлайны уже истекли.

**Тотальность.** `finalize` не может не записать событие:
1. Основной путь: `render_report(projection)` → блоб в CAS → `artifact(role=report)`; узлы `conclusion` (по одному на **каждый** question, B4) и `report`.
2. Если шаблон, CAS или построение узлов бросает исключение (любое), пишется `StudyFinalized` с `minimal_report` **внутри payload** (JSON по схеме B3, ≤ 10 КБ после JCS; при превышении — усечение списков с полем `truncated: true`) и `report_artifact_id=null`, `fallback_reason=<класс исключения>`. Тест: `test_finalize_total_on_render_failure`, `test_finalize_total_on_cas_failure`, `test_minimal_report_le_10kb`.
3. `StudyFinalized` идемпотентен: idem_key `StudyFinalized:<study_id>`.

**Payload** `StudyFinalized{trigger, terminal_reason, contributing_reasons[], verdict, question_dispositions: {qid: {disposition, reason}}, report_artifact_id | null, conclusion_ids[], minimal_report | null, fallback_reason | null}`. `question_dispositions` вычисляет **та же** чистая функция `dispositions(projection)`, что и `StudyView`. Тест `test_finalized_dispositions_replay`: replay журнала → `dispositions(projection)` == payload.

**LLM-проза.** Задача `report` остаётся в DAG как `required=False` и пишет только `ReportProse`. Шлюз HITL `report` относится только к прозе: «вернуть на доработку» перезапускает прозу и никак не блокирует finalize. Если проза SUCCESS до finalize — встраивается, иначе отчёт без неё.

**Схема.** `answers`: добавляется пара `conclusion → study` (0..1) — conclusion уровня study (итог и terminal_reason) — **в дополнение** к conclusion на каждый question (B4: каждый question имеет ровно 1 входящее answers от conclusion; при 0 question есть только conclusion→study). `based_on` — в `graph_schema.yaml` кардинальность просто **0..n** (схема не умеет условий). Условие «≥1 based_on, если disposition ∈ {answered, refuted, survived}» проверяется **кодом** в finalize (и тестом `test_based_on_required_for_counted_dispositions`; в PR 2 таких disposition нет — тест пишется на будущее как xfail в PR 3). Для not_attempted conclusion ссылается на reason текстом, based_on пуст.

**Инварианты.** I1: `closed ⇔ ∃ StudyFinalized`. I2: `closed ⇒ ∀ question: (∃! conclusion −answers→ question) ∨ (question_id ∈ minimal_report.questions ∨ minimal_report.truncated ∧ учтён в summary)`; при основном пути — conclusion у каждого, при fallback — покрытие minimal_report. I3: после StudyFinalized store **отклоняет** любую запись по study (исключение `StudyClosedError`); факт отклонения только логируется и считается метрикой `late_write_rejected_total{event_type}` — **событием не пишется**. Оговорки «кроме fork» нет: fork — это новое study с новым study_id. (Строка-событие `LateWriteRejected` из таблицы §0.1 — для попыток внутри живого study; не путать.) I4: finalize не читает Budget/HITL. I5 (B5): см. §0.5.

**Тесты.** `test_finalize_on_all_done`, `test_finalize_at_deadline_minus_reserve`, `test_finalize_after_long_downtime`, `test_finalize_budget_zero`, `test_finalize_zero_questions`, `test_report_task_failed_still_report`, `test_phase_finalizing_between_done_and_finalized`, `test_conclusion_per_question`, плюс тесты тотальности выше.

## 2. terminal_reason и disposition вопроса (X3, K8, строки 6, 8, 13)

```python
class TerminalReason(StrEnum):   # уровень study; приоритет — §0.2
    REFUSED; HUMAN_TIMEOUT; BUDGET; DEADLINE; EXPANSION_LIMIT; INFEASIBLE; CANCELLED; COMPLETED
class RefusalKind(StrEnum): SAFETY; OUT_OF_SCOPE; EMPTY_REQUEST; UNSUPPORTED_OUTPUT; BUDGET_ZERO; CONTRADICTORY; EGRESS_POLICY; INPUT_TOO_LARGE; DURATION_TOO_SHORT
class Disposition(StrEnum):
    ANSWERED; REFUTED; SURVIVED; INCONCLUSIVE             # вычисляемые (из вердикта)
    NEEDS_MORE_DATA; INFEASIBLE; ILL_POSED; NOT_ATTEMPTED # явные/кодовые
```
`ITERATION_CAP` — не terminal_reason study, а `TaskReason.iteration_cap` + disposition вопросов цикла (§4); исправление M1:61.

**Реальность 49c7dd7 (блокер №1 rev 3).** В core нет `verdict()` и нет конвейера claim/question: вердикт RM §4.1 и отчёт живут в `coscientist/experimental` (x_report). Core не импортирует experimental. Поэтому:
- **PR 2:** `dispositions()` в core возвращает **для каждого question `not_attempted`** с `NotAttemptedReason`:
  ```python
  class NotAttemptedReason(StrEnum):
      NO_TASK = "no_task"                       # ни одна задача не покрывала вопрос
      NO_COUNTING_CLAIMS = "no_counting_claims" # задачи SUCCESS, но засчитываемых claim нет (или core их не видит)
      TASKS_FAILED = "tasks_failed"             # все покрывавшие задачи FAILED
      TASKS_SKIPPED = "tasks_skipped"           # SKIPPED; детали — TaskReason задач в reason_detail
  ```
  Payload: `{disposition: "not_attempted", reason: NotAttemptedReason, reason_detail: list[TaskReason]}`. Покрытие «задача → question» — только через проекцию question→tasks ниже.
- **Перенос `verdict()` в core и вычисляемые answered/refuted/survived/inconclusive — отложены в PR 6/8.** Правила ниже (п.1–3) — целевые, в PR 2 не реализуются.

**Источник данных для проекции (rev 5, вариант (a)).** На main `a55b0a1` (merge PR #5) в core нет ни адресов, ни вопросов: `TaskSpec` (`coscientist/core/domain.py:159–169`) и `Plan` (`domain.py:199–204`) без адресов; `Study` (`domain.py:182–197`) хранит только `request: str`; `create_study` (`coscientist/core/lifecycle.py:46`) пишет `StudyCreated` = `Study.model_dump`; `accept_plan` (`lifecycle.py:149`) пишет `PlanAccepted` = `Plan.model_dump` (`lifecycle.py:167`); question-событий в core нет. Вопросы рождаются в выходе SPEC-агента (`graph_schema.yaml:40`: `ResearchSpecOut.questions[]`), который живёт вне core. SPEC выполняется **после** `StudyCreated`, поэтому `StudyCreated.questions` не годится: на момент создания вопросов ещё нет.
Решение — минимальный источник в том же событии, что и адреса:
- `class QuestionSpec(Frozen): id: str; text: str` (`core/domain.py`).
- `Plan.questions: list[QuestionSpec] = []` → попадает в payload `PlanAccepted`; `accept_plan(tasks, *, questions: list[QuestionSpec] = [], revision=1)`. Вызывающий (оркестратор/experimental) заполняет его из `ResearchSpecOut.questions[]`. Множество известных вопросов study = объединение `questions` по всем принятым ревизиям; id уникальны, повтор id с другим текстом → `ValueError`.
- `TaskSpec.addresses: list[str] = []` — id вопросов.
- Upcasters: `TaskCreated` (текущая версия → +1) и `PlanAccepted` v1→v2: отсутствующие `addresses`/`questions` → `[]` (в т.ч. вложенные `tasks[].addresses`). Writer пишет новые версии сразу.
- Проверка кодом в `accept_plan` (рядом с `_validate_dag`, `lifecycle.py:215`): каждый `addresses[i]` ∈ известных question id (этой и прошлых ревизий), иначе `ValueError("unknown question id …")` до записи событий. Тест `test_accept_plan_rejects_unknown_address`.
- `graph_schema.yaml`: у узла `question` (стр. 37–40) `from_output` дополнить `Plan.questions[]` (core); у узла `experiment` (`from_output: TaskSpec`, стр. 86) — поле `addresses`; ребро `addresses` (стр. 161) уже есть для `[experiment, question]` — пометить `by: plan (TaskSpec.addresses)`. Перегенерировать таблицы.
- Без вопросов (`questions=[]`) study валидно: проекция пуста, финализатор пишет только conclusion→study.

**Проекция question→tasks (PR 2, rev 4).** Чистая функция core `question_tasks(projection) -> dict[qid, set[task_key]]`, без новых событий: строится по `TaskSpec.addresses` задач в **принятых ревизиях плана** (`PlanAccepted`); после расширений — объединение по всем ревизиям. Задача без адресов относится к study (не к вопросу). Вопрос без задач → `not_attempted(no_task)`. Для остальных `NotAttemptedReason` выбирается по статусам адресующих задач: ≥1 SUCCESS/PARTIAL → `no_counting_claims`; иначе все FAILED → `tasks_failed`; иначе (есть SKIPPED) → `tasks_skipped`; `reason_detail` — TaskReason этих задач. PR 6 и 8 переиспользуют эту проекцию. Тесты `test_question_tasks_union_across_revisions`, `test_task_without_address_belongs_to_study`, `test_not_attempted_reason_from_task_statuses`.

**Единая функция** `dispositions(projection) -> dict[qid, (Disposition, reason)]` (чистая, в `core/`; целевое поведение после PR 6/8/**8b**):
1. **PR 8b disposition mapping.** Если по question есть ≥1 **засчитываемый** claim (counting filter §3 / DESIGN_HYP_DELTA): без гипотез — supported→answered, survived→survived, refuted→refuted, иначе inconclusive; с гипотезами — answered если ≥1 h supported, survived если ≥1 survived и ни одной supported, refuted если все h refuted, иначе inconclusive. `based_on` = counted claims решающих гипотез. Явная disposition при этом игнорируется; конфликт с явной — в отчёт `disposition_conflict{explicit, computed, claim_ids}`.
2. Иначе, если есть явное событие `QuestionDisposed{question_id, disposition ∈ {needs_more_data, infeasible}, reason, refs[]}` — берётся оно.
3. **ill_posed — только вычисляемое (решение по M1:38/40):** question ill_posed ⇔ у него есть premise с вердиктом refuted (через засчитываемые claims к узлу premise, §2.1). Явно задать ill_posed нельзя.
4. Иначе `not_attempted` с `NotAttemptedReason` (см. выше).
`QuestionDisposed` — только needs_more_data/infeasible; агент предлагает через типизированный вывод, код проверяет refs (≥1: evidence, limitation или HitlDecided). Явная disposition действует только до первого засчитываемого claim.

**2.1 Предпосылка.** Новый NodeKind не нужен: `question.premises: list[str]` + атрибут claim `premise_idx: int | None`. FK держится: claim −about→ question (существующее ребро), `premise_idx` валидируется кодом (`0 ≤ idx < len(premises)`). Вердикт по premise считается тем же `verdict()` над claims с данным premise_idx; claims с premise_idx не участвуют в вердикте самого вопроса.

**Intake** до spec: `intake(request, inputs, profile, budget) → Accept | Refuse(kind, reason)`. Кодовые проверки: пустой запрос, budget=0 или `< profile.min_plan_cost`, `duration ≤ finalize_reserve_s` (§0.5). Классификатор safety/scope — за портом, с таймаутом (PR 4). Refuse → ни одной задачи → `StudyFinalized(terminal_reason=REFUSED)` с отчётом.

**Тесты.** `test_intake_refuses_before_spec`, `test_budget_zero_refused`, `test_false_premise_ill_posed_computed`, `test_explicit_disposition_overridden_by_counting_claim`, `test_disposition_conflict_in_report`, `test_partial_feasibility_3_of_5`.

## 3. Передача научного состояния, intent опровержения, k_ref (S1–S3, H-c)

- `InputSpec.kind: artifact | claim | verdict` (+ `ref`). Для claim ядро добавляет ребро `uses_claim: experiment → claim` (новое, 17-е ребро). Claim-узлы пишет `Executor.record` из `AgentTaskOutput.claims[]` с **`attrs.provenance` = provenance производящей попытки** (H-b); если provenance на узле неизвестен — bind считает `stub`, никогда `real`. Для verdict — снимок вердикта на момент P2 в `InputBinding.digest`. PLAN-n и report читают проекцию напрямую, им это не нужно (поправка ревью принята).
- **PR 7 vs PR 8 (verdict input):** PR 7 отклонял `kind=verdict` на `accept_plan`. **PR 8 включает** `kind=verdict`: на P2 ядро считает (или читает журналированный) снимок и пишет digest в `AttemptStarted.inputs[]`.
- **`InputBinding.digest` (P2):** хеш **метаданных привязки** (kind, input/ref, artifact_id, source, at=P2), не тела claim/артефакта. Содержимое claim живёт на graph-узле; смена содержимого — отдельные `GraphNodeUpserted`.
- **Verdict snapshot persistence (PR 8 review):** кроме digest, ядро пишет событие `VerdictSnapshotsRecorded{scope: bind|finalize, attempt_id?, snapshots: {ref → {verdict, by_literature, provenance, k, refutations_needed, refutation_priority, note?}}}`. На replay `StudyRuntime.verdict_snapshots` наполняется из журнала; `start_attempt` предпочитает журналированный снимок и **не** пересчитывает его с текущего графа, если запись есть. **Инвалидация:** любой `GraphNodeUpserted` claim с данным `target`, или `GraphEdgeAdded` kind=`about` к тому же target, очищает **только затронутый** snapshot — следующий bind/finalize пересчитает. `hypothesis_verdicts` из StudyFinalized хранятся отдельно (`runtime.hypothesis_verdicts`), не в bind-кэше. Смена `k`/`k_ref` только через fork (новый study) — старые snapshots не переносятся. `StudyCreated` schema_version=4 + upcaster v3→v4 для `confirmations_needed`.
- **Claim id namespace (PR 8):** id claim = `{study}:claim:{attempt_id}:{agent_key}`; полный id чужой попытки отвергается. Unknown `target` → claim drop (reason в `ResultRecorded.claims_dropped`); вопросные узлы создаёт `accept_plan`, гипотезы — производитель; ядро **не** автосоздаёт target и не угадывает по префиксу `H*`.
- **`accepted` vs criteria (PR 8 / invariant 10):** `accepted` = результат **записан** и claim **структурно валиден** (target/about/id) — **не** «критерии прошли».
- **Check `role` (Duhem–Quine, PR 8):** роль задаётся **только в catalog** (`RequirementTemplate.role: prediction | validity` — **обязательное** поле формата catalog). `CheckResult.role` обязателен (из шаблона). План может указать `RequirementRef.role` лишь как аннотацию: при несогласии с шаблоном `accept_plan` отклоняет план; неизвестный `template_id` — reject. Нет эвристик по `template_id`. Нет шаблона → role=`unknown`, exclusion `unknown_role` (никогда не refute / не validate / **не** форсирует вердикт). **prediction** FAILED → `not_met` → counting refute. **validity** FAILED → claim invalid (`validity_ok=false`, exclusion `validity_failed`) **на уровне эксперимента**: claim в `excluded_claims`, вердикт считается по оставшимся valid claims; `inconclusive` + `reason=validity_failed` **только** когда valid claims по target не осталось. Научный FAILED **не** ретраится, **не** каскадирует H-c, бюджет списывается как обычно. Infra по-прежнему дропает claims. Upcaster TaskCreated v6 / PlanAccepted v3 **снимает** plan-side role (источник — catalog при evaluate).
- **`open_runtime` catalog:** параметр `catalog` обязателен в production path; test-helper `minimal_test_catalog` не импортируется из `runtime_factory` (только `tests.helpers.open_runtime`).
- `tests` получает атрибут `intent: confirm | refute` (по умолчанию confirm) — объявлен в `graph_schema.yaml` как `edges.tests.attrs.intent`, не только в note.
- Атрибут ребра `tests.intent` — изменение схемы рёбер (атрибут, не новое ребро; учтён в подсчёте §10).
- Stance остаётся вычисляемым (RM §2), меняется только интерпретация в вердикте: при `intent=refute` met → claim.stance `supports`, а вердикт гипотезы — **`survived`** (одно имя везде: вердикт `survived` = disposition `survived`; имя `corroborated` не используется). not_met → refutes.
- `Study.refutations_needed: int = 1` (k_ref) и `Study.refutation_priority: bool` — **фиксируются на Study при создании из профиля** (как catalog_version), в событии `StudyCreated`; позже не меняются (смена — только fork).
- **PR 8b counting filter** (заменяет «потолок провенанса»): `counts(c)` = existing_counts ∧ `provenance==real` ∧ `validity==valid` ∧ `outcome ∈ {consistent,inconsistent}` ∧ not literature. Excluded: stub, partial, invalid (`validity_failed`/`unknown_role`), indeterminate, lit, r17, superseded — **до** подсчёта, не потолком после. Verdict: `refuted` ⇔ ≥ k_ref независимых `inconsistent` (при `refutation_priority` подтверждения не спасают; иначе conflict → inconclusive); `supported` ⇔ ≥ `confirmations_needed` severe consistent и ≥1 с `intent=refute`; `survived` ⇔ ≥1 severe consistent. Study: `aggregate=count|evalue`, `evalue_alpha=0.05` (StudyCreated v5); support threshold = `confirmations_needed` (без дубля). Отчёт: `hypotheses[]` с counted/excluded/conflict; note при refuted перечисляет исключённые stub/partial подтверждения. `VerdictSnapshotsRecorded` сохраняется (не только projection). См. `docs/DESIGN_HYP_DELTA.md`.
- **Report note (PR 8 / 8b):** при `refuted` и исключённых stub/partial supports снимок / disposition несут `note`, например `\"refuted; 2 supporting results were stub/partial\"`.
- **PR 6 vs PR 8:** в PR 6 `verdict()` / `counts()` (в т.ч. R17) и поля Study/`tests.intent` живут как чистые правила и доменная модель. **`k_ref`, `refutation_priority` и `tests.intent` начинают влиять на runtime finalize/report только когда PR 8 вшьёт вызов `verdict()` в ядро** (как и «verdict-into-core» в §1: отложено в PR 6/8). До PR 8 тесты вызывают `verdict(...)` напрямую, передавая `Study.refutations_needed` / `refutation_priority`.
- Ревью предлагало только отдельный k_ref; без приоритета k_ref ничего не меняет (sup+ref → inconclusive). Приоритет принят с условиями ревью выше.
- H-c: `depends_on` без InputSpec на FAILED/SKIPPED родителе → SKIPPED(input_unavailable), кроме `TaskSpec.tolerate_failed_dep = True`. Stage-задачи SPEC…REPORT **не** каскадируются H-c (D §7.5: FAILED SPEC → plan с сырого запроса).
- `InputSpec.mode` по умолчанию `forbid` (было `use_partial` / schema_stub, D §2). Заглушки — только явным флагом; upcaster TaskCreated v3→v4 сохраняет `use_partial` для старых журналов.
- `experiment.method=simulation` — научная симуляция (enum как в graph schema); provenance попытки всё равно capped входами (H-b / §6.7), simulation не повышает stub→real.

**Тесты.** `test_uses_claim_edge`, `test_refute_intent_met_is_survived`, `test_refutation_priority_k_ref`, `test_priority_refuted_not_capped_by_stub_supports`, `test_priority_refuted_stub_support_note_in_report_payload`, `test_runtime_refuted_required_check_failed` (prediction→refuted), `test_validity_failed_claim_invalid_inconclusive`, `test_depends_on_failed_parent_skipped`, `test_tolerate_failed_dep`, `test_default_forbid_no_stubs`; обновить refmodel.verdict.

## 4. Расширение плана: PlanExpanded, foreach, LoopSpec (S6, S9, S10, H-d, H-f, H-g, строки 11–15)

- Событие `PlanExpanded{after_task, plan_rev, added_tasks[], added_questions[], superseded_tasks[], origin: plan|emergent, reason, delegated_by}` — новая ревизия plan + `supersedes`. Оно заменяет «PLAN-n только после HYP/LIT».
- Триггер (H-g): ядро после каждого `TaskDone` вызывает planner, только если выполнено одно из условий: `PlanOut.expand_after` содержит этот task_key, или задача вернула `AgentTaskOutput.expansion_request` (typed: какие подзадачи и почему), или HITL plan-шлюз прислал правку. Задача PLAN-n остаётся как контейнер попыток planner→critic (D §3.3), ключ PLAN-n. Нет триггера — нет вызова.
- Лимиты в профиле: `max_expansions` (заменяет max_replans; миграция: значение = max_replans) — **только для расширений по инициативе агента** (origin=emergent из `expansion_request` или `expand_after`). Расширения по инициативе человека (правка плана в HITL, правка вопроса, «реши сам» = PlanExpanded с `delegated_by=human`) квоту агента не тратят; у них отдельный `max_human_expansions` (по умолчанию 10, защита от бесконечной правки). `max_tasks` (на study), `max_depth` (глубина decomposes).
- Deadline задачи PLAN-n = `min(now + plan_timeout_s, effective_deadline)`, а время берётся из `replan_reserve` (доля остатка, зарезервированная под расширения, §5). Если `replan_reserve` исчерпан → `ExpansionRefused{reason=expansion_limit}`. Превышение → запрошенные задачи не создаются, событие `ExpansionRefused{reason=expansion_limit}`, в отчёте строка «не выполнено: лимит расширений». Ничего не теряется молча.
- `foreach`: `TaskSpec.foreach: Ref[hypothesis|question] selector` (например, `selected hypotheses of HYP-0`) — код разворачивает в N задач при DONE источника без вызова planner и без траты expansion. Ключи `EXP-<n>` выдаёт ядро (R5).
- `LoopSpec{loop_id, body: list[TaskSpecTemplate], scope_hypotheses: list[Ref[hypothesis]], scope_questions: list[Ref[question]], until: Predicate, max_iter: int}`. `max_iter` **обязателен**, ≤ `profile.max_loop_iter` (иначе PlanValidator отклоняет). Ядро разворачивает итерацию i в новые task_key (`EXP-<n>` с атрибутом `loop_id`, `iter=i`), поэтому DAG ацикличен.
- **Предикаты ограничены областью цикла:** они видят только узлы из `scope_*` и claims/вердикты, созданные задачами этого `loop_id`. Вся проекция им недоступна. Встроенные:
  - `survivors_le(n)`: |{h ∈ scope_hypotheses : verdict(h) ∉ {refuted}}| ≤ n;
  - `criterion_met(criterion_id)`: criterion из scope в статусе MET;
  - `no_change`: множество пар (h, verdict(h)) по scope_hypotheses и (q, disposition(q)) по scope_questions после итерации i совпадает с тем же множеством после итерации i−1 (сравнение sha256(JCS(отсортированного множества))); на i=1 — ложно.
  Новые предикаты регистрируются плагином профиля (`Registry.loop_predicates`, как CodeCheck kinds, D §8); неизвестный предикат — ошибка валидации плана.
- После каждой итерации (все её задачи DONE) ядро вычисляет until. Истина → цикл завершён. Ложь и i < max_iter → следующая итерация. Ложь и i = max_iter → **лимит цикла (исправление M1:61):** не terminal_reason study, а `LoopCapped{loop_id, iter, survivors}`. Задачи, которые планировались после цикла и зависят от его «итога», запускаются как обычно; вопросы из scope_questions получают disposition из вердиктов (inconclusive/survived), а reason каждой — `iteration_cap`. Неразвёрнутые итерации не создаются (нечего помечать SKIPPED), в отчёте строка «цикл остановлен лимитом после i итераций, осталось m».
- **[возможно избыточно]** LoopSpec — самая дорогая часть M1. Альтернатива — повторные PlanExpanded с until у planner, но тогда честный финал зависит от LLM. Оставляю урезанный вариант: 3 предиката, без DSL.
- Fan-in: `InputSpec.quorum: int | "all"`. Сводная задача запускается, если ≥ quorum входов real/partial; остальные идут в limitations.
- Отброшенные задачи → SKIPPED(superseded | expansion_limit).
- H-d: question могут добавляться в `PlanExpanded.added_questions` (origin=emergent); `supersedes` расширяется парой question→question; правка цели study — только fork (D §3.6).
- H-f: PlanValidator при расширении требует покрытия только **новых и не-disposed** вопросов; вопрос можно закрыть явной disposition infeasible/needs_more_data вместо задачи.

**Тесты.** `test_expand_after_exp`, `test_expansion_limit_reported`, `test_foreach_10_hypotheses`, `test_loop_until_one_survivor`, `test_loop_iteration_cap_honest_final`, `test_fanin_quorum`, `test_emergent_question_added`, `test_validator_partial_coverage`.

## 5. Доли бюджета и deadline (S4, поправка к дыре 6)

- `TaskSpec.budget_share: float | None`, `deadline_share: float | None`. Незаданные доли = равные доли от не распределённого остатка.
- Резервы: `finalize_reserve_s` (§0.5) и `replan_reserve: float` (доля, по умолчанию 0.1) под PLAN-n.
- Валидатор: `Σ budget_share ≤ 1` по всем задачам активной ревизии (бюджет расходуется задачами независимо от параллельности). `Σ deadline_share` проверяется **по критическому пути** DAG (максимум суммы долей по путям от источника до стока) ≤ 1 — параллельные ветви время делят, а не складывают.
- **Перенормировка при P2** (каждый старт задачи i): `remaining = effective_deadline − now − replan_reserve × (effective_deadline − now)`; `task.deadline = now + share_i / Σ share(задачи на критическом пути от i, ещё не стартовавшие) × remaining`. Бюджет: `budget_i = share_i / Σ share(ещё не стартовавшие задачи) × (budget_remaining − budget_reserve)`. Ранний SUCCESS освобождает время и деньги для хвоста; поздние задачи не голодают.

**Тесты.** `test_shares_validator_sum`, `test_tail_gets_remaining_time`, `test_reserve_untouchable`.

## 6. Финальные типы результата и рубрика (B2, B5, K7, строки 4, 5, 8)

**6.1 Финальный тип в PLAN.** `PlanOut.final_type` для каждого корневого question: `verdict` (нужен claim) | `artifact` (artifact без claim: обзор-таблица, набор гипотез, код, дизайн эксперимента) | `both`. PlanValidator: для `artifact` требуется OutputSpec с рубрикой. Для `verdict` требуется ≥1 задача с criterion. `fulfilled` для question с final_type=artifact ⇔ рубрика MET (вместо «вердикт ≠ untested», D §3.2).

**6.2 Рубрика** — CodeCheck/ReviewCheck kind `rubric`:
```python
class RubricItem(BaseModel): id: str; text: str; required: bool; check: Literal["code","judge"]; code_kind: str | None
class Rubric(BaseModel): items: list[RubricItem]; aggregate: Literal["all_required","k_of_n"]; k: int | None; digest: str  # sha256(JCS(items+aggregate))
```
- Замораживается в PLAN: `frozen_at_seq` = seq принятия ревизии плана; digest в событии. Менять можно только через PlanExpanded или HITL; «реши сам» = PlanExpanded с `delegated_by=human`. Новая рубрика `supersedes` старую; NodeRequirement по старой → STALE.
- Пункты атомарные и бинарные. Агрегация: required — все MET; optional влияют только на score в отчёте.
- Рубрика влияет только на outcome задачи и приёмку question с final_type=artifact; **никогда** на claim stance (stance — только от criterion с metric/op/target). Инвариант проверяется тестом.
- Judge failure (таймаут, невалидный JSON, отказ) → пункт UNASSESSED + 1 повтор; не FAILED (R23).

**6.3 Цитаты кодом.** `quote_in_source`: нормализация (NFKC, casefold только для latin/cyrillic, схлопывание пробелов, снятие переносов `-\n`, унификация кавычек и тире), затем точное подстрочное совпадение в тексте, извлечённом из CAS-блоба (`cas/<sha256>`, D §3.8). `SourceQuote` получает `source_sha256`, `lang`, `normalization_version`. Перевод — отдельное поле `translation`, не проверяется. OCR-текст: provenance=partial, отдельный артефакт-текст со своим sha256.

**6.4 «Нет ответа + нужный эксперимент».** disposition `needs_more_data` + обязательный artifact(role=report, `kind=experiment_design`) с рубрикой (гипотеза, переменные, размер выборки, критерий успеха). Это честный финал question, не провал.

**6.5 Противоречия.** Ребро `contradicts: claim → claim` (18-е; одна запись на неупорядоченную пару) создаёт код, когда два accepted claims к одной цели имеют противоположные stance. final_type=artifact «карта противоречий» генерируется кодом из этих рёбер; рубрика проверяет полноту. **[спорно]** Можно обойтись без ребра (выводить на лету), но веб и отчёт хотят его видеть, а стоит оно дёшево.

**6.6 Первые принципы.** evidence kind `derivation`: artifact(role=code|report) с выводом + `ReviewCheck` по рубрике (шаги, допущения) и, где возможно, CodeCheck (символьная проверка, численная проверка). В verdict() засчитывается с потолком `inconclusive`, если только ReviewCheck, и полноценно, если есть исполняемая проверка (§7). Вердикт помечается «теоретически», по аналогии с «по литературе».

**6.7 Симуляция ≠ заглушка (H-b).** ~~Вариант с четвёртым значением provenance (`real | partial | simulated | stub`)~~ — вычеркнут. Решение: `Provenance = real | partial | stub` — текущее `simulated` переименовывается в `stub` (заглушки D §3.4: потолок PARTIAL, вердикт ≤ inconclusive). Научная симуляция — `experiment.method=simulation` с provenance=real: засчитывается в вердикт, помечается «на модели», validity_bounds обязаны это назвать. Verdict study `simulated` → `stubbed`.
- **Upcaster** (PR 1, `core/upcasters.py`): provenance `"simulated" → "stub"` в событиях, где она хранится: `AttemptStarted` и `ResultRecorded`, включая вложенное `inputs[].source`. Отдельного события `InputBound` нет. Verdict `"simulated" → "stubbed"`.
- **Синхронизация study_view ↔ D §3.4 в обе стороны (rev 3, (b)).** Сейчас `core/study_view.py:47–50` шире D §3.4: verdict simulated при **любом** `stub_share > 0`, а `stub_share` зависит от необязательного аргумента `simulated_done` (если его не передали — 0, и stubbed не вычисляется вовсе). Ветка fulfilled (стр. 38) смотрит только на outcome required задач и игнорирует вопросы. Решение PR 1:
  1. `stub_share` вычисляется **из projection** (доля DONE задач с provenance=stub среди DONE), параметр `simulated_done` удаляется (вызовы обновить).
  2. Правило, одинаковое в коде и D §3.4: `stubbed` ⇔ все required ∈ {SUCCESS, PARTIAL} ∧ ∃ required задача с provenance=stub. Нерequired заглушки снижают только `stub_share` в view, не verdict. D §3.4 правится под это правило.
  3. `fulfilled` в PR 1 по-прежнему по задачам; в D §3.4 явно записать «вопросы не учитываются до PR 2». PR 2 добавляет: при ≥1 question с disposition not_attempted verdict не выше `partial`. **Следствие:** до PR 6/8 study с вопросами в core не бывает fulfilled (все вопросы not_attempted) — это честно и задокументировать в D §3.4.
  4. Тест `test_study_view_stubbed_computed` (PR 1): stubbed без передачи simulated_done; required SUCCESS + одна required stub → stubbed; только нерequired stub → fulfilled.
- **report required=False (PR 2) меняет ветку partial (`study_view.py:36`, `if not req: verdict="partial"`):** задача report больше не входит в `req`, поэтому (а) её FAILED не понижает verdict; (б) study, где единственной required была report (план без required задач), уходит в ветку `not req` → `partial`. Документировать в D §3.4 и тестом `test_report_not_required_verdict`.

**Тесты.** `test_final_type_artifact_fulfilled_by_rubric`, `test_rubric_frozen_digest`, `test_rubric_change_only_via_expansion`, `test_rubric_never_changes_stance`, `test_judge_failure_unassessed_retry`, `test_quote_normalized_exact_match`, `test_needs_more_data_requires_design`, `test_contradicts_edge`, `test_derivation_verdict_marked`, `test_stub_vs_simulation`, `test_study_view_stubbed_computed`.

## 7. Исполняемые критерии (B7, B8)

CodeCheck kinds: `exec_runs` (команда в sandbox, exit 0, лимиты времени и памяти), `tests_pass` (junit-артефакт, `failures == 0`, `tests ≥ min`), `metric_on_holdout` (metrics.json + EvidenceRef, holdout-артефакт с sha256 зафиксирован до запуска), `reproducible_rerun` (две попытки, |Δ| ≤ tol). Выход каждого — artifact(log), на который ссылается EvidenceRef. Sandbox: без сети, RO-входы, лимиты. **Тесты:** `test_exec_runs_exit_code`, `test_tests_pass_junit`, `test_holdout_digest_frozen`.

## 8. Класс побочного эффекта инструмента (K4, K5, K13)

- `ToolSpec.effect: read | write | external_effect` (по умолчанию **read**; заменяет `read_only: bool = False`, D §6.1). `idempotent` остаётся.
- write/external_effect вызываются только через гейт `side_effect` (новый в HitlPolicy). Headless: по умолчанию `deny` → ToolError(local), задача решает сама (обычно SKIPPED с reason=human_rejected). Гейт привязан к классу инструмента, а не к experiment.costly.
- `effect_idem_key = sha256(JCS({study_id, task_key, tool, args_digest}))`. Событие `EffectIntended{key}` пишется **до** вызова, `EffectDone{key, result_ref}` — после. При повторе/recover: если есть Done → вернуть сохранённый результат; Intended без Done → вызов только если инструмент поддерживает передачу idem_key серверу, иначе ToolError(opaque), и человек решает.
- Taint: артефакты из web/tool-вывода помечаются `tainted=true`. Попытке с tainted-контекстом write/external_effect не выдаются вообще.
- `POST /studies` принимает `Idempotency-Key`; повтор → тот же study_id.

**Тесты.** `test_default_effect_read`, `test_write_requires_gate`, `test_effect_not_repeated_on_retry`, `test_tainted_context_no_write_tools`, `test_post_studies_idempotent`.

## 9. Снимок входа (K10, A3, A9)

- `InputBinding.digest` (sha256 содержимого на момент P2), `retrieved_at`, `source_uri`. Для MCP/БД — digest нормализованного ответа (JCS), ответ сохраняется артефактом.
- `Attempt.inputs_digest = sha256(JCS(sorted bindings))` рядом с config_digest (R14).
- Если между попытками одной задачи inputs_digest изменился → limitation `input_drift` и provenance=partial.
- `study.as_of: datetime | None` для вопросов, чувствительных ко времени; отчёт его показывает.
- Пользовательские входы: `POST /studies {…, inputs[]}` → artifact(role=data, origin=user) до spec, с `InputLimits` профиля (bytes, ratio распаковки, pages, rows) → при превышении intake Refuse(INPUT_TOO_LARGE) или partial.

**Тесты.** `test_input_digest_recorded`, `test_input_drift_limitation`, `test_user_inputs_registered_before_spec`, `test_zip_bomb_refused`.

## 10. Покрытие неявных подзадач и дерево в отчёте (строки 14, 15, S8)

- Ребро `decomposes: question → question` с атрибутом `origin: explicit | implicit | emergent`; **проверка ацикличности** decomposes (и отсутствия self-loop) при каждой записи — как для depends_on. Planner обязан вернуть `PlanOut.subquestions[]` с именами. PlanValidator проверяет: (а) каждый лист покрыт задачей или disposition; (б) ReviewCheck критика по рубрике «декомпозиция покрывает исходный вопрос» (заморожена в PLAN, §6.2); (в) глубина ≤ max_depth, число ≤ max_tasks.
- Правило свёртки (frozen в PLAN): `rollup: all | any | majority | rubric`. Disposition родителя вычисляется по детям.
- Отчёт (финализатор): раздел «Дерево подзадач» — question → subquestion → task_key, статус/outcome, reason, origin (plan/emergent), revision, где появилось, superseded-задачи зачёркнуты. Это закрывает R29 «покрытие».
- **Полный счёт изменений схемы рёбер** (16 в v0.4):
  | Изменение | Тип | Где |
  |---|---|---|
  | `uses_claim: experiment → claim` | новое ребро (17) | §3 |
  | `contradicts: claim → claim` | новое ребро (18); **одна запись на неупорядоченную пару**: код хранит (min(id), max(id)), повтор → Idempotent | §6.5 |
  | `decomposes: question → question` | новое ребро (19), ацикл. | §10 |
  | `answers: conclusion → study` | новая пара существующего ребра | §1 |
  | `supersedes: question → question` | новая пара существующего ребра | §11 |
  | `tests.intent: confirm\|refute` | новый атрибут ребра | §3 |
  | `claim.premise_idx` | атрибут узла (вместо `question#premise/i`) | §2.1 |
  Итого 19 видов рёбер, +2 пары, +1 атрибут ребра. Поля-списки id вместо рёбер ломали бы FK-проверку проекции (RM §3).

**Тесты.** `test_validator_requires_named_subquestions`, `test_rollup_rule`, `test_report_subtask_tree_marks_emergent`.

## 11. Supersede вопроса и k (C10, H-d)

- `supersedes: question → question`. В `counts(c)` (RM §4.1) добавляется условие: target и checked_against claim не superseded. Claims к старой ревизии остаются в графе, но в k не идут; отчёт показывает их как «к прежней формулировке».
- Правка вопроса в HITL = PlanExpanded (added_questions + superseded), задачи старого вопроса без общего потомка → SKIPPED(superseded).

**Тесты.** `test_superseded_question_claims_not_counted`, `test_hitl_question_edit_skips_old_tasks`.

## 12. R17 (H-e)

Формулировка: `criterion.frozen_at_seq < seq первой evidence, на которую ссылается claim с checked_against = этот criterion` (не любой evidence study). Критерий воспроизведения статьи, созданный в PLAN-1 после LIT-0, корректен. Плюс: evidence, использованная для **выбора** порога (например, число статьи), не может засчитываться как подтверждение того же criterion. Иначе получается цикл.

В PR 6 правило входит в путь `counts()` / `verdict()` через `claim_counts_under_r17` (и зеркало `Claim.counts_under_r17` в refmodel): без freeze/evidence metadata — базовый RM §4.1 `counts()`; при metadata — claim не идёт в k, если R17 не выполнен.

**M1 / PR 8 scope:** R17 **runtime wiring is out of scope for M1**. Executor derives `frozen` from plan-accept criterion nodes but does **not** populate `criterion_frozen_at_seq` / `first_cited_evidence_seq` / confirming-vs-threshold evidence sets on claim nodes. Pure-rule tests (PR 6) cover `claim_counts_under_r17`; an xfail marks the missing runtime path. Wiring R17 metadata through `Executor.record` is a post-M1 follow-up.

**Тест.** `test_r17_criterion_after_lit_valid`, `test_r17_threshold_source_not_counted`, `test_r17_claim_excluded_from_verdict`; `test_r17_runtime_metadata_not_wired_yet` (xfail, M1).

## 13. Property-тест (п.7 ревью)

`hypothesis.stateful.RuleBasedStateMachine` `StudyLifecycle` (расширение `test_every_study_closes_by_deadline`). Правила: create_study (в т.ч. budget=0, пустой запрос, refuse), accept_plan(цепочка до 12, fan-out до 10), cancel_study, fail_random_attempt, hang, crash_and_recover, long_downtime, hitl_answer/timeout (experimental runtime), advance_clock. Правила expand/LoopSpec/question_supersede добавляют PR 9/11 в свои тесты; PR 3 их не содержит.
Инварианты после каждого шага: все текущие из D §3.3 + store отклоняет запись после StudyFinalized (I3) + все дедлайны ≤ effective_deadline.
Финал (`now = study.deadline + tick`): `∃ StudyFinalized ∧ (report_artifact_id ∨ minimal_report) ∧ terminal_reason ∈ TerminalReason ∧ ∀ question disposition ≠ None ∧ I2 (conclusion ∨ покрытие minimal_report) ∧ ∀ task DONE с reason ∈ TaskReason`. Работает на refmodel и core (`impl=`).

## 14. Порядок реализации (PR-шаги)

| PR | Содержание | Закрывает | Зависит |
|---|---|---|---|
| 1 (#5) | `TaskReason`/`CloseReason` enum + upcasters строк (§0.1); `TerminalReason`, `Disposition`, `NotAttemptedReason` (объявление); `RefusalKind` и intake — **только объявление, docstring «PR 4, не реализовывать»**; writer пишет `TaskDone`/`AttemptClosed` сразу v2; синхронизация study_view↔D §3.4 (§6.7); переименование provenance `simulated→stub`, verdict `simulated→stubbed` + upcaster (§6.7); `finalize_reserve_s`, `effective_deadline`, проверка `duration > reserve` в create_study, обрезка plan/report/HITL дедлайнов (§0.5) | B1, B5, H-b | — |
| 2 | Финализатор (§1): `StudyCancelled` + cancel_study, `StudyFinalized` (trigger, terminal_reason §0.2, contributing_reasons), фазы running/finalizing/closed, удалить правило D §3.2 «closed ⇔ все DONE»; детерминированный отчёт + conclusion на каждый question + conclusion→study; тотальность (minimal_report ≤ 10 КБ, схема §0.3); `dispositions()` — **все вопросы not_attempted + NotAttemptedReason**; store отклоняет запись после финала (лог+метрика); recover; шлюз report — только проза | H-a, H-h, B2, B3, B4 | 1 |
| 3 | Property-тест §13; инварианты, которые ещё не реализованы (дальние PR), помечены `xfail(strict=True)` по одному | И | 2 |
| 4 | Intake + refuse (budget=0, пустой, duration ≤ reserve, safety/scope за портом) | X3, K3, K12 | 2 |
| 5 | `simulate=forbid` по умолчанию; H-c (`tolerate_failed_dep`); `method=simulation` | S5, H-c | 1 |
| 6 | R17 в `counts()`/`verdict()`; `tests.intent`, `survived`, k_ref + refutation_priority на Study (правила + модель; **runtime wiring → PR 8**) | S2, S3, H-e | 1 |
| 7 | `InputSpec.kind` artifact\|claim + ребро `uses_claim` (P2); bindings+digest в AttemptStarted; **`kind=verdict` → PR 8** | S1 | 6 |
| 8 | Вшивка `verdict()` в runtime finalize/report + `InputSpec.kind=verdict` (digest на P2); k_ref / intent / survived в проде; check `role` (Duhem–Quine); `QuestionDisposed` (needs_more_data/infeasible), premises + premise_idx (ill_posed вычисляемый), конфликт в отчёте, PlanValidator с частичным покрытием | K8, H-f, S2/S3 runtime | 2, 6, 7 |
| 8b | Hypothesis verdict model (DESIGN_HYP_DELTA): claim `validity`/`outcome`/`severe`/`independence_key`; stub/partial excluded before counting; support via `confirmations_needed`; `aggregate` / `evalue_alpha` on Study; disposition mapping with hypotheses; report `hypotheses[]` | H-e, S2/S3 model | 6, 7, 8 |
| 9 | `PlanExpanded` + триггер + квоты агента/человека + replan_reserve + question add/supersede + фильтр k | S6, H-d, H-g, C10 | 8b |
| 10 | foreach + quorum fan-in | S9 | 9 |
| 11 | LoopSpec (scope, 3 предиката, обязательный max_iter, LoopCapped) | S10 | 9 |
| 12 | budget_share/deadline_share + перенормировка + валидатор (критический путь) | S4 | 9 |
| 13 | Рубрика + final_type + PlanValidator | B2, B5, K7 | 8 |
| 14 | quote_in_source с нормализацией по CAS | K9 | — |
| 15 | needs_more_data + experiment_design; contradicts; derivation | строки 4, 5, 8, 9 | 13 |
| 16 | decomposes (ацикл.) + rollup + дерево в отчёте | строки 14, 15, S8 | 9, 13 |
| 17 | Исполняемые CodeCheck kinds + sandbox-лимиты | B7, B8, K5 | — |
| 18 | ToolSpec.effect, гейт side_effect, effect_idem_key, taint, Idempotency-Key | K4, K5, K13 | — |
| 19 | Снимок входа + пользовательские inputs + InputLimits | K10, A3, K1 | 4 |

**Факт PR 1 (#5), сверх плана:** дедлайны попыток и задач EXP тоже обрезаны до `effective_deadline`; upcaster `StudyCreated` v1→v2 (старые study: `finalize_reserve_s=0`); причины `retry_scheduled` и отмены переведены в enum.

PR 1–3 — блокер M1. PR 14, 17, 18 независимы. PR 10, 12, 13 **не зависят от LoopSpec (PR 11)**: их тесты и валидаторы не используют LoopSpec; PR 11 можно выпустить последним или отложить.

## 15. Расхождения с ревью и их разрешение

1. **Disposition** — принято условие ревью: ill_posed только вычисляемый; явные — только needs_more_data/infeasible и действуют до первого засчитываемого claim; конфликт — в отчёт; payload StudyFinalized считается той же функцией (replay-тест).
2. **refutation_priority** — принято: фиксируется на Study; PR 8b исключает stub/partial до подсчёта (потолок не нужен); ≥ k_ref real refs → `refuted`; отчёт пишет `note` про исключённые stub/partial подтверждения.
3. **LoopSpec** — принято: scope цикла, точный no_change, обязательный max_iter с потолком профиля, лимит — LoopCapped + reason вопросов, не terminal_reason. Остаётся оговорка: LoopSpec может оказаться избыточным для M1. Я бы выпустил его последним из 9–12 и только если сценарий «пока не останется одна» нужен в приёмке M1.
4. **Provenance** — принято: upcaster `simulated→stub`/`stubbed`, вариант с четвёртым значением вычеркнут, перенос в PR 1.
5. **H-a** — по сути согласен; уточнение: «fulfilled без отчёта» возможен только при report.required=False (по умолчанию True), «closed без отчёта» — всегда.
6. **Квоты расширений** — принято: человек не тратит квоту агента, отдельный `max_human_expansions`; PLAN-n — из replan_reserve.
7. **Доли** — принято: перенормировка по не стартовавшим задачам; deadline — по критическому пути, budget — Σ ≤ 1.
8. **TerminalReason (B2)** — rev 3: приоритет по флагам заменён на «причина = триггер финализации» (§0.2), прочие — в contributing_reasons по seq. Вопрос «budget или deadline выше» снят: решает триггер, тай-брейк в одном тике cancelled > budget > deadline.
9. **Открыто:** до PR 6/8 core-study с вопросами не может быть fulfilled (все вопросы not_attempted, §6.7 п.3). Это сознательно; если приёмка M1 требует fulfilled на core-рантайме, PR 6/8 становятся блокером M1.
