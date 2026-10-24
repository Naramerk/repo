# CoScientist-next — дизайн ядра

Статус: черновик v0.1 (2026-10-23). Источник требований: карта CoScientist (Page), docs/CAPABILITIES.md,
ТЗ/ТП НИРСИИ, `rerun_cossessions/baseline_20261022/KNOWN_BUGS.md` старого репо (B1, B2, R1–R5).
Старый код (`CoScientist_dev-dev_1`, коммит 29c5831) — эталон поведения, не код для переноса.

## 1. Цели и не-цели

Цели
- Состоянием исследования владеет код. LLM выдаёт типизированные данные; статусы, переходы и запись результата делает ядро.
- Все обязательные возможности сохраняются: гипотезы, литература, медицина, микрофлюидика, alembic-fallback, coder, веб.
- Один версионированный каталог требований (Том Б + plan_criteria + правила R).
- Каждый переход переживает падение процесса: журнал событий, идемпотентное восстановление.
- Токены и стоимость по прогону и по задаче; трассировка вызовов инструментов.
- Не воспроизводить B1, B2, R1–R5 (у каждой — свой тест, §12).
- Воспроизводить ЛЮБОЕ эмпирическое или вычислительное исследование, насколько возможно; примеры эталона — не весь охват. Ядро (`core/`) и граф (RESEARCH_MODEL.md) не содержат знаний о предметной области: домены живут только в профилях (инструменты, проверки, промпты). Нет нужной возможности → путь alembic (сборка инструмента, §6.3), затем coder.
- Стабильность — измеримая цель первого класса (§12.4): одинаковый вход → одинаковый исход, близкие стоимость и форма графа.

Не-цели
- Защитные плагины, которые следят за LLM (repeat_call_guard, artifact_gate, enforce_pending_record_result, report_guard и т.п.).
- Перенос старых модулей или формата старого состояния (кроме миграции критериев плана, §5.5).
- Google ADK / LiteLLM как рантайм агентов (решение 2026-10-24: свой тонкий LLM-клиент, §7).
- Камера лаборатории и реактор; сценарий «Запрос заказчика» до решения владельцев (§14).

## 2. Доменная модель

Все типы — pydantic v2, `frozen=True`. Id — строки с префиксом, генерирует ядро (не LLM).

```python
class NodeKind(StrEnum):
    # = coscientist/schema/graph_schema.yaml (RESEARCH_MODEL.md §2); тест сверяет
    STUDY = "study"; CONSTRAINT = "constraint"; QUESTION = "question"; CRITERION = "criterion"
    HYPOTHESIS = "hypothesis"; PLAN = "plan"; EXPERIMENT = "experiment"; ARTIFACT = "artifact"
    EVIDENCE = "evidence"; CLAIM = "claim"; CONCLUSION = "conclusion"; REPORT = "report"
    HITL = "hitl"  # M1 PR12: budget (etc.) decision node; approved_by/delegated_by live here

class Study(BaseModel):
    study_id: str              # "st_<ulid>"
    profile: str               # "research" | "medicine" | "microfluidics"
    request: str
    catalog_version: str       # фиксируется при создании
    hitl_policy: HitlPolicy    # §7.3
    deadline: datetime         # верхняя граница всего study (§3.3)
    finalize_reserve_s: int = 30  # резерв под финализацию; effective_deadline = deadline − reserve (§0.5/M1)
    # Intake (M1 PR4): duration ≤ finalize_reserve_s → Refuse(DURATION_TOO_SHORT), not ValueError;
    # empty/budget_zero/safety → other RefusalKind; event StudyRefused in same txn as StudyCreated.
    parent_study_id: str | None = None   # «продолжить сессию» (§3.6)
    goal: Goal                 # statement, research_form, trl (заполняет spec)
    constraints: list[Constraint] = []   # узел constraint — только для проверяемых
    confirmations_needed: int = 1        # профиль; k для вердикта (RESEARCH_MODEL §4.1)
    max_replans: int = 2                 # профиль; перепланирования PLAN-1..n
    # статуса нет: study_view() выводится из задач (§3.2)

class Hypothesis(BaseModel):
    hypothesis_id: str; statement: str; rank: int; prediction: str; selected: bool
    rationale: str | None = None
    addresses: list[str]       # question_id, ≥1, умолчания нет
    approved_by: Literal["human","critic"] | None = None; delegated_by: Literal["human","timeout"] | None = None
    # PR8b: projection (+ VerdictSnapshotsRecorded); not an agent-writable field
    verdict: Literal["supported","survived","refuted","inconclusive","untested"] | None = None

class Need(BaseModel):
    needed: bool; reason: str

class Plan(BaseModel):
    plan_id: str; study_id: str; revision: int        # новая ревизия = новая запись
    hypotheses: Need; literature: Need                 # решения planner (профиль может обязать)
    critic_decision: Literal["accept","revise","reject"] | None = None
    supersedes: str | None = None                      # предыдущая ревизия (PLAN-n или правка HITL)
    approved_by: Literal["human","critic"] | None = None; delegated_by: Literal["human","timeout"] | None = None
    tasks: list[TaskSpec]                              # порядок = номер задачи

class TaskSpec(BaseModel):
    task_key: str              # "LIT-<n>" | "EXP-<n>" | "HYP-<n>" | "PLAN-<n>", n назначает ядро при принятии плана, не меняется (R5)
    title: str; goal: str
    route: str                 # имя маршрута-агента ("experiment", "coder", "medical", ...)
    required: bool = True
    tools_hint: list[str] = []
    expected_outputs: list[OutputSpec]
    criteria: list[RequirementRef]                     # §5
    depends_on: list[str] = []                         # task_key
    inputs: list[InputSpec] = []                       # что берётся от зависимостей

class InputSpec(BaseModel):
    from_task: str; output: str; schema_ref: str | None
    # code field ``mode``: use_partial|simulate|forbid; default forbid (M1 PR5; was schema_stub)
    simulate: Literal["schema_stub","llm_stub","forbid"] = "forbid"

class OutputSpec(BaseModel):
    name: str; media_type: str; schema_ref: str | None; required: bool = True

class Task(BaseModel):
    task_id: str               # = f"{study_id}/{task_key}"; уникален (R5)
    kind: TaskKind             # spec | plan (PLAN-0..n) | hypotheses (HYP-n) | experiment (LIT-n = method literature_search, EXP-n) | report
    spec: TaskSpec
    state: TaskState           # PENDING | RUNNING | DONE
    outcome: TaskOutcome | None   # SUCCESS | PARTIAL | FAILED | SKIPPED, только при DONE
    reason: str | None         # для FAILED/SKIPPED: deadline, budget, cancelled, invalid_status, ...
    deadline: datetime         # у PENDING и RUNNING всегда есть
    attempts: list[str]
    inherited_from: str | None = None   # task_id в родительской сессии (§3.6)

class InputBinding(BaseModel):        # как задача получила вход
    input: str; artifact_id: str
    source: Literal["real","partial","stub"]

class Attempt(BaseModel):
    attempt_id: str            # = f"{task_id}#{n}"
    n: int                     # попытка — запись внутри RUNNING, не отдельное состояние
    dispatch_id: str           # ключ идемпотентности вызова маршрута
    route_returned: bool       # §3.4
    closed: bool; close_reason: str | None
    provenance: Literal["real","partial","stub"]   # max по входам (транзитивно по produced_by→attempt); не-real → outcome ≤ PARTIAL
    config_digest: str         # sha256(JCS(resolved config)) — снимок конфигурации (Том Б п.7, R14)
    agent_output_digest: str | None
    deadline: datetime

class Result(BaseModel):
    result_id: str             # = f"{attempt_id}:result"; одна запись на попытку
    attempt_id: str
    outcome: Outcome           # SUCCESS | PARTIAL | FAILED — ВЫЧИСЛЕН кодом (§4)
    inputs: list[InputBinding]
    provenance: Literal["real","partial","stub"]   # копия Attempt.provenance → не-real: outcome ≤ PARTIAL
    agent_claim: Outcome | None   # что заявил агент
    downgrade: Downgrade | None   # принят, только если claim < вычисленного и есть reason
    computed_outcome: Outcome     # до понижения, хранится всегда
    artifacts: list[str]       # artifact_id
    check_results: list[CheckResult]
    unassessed: list[str] = []    # id required ReviewCheck без вердикта (R23), на outcome не влияют
    payload_digest: str

class Artifact(BaseModel):
    artifact_id: str           # = "art_" + sha256(content)[:16] + "_" + sha256(produced_by.call_id)[:8]; блоб: cas/<sha256> (§3.8)
    name: str; media_type: str
    sha256: str; size: int
    uri: str                   # file:// или s3://
    produced_by: ToolCallRef   # §6; источник обязателен (R30)
    schema_ref: str | None

class EvidenceRef(BaseModel):          # модель доказательства (п.2 ревью)
    artifact_id: str
    json_pointer: str                  # RFC 6901, обязателен; "" = весь документ
    digest: str                        # sha256(JCS(значение по указателю))

class SourceQuote(BaseModel):          # доказательство из литературы (M1 PR14 / K9)
    artifact_id: str                   # artifact(role=paper) или OCR-текст (свой sha256)
    locator: str                       # страница или раздел
    quote: str; digest: str            # digest = sha256(JCS(quote)); проверка quote_in_source
    source_sha256: str                 # обяз.; = SourceFetched.digest = artifact.sha256
    lang: str | None = None
    normalization_version: str | None = None  # "rules+unidata_version"; None → UNASSESSED
    translation: str | None = None     # не проверяется кодом
# quote_in_source: только is_fetch tool via invoke_tool (FetchCapability) + SourceFetched v1
# {call_id,tool,digest,url} (не effect index); reject put/put_sandbox; network stays tainted=True
# (journal ≠ trusted). Token-boundary + digit-edge; CJK/Thai ≥8 chars no boundary; ≥4 letter-tokens;
# super/sub → ^n/_n до NFKC; -\\n join только между буквами; presence≠polarity; study-scoped digests.

class Node(BaseModel):                 # проекция для графа/метамодели/веба
    node_id: str; kind: NodeKind; parent_id: str | None; ref_id: str
```

Все сущности существуют только как проекция журнала событий (§3.5). Отношение «требование → доказательство»:
`NodeRequirement.evidence: list[EvidenceRef]`. Проверка ссылается на артефакт по id и, при необходимости,
по json_pointer; digest фиксирует значение, на котором проверка прошла (повторная проверка на другом значении — новый CheckResult).

## 3. Модель состояний

### 3.1 Состояния (всё пространство)

```python
class TaskState(StrEnum): PENDING; RUNNING; DONE
class TaskOutcome(StrEnum): SUCCESS; PARTIAL; FAILED; SKIPPED      # только при DONE
class Outcome(StrEnum): SUCCESS; PARTIAL; FAILED                    # результат попытки
```
- Все стадии study — задачи одного DAG: `spec → plan → [HYP-n | LIT-n | EXP-n]* → report`. Литература и гипотезы не обязательны: planner решает `plan.literature`/`plan.hypotheses` = {needed, reason}, профиль может обязать; бывшая стадия literature — эксперимент LIT-0 (method=literature_search), отдельного TaskKind нет; после HYP/LIT planner может перепланировать (новая ревизия plan). Отдельной машины состояний study нет.
- Нет BLOCKED, UNKNOWN, AWAITING_HUMAN, CANCELLED. Ожидание человека — открытый HitlRequest внутри RUNNING (со своим deadline). Отмена — DONE(SKIPPED, reason=cancelled).
- Значение вне enum (из вывода агента или инструмента) → `InvalidStatus` → попытка закрывается FAILED(retryable). Хранить «неизвестный» статус негде.

### 3.2 Статус study — вычисляется, не хранится

```python
def study_view(tasks, *, attempts=None, finalized=False, now=None, effective_deadline=None,
               question_dispositions=None) -> StudyView:
    # stub_share = |DONE with provenance=stub| / |DONE|  (из projection/attempts; без simulated_done)
    # closed ⇔ ∃ StudyFinalized (finalized=True). Не «все задачи DONE».
    if finalized:
        return StudyView(phase="closed", verdict=..., stub_share=..., progress=1.0)
    if any(t.state != DONE for t in tasks) and not (now and effective_deadline and now >= effective_deadline):
        return StudyView(phase="running", progress=done/total, awaiting_human=open_hitl > 0, stub_share=...)
    # finalizing: все DONE или now ≥ effective_deadline, финала ещё нет
    req = [t for t in tasks if t.spec.required]   # report: required=False (PR 2)
    if not req: verdict = "partial"
    elif all(t.outcome in {FAILED, SKIPPED} for t in req) or plan_task.outcome in {FAILED, SKIPPED}:
        verdict = "failed"
    elif all(t.outcome in {SUCCESS, PARTIAL} for t in req) and any(prov(t)=="stub" for t in req):
        verdict = "stubbed"
    elif all(t.outcome == SUCCESS for t in req):
        verdict = "fulfilled"
    else: verdict = "partial"
    # PR 2: ≥1 question not_attempted → verdict не выше partial (fulfilled/stubbed → partial)
    return StudyView(phase="finalizing", verdict=verdict, stub_share=..., ...)
```
I1: `closed ⇔ ∃ StudyFinalized`. Тест: `test_b1_closed_only_when_all_tasks_done` (фаза closed только при finalized).
I2: `closed ⇒ ∀ question: (∃! conclusion −answers→ question) ∨ (question_id ∈ minimal_report.questions ∨ minimal_report.truncated ∧ учтён в summary)`; на основном пути — conclusion у каждого question, на fallback — покрытие `minimal_report`. **Отклонение:** last-resort payload (`last_resort_payload`) пишет только study-level conclusion (`conclusion_ids_for(sid, {})`) и пустые `questions` — per-question conclusions отсутствуют.
I3: после `StudyFinalized` store отклоняет любые записи (`StudyClosedError`); метрика `late_write_rejected_total`.

### 3.3 Переходы

| # | Переход | Событие | Кто | Условие |
|---|---|---|---|---|
| P1 | — → PENDING | `TaskCreated` | код | study создан (стадии) или план принят (эксперименты); ключ назначен ядром |
| P2 | PENDING → RUNNING | `AttemptStarted` | планировщик | все зависимости DONE (с любым outcome), слот свободен |
| P3 | RUNNING → RUNNING | `AttemptClosed(retryable, retry_at)` + позже `AttemptStarted` | исполнитель / `tick` | попытка FAILED и retryable, n < max_attempts, deadline не истёк; `retry_at` пишется в журнал; следующий `AttemptStarted` — только когда `tick` видит `clock.now() ≥ retry_at` (без `asyncio.sleep`/таймеров). После crash fold восстанавливает `_retry_at` из журнала |
| P4 | RUNNING → DONE(outcome) | `TaskDone` | исполнитель | результат записан (§4); outcome = compute_outcome, при provenance≠real ≤ PARTIAL |
| P5 | PENDING/RUNNING → DONE(FAILED, deadline) | `AttemptClosed(deadline)` + `TaskDone` | таймер | now > task.deadline; см. «Отмена по deadline» ниже |
| P6 | PENDING/RUNNING → DONE(SKIPPED, cancelled\|budget\|study_deadline) | `TaskDone` | API / бюджет / таймер study | — |
| P7 | RUNNING → DONE(SKIPPED, human) | `TaskDone` | HITL skip_task | только по решению человека |

Отмена по deadline (P5, P6):
1. Событие `AttemptClosed` пишется первым (closed=true) — дальнейшие записи попытки отклоняются.
2. `task.cancel()` asyncio-корутины попытки; ожидание ≤ 5 с.
3. LLM-адаптер: httpx `Timeout(connect=10, read=min(120, остаток deadline), write=30, pool=10)`; ни одного вызова без таймаута.
4. Синхронные MCP/локальные инструменты выполняются в потоке (`asyncio.to_thread`) с таймаутом, а stdio-MCP и sandbox — в subprocess, который убивается (`SIGTERM`, через 5 с `SIGKILL`).
5. Coder/alembic job: `JobStore.cancel` (§6.4).
6. Слот параллельности освобождается в `finally` попытки и, независимо, при `AttemptClosed` (семафор по attempt_id, идемпотентно).

DONE финален; повторный запуск — только через «продолжить сессию» (§3.6). Поэтому гонки «перезапуск после закрытия» нет.

Критика плана: задача plan внутри RUNNING делает ≤ max_plan_revisions циклов planner→critic (это попытки, не состояния); accept → DONE(SUCCESS) и P1 для экспериментов; reject или исчерпание → DONE(FAILED).

Достижимость терминала по построению:
- у каждой PENDING/RUNNING задачи есть `deadline ≤ study.deadline`; P5/P6 срабатывают безусловно по таймеру;
- попытки конечны (max_attempts), проверки ревью конечны (review_deadline < attempt deadline → UNASSESSED), HITL-запрос конечен (§7.3);
- DAG ацикличен (проверка при принятии плана), P2 требует только DONE зависимостей, а не их успеха.
Тест (hypothesis `RuleBasedStateMachine`, инжектированные часы `FakeClock`): `test_every_study_closes_by_deadline`.
- Правила: create_study, accept_plan(случайный DAG), start/finish/fail/hang attempt, advance_clock, open/answer HITL, recover_after_idle (HitlDeadlineExtended), cancel_task, fork, lose_lease (второй владелец получает lease), crash_and_recover.
- Инварианты после каждого шага: ≤ 1 открытой попытки на задачу; replay журнала == проекция; каждый deadline (задача, попытка, HITL, job) ≤ study.effective_deadline(); писатель без актуального epoch ничего не записал.
- Финал: при `now = study.deadline + tick` все задачи DONE.
Интеграционный тест `test_llm_hang_past_deadline`: фейковый OpenAI-совместимый сервер не отвечает; к deadline+5 с попытка закрыта, корутина отменена, слот свободен, задача DONE(FAILED, deadline), поздний ответ отклонён.

### 3.4 Провал зависимости: частичные и симулированные входы

При P2 для каждого `InputSpec` ядро строит `InputBinding`:
1. зависимость SUCCESS → `real`;
2. зависимость PARTIAL/FAILED, но нужный выход есть и проходит schema_valid → `partial`;
3. иначе, если `simulate != forbid` → заглушка (`schema_stub` — генерация по JSON-схеме; `llm_stub` — роль stubber, по умолчанию выключен, включается флагом профиля) → `stub`;
4. `simulate=forbid` и данных нет → задача DONE(SKIPPED, reason=input_unavailable) без запуска.
5. Зависимость закрыта с reason=human_timeout (гейт HITL, §7.3) → все потомки DONE(SKIPPED, input_unavailable), заглушки запрещены.
- Дорогие маршруты (coder, alembic, FEDOT, GPU) по умолчанию `simulate=forbid`; разрешение — только с отдельным `Budget` для работы на заглушках (§7.4).
- `provenance` (real < partial < stub) хранится на попытке, не на артефакте: артефакт stub ⇔ его `produced_by` ведёт к попытке с provenance=stub. Перепаковка через MCP-инструмент (новый артефакт той же попытки) флаг не снимает. Входы `partial` ставят provenance=partial на попытке (потолок PARTIAL).
- `compute_outcome`: при Attempt.provenance ≠ real outcome = min(outcome, PARTIAL). SUCCESS на заглушках или частичных данных невозможен.
- `StudyView` содержит `stub_share` (доля DONE-задач с provenance=stub) и verdict `stubbed` ⇔ все required ∈ {SUCCESS, PARTIAL} ∧ ∃ required с provenance=stub. Нерequired-заглушки влияют только на stub_share.
- Задача `report` — `required=False` (PR 2): пишет только прозу; HITL-шлюз report относится только к прозе и не блокирует finalize. FAILED/отсутствие report не понижает verdict; study без required-задач (только report) → verdict `partial` (`test_report_not_required_verdict`).
- Вопросы: при ≥1 disposition `not_attempted` verdict не выше `partial`. PR13: `final_type=artifact` ⇔ рубрика MET → disposition `answered` (без claim); study может быть `fulfilled` на качественном артефакте.
- Отчёт (§10) обязательно содержит три раздела: «Сделано» (real), «Выполнено на заглушках» (задачи и входы stub/partial), «Требует доработки человеком» (FAILED, SKIPPED, stub-цепочки с причинами).
Тесты: `test_failed_dep_runs_dependent_on_partial_data`, `test_simulated_input_caps_at_partial` (provenance=stub), `test_simulated_flag_propagates`, `test_report_lists_stub_and_human_sections`.

### 3.5r Восстановление
`recover(study_id)` идемпотентен:
- попытка с route_returned и без результата → запись из сохранённого вывода;
- открытая попытка без ответа: idempotent-инструмент → повтор с тем же dispatch_id; иначе `AttemptClosed(crashed, retryable)` → P3/P4;
- coder job → опрос JobStore по job_id;
- ключи и номера берутся из журнала (R5); истёкшие deadline срабатывают на первом тике (кроме HITL, §7.3).

### 3.5 Хранилище

Один процесс-владелец на study. Хранилище: SQLite (WAL) `data/<study_id>/events.db` + каталог артефактов.

```sql
CREATE TABLE events(
  seq INTEGER PRIMARY KEY,              -- монотонный
  study_id TEXT NOT NULL, type TEXT NOT NULL,
  entity_key TEXT NOT NULL,             -- task_id / attempt_id / result_id
  idem_key TEXT NOT NULL UNIQUE,        -- напр. "ResultRecorded:"+result_id
  payload JSON NOT NULL, ts TEXT NOT NULL, schema_version INT NOT NULL);
```
- Append-only. Проекция строится fold'ом; снапшот проекции каждые N событий (оптимизация, не источник истины).
- Повтор события с тем же `idem_key` и тем же payload_digest → no-op (`Idempotent`); с другим → `Conflict` (старое событие сохраняется).
- Запись события и обновление проекции — одна транзакция.

### 3.5a Писатель, лок, проекции (к концу M1)

- `EventStore` — порт; реализация SQLite WAL, одна БД на study, `busy_timeout=5000`, `synchronous=FULL`.
- Lease на study: строка `lease(study_id, owner, owner_nonce, epoch, expires_at)`, продление каждые 10 с, TTL 30 с. Захват lease увеличивает `epoch` (кроме renew того же экземпляра). Каждый runtime-экземпляр держит уникальный `owner_nonce`; renew / release / fencing требуют совпадения `owner`+`epoch`+`owner_nonce`. Пока lease жив, **новый** процесс с тем же `owner` (другой nonce) получает `LeaseLost` и **не** продлевает lease на месте — после crash (и для мигрированных БД с `owner_nonce=''` на живом lease) повторный захват ждёт TTL. `renew_lease` и `release_lease` при отсутствии nonce или несовпадении строки оба поднимают `LeaseLost` (без тихого no-op).
- Fencing: каждая транзакция записи начинается с `UPDATE lease SET expires_at=? WHERE study_id=? AND owner=? AND epoch=? AND owner_nonce=? AND expires_at>now`; 0 строк → откат и `LeaseLost` (процесс прекращает запись по study). Поэтому владелец, «проснувшийся» после TTL, не может записать ни одного события. Каждое событие хранит `epoch`.
- SQLite I/O не на event loop: async `append`/`append_many`/lease/`upsert_projection` (fenced) — через `asyncio.to_thread` под `threading.RLock`. Чтения (`read_*`, `get_row`/`get_projection`, `current_lease`) — отдельное read-only соединение (WAL) под `_read_lock` через `to_thread`; `close` ждёт in-flight reads. Acquire в `open_runtime` — через `asyncio.shield`: на cancel ждём поток, затем release только если этот open реально захватил lease (`acquired_here`), не из-за seeded `owner_nonce` при `acquire=False`.
- Сериализация строгая: payload события и строки проекций — только JSON-типы (схема / `model_dump(mode="json")`); неизвестный тип → `TypeError` на записи (нет `_json_default` / тихой stringification). `AttemptClosed` обновляет проекцию `tasks` с `retry_at` (ISO-строка).
- Один писатель: все события идут через одну `asyncio.Queue` на study; `tick` (дедлайны и due `retry_at`, запись снимается из `_retry_at` при fire), recover, API-команды, JobStore — только через неё. JobStore пишет `JobSubmitted/JobStatusChanged/JobTimedOut/JobCancelled` через ту же очередь (сам воркер джоба в БД не пишет, только отдаёт статус по опросу).
- Проекции (в той же БД, обновляются в транзакции события): `studies, plans, tasks, attempts, results, artifacts, requirements, jobs, hitl_requests, usage, tool_calls`.
- `schema_version` у события; старые payload неизменяемы; чтение через `upcasters[type][v] -> v+1`. Тест `test_replay_equals_projection`: fold всего журнала с нуля == текущие проекции (на каждом фикстурном журнале и после property-теста).
- ТЗ п.8: ≥10 параллельных задач в ОДНОМ study. Один писатель на study достаточен: LLM/инструменты идут вне очереди, в очередь — только события (≤ 10 КБ, артефакты пишутся в файлы до события). Ожидаемая нагрузка ~ десятки событий/с; ёмкость SQLite WAL с fsync — сотни транзакций/с.
- Очередь записи ограничена (maxsize=1000, backpressure); группировка событий одной попытки в одну транзакцию.
- Нагрузочный тест M0: один study, 20 параллельных попыток (запас ×2 к п.8), фиктивные инструменты 0–2 с, fsync включён, ≥5000 событий; 0 `database is locked`, p99 записи ≤ 50 мс, глубина очереди ≤ 100, `test_replay_equals_projection` после теста.
- Postgres — когда нужны несколько процессов/хостов-писателей, HA или запросы веба через многие study. Порт тот же.

### 3.6 Продолжение сессии (fork)

```python
class Clarification(BaseModel):
    target: str | None          # task_key или None (весь запрос)
    text: str
def continue_session(parent_id: str, clarifications: list[Clarification]) -> str  # новый study_id
```
1. Новый study с `parent_study_id`; журнал родителя не меняется.
2. Наследование задачи только если совпадают `task_key` И `spec_digest` (sha256 канонического JSON RFC 8785 от TaskSpec); иначе задача затронута.
3. Затронутое подмножество: задачи-цели уточнений + все их потомки в DAG; уточнение без цели → задачи spec и plan + потомки.
4. Если затронут plan — перепланирование с инструкцией сохранить неизменённые задачи; задачи сопоставляются по `task_key`, новые получают ключи `EXP-(max+1)…` (нумерация продолжает родителя).
5. Незатронутые задачи создаются сразу DONE с `inherited_from` и ссылками на результаты/артефакты родителя (событие `TaskInherited`, без запуска). Затронутые — PENDING.
6. Идемпотентность: `idem_key = f"fork:{parent_id}:{jcs_sha256(clarifications)}:{nonce}"`; nonce по умолчанию пуст — повторный вызов возвращает тот же study_id; явный nonce — новый прогон с тем же текстом. Внутри — `TaskInherited:{task_key}`.
7. GC родителя не удаляет артефакты, на которые ссылается `TaskInherited` любого потомка (ref-count по журналам потомков).
8. Наследуемая задача сохраняет provenance своих попыток. При другом catalog_version у ребёнка унаследованные NodeRequirement помечаются STALE; наследуемая задача с STALE required-проверкой считается затронутой и перезапускается (M2).
9. Fork после human_timeout — штатный путь возобновления (§7.3): закрытый гейт и его потомки затронуты.
Тесты: `test_fork_reruns_only_affected_subgraph`, `test_fork_keys_stable_no_duplicates`, `test_fork_idempotent`.

### 3.7 Падение процесса на каждом шаге

| Падение между | В журнале | recover |
|---|---|---|
| до `AttemptStarted` | PENDING | планировщик запустит |
| `AttemptStarted` и ответом агента | открытая попытка | idempotent → повтор с dispatch_id; иначе crashed → P3/P4 |
| ответом агента и `RouteReturned` | открытая попытка | как выше |
| `RouteReturned` и `TaskDone` | route_returned=true | запись из сохранённого вывода |
| внутри транзакции записи | откат | как выше |
| файл артефакта записан, события нет | `tmp/<call_id>` | GC удалит после 24 ч без живой попытки |
| `TaskInherited` частично | часть задач fork | повтор fork с тем же idem_key дописывает недостающие |

### 3.8 Артефакты (к концу M1)
- Хранение content-addressed: `cas/<sha256>`; `Artifact` — запись (id, имя, produced_by) поверх содержимого.
- Чтение (`read_artifact`, API, проверки) сверяет sha256; несовпадение → `ArtifactCorrupt`, проверка FAILED, событие.
- Digest значения по JSON pointer = sha256 канонического JSON RFC 8785 (JCS).
- То же содержимое с другим produced_by: блоб один, записей Artifact две (artifact_id по §2 различаются call_id-частью); провенанс не перезаписывается.
- Артефакт, пришедший от попытки с `closed=true` (закрыта по результату, отмене, deadline), отклоняется `ArtifactRejected(attempt_closed)`.

## 4. Запись результата

### 4.1 Поток

```python
class AgentTaskOutput(BaseModel):           # структурированный вывод агента маршрута
    claim: Outcome | None                   # мнение агента
    summary: str
    outputs: dict[str, ArtifactHandle]      # имя OutputSpec -> артефакт, созданный инструментом
    values: dict[str, Any] = {}             # метрики для metric_threshold
    claims: list[Claim] = []                # утверждения с EvidenceRef

class Executor:
    async def run_attempt(self, attempt_id: str) -> Result: ...
    def record(self, attempt_id: str, out: AgentTaskOutput) -> Result:   # единственный путь записи
```
1. Агент маршрута возвращает `AgentTaskOutput` (typed `output_schema` роли). Невалидный JSON → один повтор с ошибкой схемы, затем Result(failure, reason=schema_invalid).
2. `ArtifactHandle` должен ссылаться на артефакт, уже зарегистрированный слоем инструментов (§6). Агент не создаёт артефакты текстом.
3. `Executor.record` → `Checks.evaluate(task, out)` → `outcome = compute_outcome(...)` → события `ResultRecorded` (с check_results) и `TaskDone` или `AttemptClosed(retryable)` в одной транзакции.
4. Внутри той же транзакции ставятся в очередь ReviewCheck (§5). Это единственный путь записи, поэтому проверки качества идут всегда (замена хука plugin.py:351).
5. Инструмента `record_result` у LLM нет.

### 4.2 Вычисление статуса

```python
def compute_outcome(reqs, claim, reason, provenance: str) -> tuple[Outcome | None, Downgrade | None]:
    """None = ждём CodeCheck/ReviewCheck; задача RUNNING. Чистая функция."""
    req = [r for r in reqs if r.required]
    if not req:
        raise CatalogError("no required requirements")   # §4.2: где ловится
    if any(r.status == PENDING for r in req):
        return None, None
    ok = {MET, WAIVED, NOT_APPLICABLE}
    code = [r for r in req if r.is_code]; review = [r for r in req if not r.is_code]
    if any(r.status in {FAILED, MIGRATION_ERROR} for r in req):
        base = PARTIAL if any(r.status in ok for r in req) else FAILED
    elif all(r.status in ok for r in code) and all(r.status in ok | {UNASSESSED} for r in review) and code:
        base = SUCCESS              # UNASSESSED review не понижает при всех MET CodeCheck (B2b); виден в Result.unassessed
    elif all(r.status in ok for r in req):
        base = SUCCESS
    else:
        base = PARTIAL              # только ReviewCheck и все UNASSESSED
    if provenance != "real": base = min(base, PARTIAL, key=RANK)    # §3.4
    if claim and RANK[claim] < RANK[base] and reason:
        return claim, Downgrade(from_=base, to=claim, reason=reason)   # B2a
    return base, None               # понижение без reason игнорируется и логируется
```
- Задача терминальна только когда все required-проверки получили вердикт или истёк review_deadline (тогда ReviewCheck → UNASSESSED; влияние на outcome — строго по compute_outcome выше; R23 показывается отдельно).
- Пустой список required: план с такой задачей отклоняется при принятии (`PlanValidator`, `CatalogError` → попытка задачи plan FAILED(retryable)). В рантайме (`Executor.record`) `CatalogError` ловится в `record()`: Result(outcome=PARTIAL, reason=catalog_error), событие `CatalogErrorRaised`; никогда SUCCESS.
- Закрытая попытка: `record()` первым шагом проверяет `attempt.closed`; при true → `AttemptClosedError`, ничего не пишется (событие `LateWriteRejected` для трассировки). Проверка повторяется внутри транзакции записи (`require_open_attempt`). Повторный `record` после успешной записи (попытка уже closed через Result+Close+Done) тоже даёт `AttemptClosedError` — не `Idempotent`: идемпотентность журнала относится к повторной доставке того же `idem_key`, а не к повторному вызову `record` с новым payload.
- `AgentTaskOutput` получает поле `downgrade: DowngradeClaim | None`, где `DowngradeClaim(to: Outcome, refs: list[str | EvidenceRef])` — непустой список id CheckResult или EvidenceRef из этой попытки; свободный текст без refs отклоняется (M1).
- B2: `metric_threshold`/`value_matches` на сыром JSON-артефакте с EvidenceRef — достаточное доказательство (MET). ReviewCheck не может перевести MET CodeCheck в FAILED; он добавляет свой отдельный результат.

Тесты: `test_b1_closed_only_when_all_tasks_done`, `test_b2_matching_value_in_raw_artifact_is_success`,
`test_agent_claim_only_downgrades`, `test_rt1_task_linked_to_plan` (RT1: в старом completion.py:165 задача не связана с планом — у каждого experiment ровно 1 входящее `plans`), `test_h1_hypothesis_gets_verdict` (H1: старый completion.py:95 закрывал исследование без вердикта — при DONE всех tests-экспериментов вердикт ≠ untested), `test_recovery_stage_task_not_rerun` (задача, записанная внутри стадии, при recover не перезапускается: 0 новых попыток, 0 новых узлов), `test_b2a_article_small_claim_partial_without_reason_stays_success`, `test_b2a_article_small_downgrade_with_reason_stored`, `test_b2b_article_small_unassessed_review_does_not_downgrade`, `test_empty_required_never_success`, `test_review_scheduled_on_every_record_path`.

## 5. Каталог требований

### 5.1 Типы

```python
class RuleRef(BaseModel):          # текст правила, не проверка
    rule_id: str                   # "R11", "ENG:registration_integrity"
    source: str; excerpt: str; digest: str

class MethodSource(BaseModel):     # Том Б
    id: str; volume: str; section: str; edition: str; sha256: str

class CodeCheck(BaseModel):
    kind: Literal["artifact_exists","schema_valid","json_pointer","source_link",
                  "metric_threshold","value_matches","graph_shape","quote_in_source",
                  "min_sources","question_coverage","recency",
                  "exec_runs","tests_pass","metric_on_holdout","reproducible_rerun",
                  "rubric"]  # = schema/graph_schema.yaml code_check_kinds; rubric = M1 PR13
    output: str | None; schema_ref: str | None; pointer: str | None
    metric: str | None; op: Literal["<","<=","==",">=",">","in"] | None; target: Any = None
    tolerance: float | None = None
    # M1 PR17 executable kinds (sandbox limits)
    command: str | None = None; argv: list[str] | None = None
    timeout_s: float | None = None; memory_mb: int | None = None
    max_output_bytes: int | None = None; allow_network: bool = False
    min_tests: int | None = None
    holdout_output: str | None = None; holdout_digest: str | None = None
    rerun_tol: float | None = None
# Sandbox net_isolation + fs_isolation hard|soft|none on Artifact + SandboxResult; trusted only if both hard (soft/none ⇒ trusted=false + note).

class ReviewCheck(BaseModel):
    reviewer_role: Literal["reviewer","critic","human"]
    question: str; max_calls: int = 1

class RequirementTemplate(BaseModel):
    id: str; title: str
    method_sources: list[str]; rule_ids: list[str]
    applies_to: list[NodeKind]; check: CodeCheck | ReviewCheck; required: bool
    version: str

class Catalog(BaseModel):
    catalog_version: str           # semver
    edition: str                   # "Том Б, 19.08.2026"
    sources_sha256: dict[str, str]
    rules: list[RuleRef]; templates: list[RequirementTemplate]
    defaults: dict[NodeKind, list[str]]     # шаблоны по умолчанию

class ReqStatus(StrEnum): PENDING; MET; FAILED; WAIVED; NOT_APPLICABLE; UNASSESSED; MIGRATION_ERROR; STALE

class NodeRequirement(BaseModel):
    id: str; template_id: str; node_id: str; params: dict = {}
    required: bool; status: ReqStatus
    evidence: list[EvidenceRef] = []
    verdict: ReviewVerdict | None = None
    waived_by: str | None = None    # id HITL-решения
    check_version: str

class ReviewVerdict(BaseModel):
    decision: Literal["pass","fail","unassessed"]; reasons: list[str]
    evidence: list[EvidenceRef]     # непусто для pass (валидирует код)
    reviewer: str; model: str
```

### 5.2 Валидация каталога (при загрузке; ошибка = отказ старта)
- каждый RuleRef покрыт ≥1 шаблоном; каждый NodeKind имеет `defaults`;
- неизвестный `kind` / `reviewer_role` / NodeKind → `CatalogError`, не PENDING;
- шаблон без method_sources и rule_ids → ошибка.

### 5.3 Источники → каталог

| Источник | Как попадает в каталог |
|---|---|
| Том Б: 14 требований (problem, goals, planning, resources, knowledge, data, config, model, execution, validation, comparison, application, trace, report) | 14 шаблонов, method_sources = пункт Тома Б; маппинг проверок — таблица «Сопоставление» на карте |
| plan_criteria (check_kind) | критерий плана → NodeRequirement на узле задачи по §5.5 |
| reviewed-rules.json: R02, R04, R05, R06, R11×2, R14, R16, R17×2, R18, R23, R24, R28, R29, R30, ENG | RuleRef; привязка через `rule_ids` шаблонов; R05, R23, R24, R28 реализованы как инварианты ядра и правило агрегации, их шаблоны — CodeCheck над журналом |

### 5.4 Агрегация статуса узла
- любой required FAILED/MIGRATION_ERROR → узел failed (R24, без усреднения);
- иначе любой required PENDING → in_progress;
- все required MET/WAIVED/NOT_APPLICABLE → done; UNASSESSED показывается отдельно (R23);
- optional не влияют на статус.
- Смена catalog_version: старые NodeRequirement → STALE, пересчёт только явной командой.

### 5.5 Миграция plan_criteria

| старое | новое |
|---|---|
| check_kind=artifact_exists | CodeCheck(artifact_exists) |
| json_pointer_exists | CodeCheck(json_pointer) |
| json_schema | CodeCheck(schema_valid) |
| null + metric/operator/target | CodeCheck(metric_threshold) |
| null без метрики | ReviewCheck(question=condition) |
| неизвестный | MIGRATION_ERROR + список в отчёте миграции; для required блокирует |

## 6. Слой инструментов

### 6.1 Контракт

```python
class ToolSpec(BaseModel):
    name: str; provider: Literal["mcp","local","coder","alembic"]
    input_schema: dict; output_schema: dict | None
    # PR18 / DESIGN_M1 §8: effect replaces read_only (default read).
    effect: Literal["read","write","external_effect"] = "read"
    idempotent: bool = False
    supports_idem_key: bool = False   # recover may re-call after EffectIntended without Done
    timeout_s: float = 120; max_retries: int = 2

class ToolError(Exception):
    kind: Literal["local","transport_unsent","transport_sent","opaque"]
    retryable: bool; detail: str

class ToolCallRef(BaseModel):
    call_id: str                  # = f"{attempt_id}:{seq}"
    tool: str; args_digest: str; started_at: datetime; ended_at: datetime | None

class Tool(Protocol):
    spec: ToolSpec
    async def call(self, args: dict, ctx: CallCtx) -> ToolOutput: ...

class ToolOutput(BaseModel):
    data: Any; artifacts: list[Artifact]    # обёртка сама регистрирует артефакты и считает sha256
```
- **Единственный путь вызова:** `coscientist.tools.invoke_tool` (агентский wiring — позже). Прямой `Tool.call` обходит гейты — запрещён вне тестов; import-linter: api/checks/experimental не импортируют `tools.contract` / `tools.effects` напрямую; `coscientist.agents` — без `tools.contract` / `core.tool_registry`.
- Класс эффекта — **закрытый** `core.tool_registry`: регистрирует только `core.tool_loader` (gate), затем `freeze_registry` → `_REGISTRY` = `MappingProxyType`; незарегистрированное имя → `external_effect`. `ToolSpec.effect` — advisory only.
- **M2 follow-up (границы AST / import-linter):** AST-тест не ловит aliases / `getattr` / вызовы через переменную; импорт `core.tool_registry` из других non-agents пакетов пока не запрещён; контракт `agents` станет содержательным, когда в `coscientist.agents` появится код.
- `write` / `external_effect` только через HITL-гейт `side_effect` (headless default `deny` → `SideEffectDenied` + строка в отчёте). Taint deny — тоже `SideEffectDenied(reason=taint_deny)`.
- `effect_idem_key` включает `attempt_id` для завершённых эффектов; **read не индексируется**. Перед вызовом: любой `EffectIntended` без `EffectDone` с тем же `(study, task_key, tool, args_digest)` на **любой** attempt → opaque/unknown, повтор запрещён (exactly-once). `transport_unsent` → `EffectDone(failed)`.
- Taint: `AttemptStarted.tainted_context` из журнала (не от вызывающего); наследование на derived/sandbox через per-attempt `ContextVar` (не shared store).

### 6.2 Правила повтора

| kind | значение | повтор |
|---|---|---|
| local | ошибка аргументов/схемы | нет; ошибка возвращается агенту один раз |
| transport_unsent | запрос точно не ушёл (connect refused, DNS) | да, backoff, до max_retries |
| transport_sent | запрос ушёл, ответа нет (timeout, reset) | только если idempotent=True или effect=read |
| opaque | непонятно | нет; попытка → failure(retryable=True) |

Таймаут на вызов — `timeout_s`; таймаут попытки — `Attempt.deadline` (сумма бюджета маршрута).

### 6.3 Маршрутизация и alembic-fallback

```python
class ToolRouter:
    def resolve(self, need: ToolNeed) -> Resolution:
        # 1) каталог MCP (по имени и семантическому поиску) → TOOL_FOUND
        # 2) нет → AlembicJob (repo_search → build → validate → register) → TOOL_BUILT
        # 3) alembic failed/disabled → CoderJob → CODER
```
- `Resolution` пишется событием `ToolResolved(task_id, status, tool|job_id)`: каждая задача имеет ровно один статус tool_found / tool_built / coder / tool_missing (возможность #8).
- Политика маршрутизации — в профиле: `alembic: enabled|disabled`. R3 проверяется фикстурой с пустым MCP-каталогом (`EmptyMcpCatalog`), а не флагом профиля.
- Зарегистрированный alembic инструмент добавляется в каталог с версией и снова идёт через путь 1.

### 6.4 Coder и alembic как асинхронные задачи (R4)

```python
class Job(BaseModel):
    job_id: str; kind: Literal["coder","alembic"]; attempt_id: str
    status: Literal["queued","running","succeeded","failed","timed_out","cancelled"]
    deadline: datetime; artifacts: list[str]

class JobStore(Protocol):
    def submit(self, kind, spec, attempt_id, deadline) -> str      # идемпотентно по (attempt_id, spec_digest)
    def get(self, job_id) -> Job
    async def wait(self, job_id, until: datetime) -> Job          # ограничено deadline
    def cancel(self, job_id) -> None
```
- job_id берётся из результата submit и хранится в событии `JobSubmitted`; не парсится из текста ответа.
- `wait` никогда не превращает timed_out в running. По deadline → `JobTimedOut` → попытка failure(retryable=True).
- Окончание хода агента не оставляет открытых задач: открытые jobs отменяются или ожидаются исполнителем до deadline.
- Бэкенды кодера: local sandbox, HTTP code_exec — один интерфейс.
- `JobTimedOut` всегда сопровождается `cancel(job_id)` и событием `JobCancelled` (или `JobCancelFailed` с причиной; попытка всё равно закрыта).
- Не больше одного живого job на попытку: `submit` при живом job той же попытки → `local` ToolError.
- Артефакты job, пришедшие после закрытия попытки, отклоняются (§3.8).

### 6.5 Опциональные интеграции (A2A, FEDOT.MAS, codesynapse и др.)
- Только внешние сервисы за контрактом Tool (MCP или HTTP-адаптер в `tools/integrations/<name>.py`, ≤ 300 строк), включаются в профиле `integrations: [...]`. В ядро не вендорятся, без них всё работает.
- FEDOT.MAS: Tool `fedotmas.run_pipeline` (provider=mcp\|http, read_only=false, idempotent=false), выход — артефакт с результатом; как любая задача плана.
- A2A входящий: `api/a2a.py` — фасад над `POST /studies` и потоком событий (agent card, tasks → study). A2A исходящий: удалённый агент = Tool (provider=http, адаптер A2A-клиента).
- MCP-фасад: модули CoScientist (гипотезы, литература, план) публикуются как MCP-инструменты, вызывающие API.

## 7. Агентный слой

Решение (2026-10-24): **без Google ADK**. Агентный слой — детерминированный оркестратор + тонкий LLM-клиент (`coscientist/llm/`). **Всё состояние агентов живёт в журнале событий** (event journal): при падении/recover агент не держит свой dict статусов, pending или retry — live ≡ replay из журнала. LLM только предлагает типизированные данные; переходы и запись результата делает ядро.

### 7.1 Роли (тонкие агенты, typed I/O)

| Роль | Вход | Выход (output_schema) | Инструменты |
|---|---|---|---|
| hypothesizer | StudyContext | HypothesesOut(hypotheses: list[Hypothesis]) | литература read_only |
| researcher | StudyContext, Hypothesis?, query_plan | LiteratureOut(papers, quotes, claims) | papers_search, download, parse |
| planner | StudyContext, LiteratureOut, Hypothesis | PlanOut(tasks: list[TaskSpecDraft]) | каталог инструментов read_only |
| critic | PlanOut | CriticVerdict(decision: accept\|revise\|reject, issues) | нет |
| executor-route | TaskSpec, контекст зависимостей | AgentTaskOutput | инструменты задачи (через ToolRouter) |
| reviewer | ReviewCheck, EvidenceRef[] | ReviewVerdict | read_artifact |
| reporter | проекция (только чтение) | ReportProse(sections с evidence ids) | нет |

- Порядок вызовов ролей задаёт ядро (`Orchestrator` — код, не LLM).
- Модель на роль (+ резерв) задаётся профилем и передаётся в запрос клиента; в коде клиента нет имён моделей и угаданных лимитов.

### 7.2 Тонкий LLM-клиент (`coscientist/llm/`)

```
core/     — не импортирует coscientist.llm, google.adk, httpx (import-linter)
llm/      — не импортирует coscientist.core (import-linter)
agents/   — вызывает llm + tools.invoke_tool; не хранит состояние вне журнала
```

```python
class LlmConfig(BaseModel):  # frozen; strict; все поля обязательны — без defaults в коде
    base_url: str; api_key_env: str
    chat_completions_path: str    # relative, e.g. "chat/completions"
    max_tokens_field: str         # JSON field name for completion cap
    timeout_s: float              # на одну попытку; ≤ total_deadline_s
    total_deadline_s: float       # wall-clock на все попытки + backoff
    max_retries: int
    retry_backoff_s: float; retry_backoff_multiplier: float
    retry_backoff_jitter_s: float; retry_backoff_max_s: float  # ≥ retry_backoff_s
    retry_on_timeout: bool; honor_retry_after: bool
    retryable_status_codes: tuple[int, ...]  # no 2xx, no duplicates
    max_completion_tokens: int
    accepted_finish_reasons: tuple[str, ...]  # иначе LlmFinishReasonError
    unexpected_transport_retryable: bool

class LlmRequest(BaseModel):
    model: str; messages: list[ChatMessage]

class Usage(BaseModel):
    prompt_tokens: int; completion_tokens: int
    latency_ms: float  # wall-clock LlmClient.complete (retries+backoff)

class LlmResponse(BaseModel):
    content: str; model: str; finish_reason: str; usage: Usage

class ProviderAdapter(Protocol):
    async def complete(self, request: LlmRequest, *, timeout_s: float) -> LlmResponse: ...

class Transport(Protocol):  # тесты — fake; сеть не в unit-тестах
    async def request(self, method, url, *, headers, body, timeout_s) -> TransportResponse: ...
```

- **Успех HTTP = 200 только.** Любой другой status → `LlmProviderError` (не 2xx «успех»).
- Реализация: `OpenAICompatibleProvider` + `LlmClient` с обязательными инъекциями `clock`, `sleep`, `wait_for`, `rng` (без `time.perf_counter` / `asyncio.sleep` в клиенте). Retry/timeout/deadline по `LlmConfig`; exponential backoff + jitter (потолок `retry_backoff_max_s` только для backoff).
- `Retry-After` парсится **только** если `honor_retry_after` и status ∈ `retryable_status_codes` (иначе заголовок игнорируется и не маскирует `LlmProviderError`). Формат: RFC 7231 `delta-seconds` = ASCII `1*DIGIT` (int) **или** strict IMF-fixdate (`… GMT`, timezone обязателен; без GMT → `LlmRetryAfterError`). Дата в прошлом → delay **0** (RFC 9110 §10.2.3). Невалидный `Retry-After` → `LlmRetryAfterError`. Значение не clamp’ится к `retry_backoff_max_s` — только к `total_deadline_s`. `clock.wall_utc()` обязан быть timezone-aware (проверка в коде).
- `attempt_timeout` (= min(timeout_s, remaining deadline)) передаётся и в `wait_for`, и в `provider.complete` → `transport.request(..., timeout_s=...)`.
- Usage (prompt/completion tokens + latency) возвращается на каждый успешный `complete`.
- Типизированные ошибки: `LlmConfigError`, `LlmTimeoutError`, `LlmTransportError`, `LlmProviderError`, `LlmResponseError`, `LlmFinishReasonError`, `LlmRetryAfterError`. Нет угадывания retryable по тексту/форме ответа — только флаги и `retryable_status_codes` из config. `finish_reason` ∉ `accepted_finish_reasons` → ошибка (не silent success для `length`/`content_filter`).
- Секреты только из env; тела запросов/ответов и ключи не логируются и не печатаются.
- import-linter: `llm` ↛ `core`/`tools`/`agents`/`api`/`checks`/`experimental`/`google`/`httpx`.
- Порт агента (поверх клиента; wiring в M2):

```python
class AgentPort(Protocol):
    async def run(self, role: str, input: BaseModel, tools: list[Tool], budget: Budget) -> AgentRun
class AgentRun(BaseModel):
    output: BaseModel | None; error: str | None; usage: Usage; tool_calls: list[ToolCallRef]
```

### 7.3 HITL как состояние (R2)

```python
class HitlPolicy(BaseModel):
    mode: Literal["interactive","auto","headless"]
    gates: dict[str, GateConfig]       # включённые шлюзы, профиль; RESEARCH_MODEL.md §5.2
    # spec_approval, plan (маршрут), hypotheses (после HYP-n), costly_or_nonreal (перед EXP costly или provenance≠real),
    # report, result_acceptance, skip_task, side_effect (write/external_effect tools; headless default deny)

class GateConfig(BaseModel):
    enabled: bool = True
    timeout_s: int                     # spec 120, plan 180 (до плана ≤ 300 с на человека), прочие 1800
    on_timeout: Literal["delegate","delegate_capped","fail"] = "delegate"   # delegate: решает critic, «timeout→agent»
    # costly_or_nonreal: по умолчанию "fail"; delegate_capped — critic в пределах Budget(delegated_costly), ₽;
    # delegate без лимита — только явно в профиле. Явное «реши сам» подчиняется тем же режимам.

class HitlRequest(BaseModel):
    request_id: str; kind: str         # имя шлюза из HitlPolicy.gates
    payload_ref: str; deadline: datetime
class HitlDecision(BaseModel):
    request_id: str; decision: Literal["accept","revise","reject","delegate"]; author: str
    approved_by: Literal["human","critic"]; delegated_by: Literal["human","timeout"] | None; reason: str
    edits_ref: str | None              # plan и hypotheses: новая ревизия + supersedes
```
- Решение — событие `HitlDecided` с автором; на узле (plan, hypothesis, report, experiment) хранится `approved_by`, `delegated_by`.
- headless/auto: запрос к человеку не создаётся; решение = вердикт critic/reviewer; `approved_by="critic"`, `delegated_by=None`.
- interactive, «реши сам»: человек делегирует → решает critic; `approved_by="critic"`, `delegated_by="human"`.
- interactive, таймаут: `on_timeout=delegate` (по умолчанию) → решает critic, событие `HitlTimedOut`, `delegated_by="timeout"` (в веб и отчёте «timeout→agent»). `on_timeout=fail` → задача DONE(FAILED, human_timeout); потомки → DONE(SKIPPED, input_unavailable) без заглушек (§3.4 п.5); study verdict failed, возобновление через fork. result_acceptance по таймауту → принимается вычисленный outcome; skip_task → не пропускать.
- Шлюз costly_or_nonreal: `fail` (по умолчанию) — таймаут → FAILED(human_timeout), потомки SKIPPED, продолжение fork; явное «реши сам» — только при заданном `Budget(delegated_costly)` и оценке ≤ остатка, иначе delegation_disabled с причиной человеку сразу (RESEARCH_MODEL §5.2); `delegate_capped` — решает critic, траты из `Budget(delegated_costly)` (счётчик: списывается факт, факт > оценки блокирует делегирование), при отказе DONE(SKIPPED, budget); `delegate` — critic без лимита. Всегда событие `HitlDecided`/`HitlTimedOut` с reason и `spent_rub`; отчёт и веб показывают, что решил critic, и сумму.
- Ключи задач: `PLAN-0..n`, `HYP-n`, `LIT-n`, `EXP-n` (нумерация с 0 в каждой серии). Перепланирование: каждая задача PLAN-n, n = 1..`max_replans`, имеет deadline ≤ study.deadline и долю бюджета study (`Budget.replan_share`); исчерпание max_replans → planner работает с текущей ревизией.
- Шлюз plan показывает plan.literature/plan.hypotheses с причинами и список LIT/HYP/EXP; правка маршрута (добавить/удалить задачу) = новая ревизия plan + `supersedes`, ключи уже выданных задач не меняются (R5).
- Бюджет 10 мин ТЗ до плана: deadline задачи plan = study.created_at + 600 с; HITL-таймауты spec и plan вычитаются из него и обрезаются его остатком.
- После простоя deadline не срабатывает мгновенно: при recover `hitl_deadline = min(max(исходный, now + grace_s=60), task.deadline, study.deadline)` и пишется `HitlDeadlineExtended`. Продление никогда не выходит за deadline задачи и study; если они уже истекли — срабатывает P5.
- headless/auto: таймаута нет — решение принимает critic/reviewer сразу (accept_if_critic_accepted; reject → DONE(FAILED)).
- Отсутствие ревьюера = ошибка конфигурации при старте, а не `fallback_reviewer_unavailable` в рантайме.
Тесты: `test_r2_headless_plan_review_completes_without_human`, `test_hitl_delegate_records_critic_and_delegated_by`, `test_hitl_timeout_delegate_marks_timeout_agent`, `test_hitl_timeout_fail_skips_dependents`, `test_plan_edit_creates_revision_derived_from`.

### 7.4 Budget (#24)
```python
class Budget(BaseModel):
    max_llm_calls: int; max_tokens: int; max_cost_rub: Decimal | None
    max_tool_calls: int; deadline: datetime
```
Бюджеты: на study, на попытку (из RouteConfig), на роль. Исчерпание (достижение потолка, `spent ≥ max_cost_rub`) → `BudgetExceeded(scope)`: попытка failure(retryable=False), при исчерпании бюджета study все не-DONE задачи → DONE(SKIPPED, budget) (P6), study закрывается с verdict partial/failed. На `create_study` `Budget.delegated_costly` (если задан вместе с `max_cost_rub`) обязан быть `≤ max_cost_rub` — проверка только при создании, не при fold/`model_validate`, чтобы старые журналы с более широким `delegated_costly` всё ещё доходили до `StudyFinalized`.

**M1 note.** Core persists and folds `BudgetExceeded(scope=study)`; tick finalizes with `trigger=budget`. Production producer: `BudgetDebited.spent_rub` (via public `record_budget_debit` / costly HITL settle) accumulates as `Decimal` on the runtime; when `spent_rub ≥ Budget.max_cost_rub` (reaching the cap counts), core emits `BudgetExceeded(study)` (also re-checked on `tick` after reopen). Task-level `TaskDone(budget)` from the delegated_costly gate still does **not** close the study.

### 7.5 ТЗ исследования (#1)
Роль `tz_spec`: вход — запрос, выход `ResearchSpec(object, goal, scope, constraints, success_metrics)`. Задача kind=spec — первая в DAG; `spec_approval` по политике HITL; FAILED → потомки работают по §3.4 (plan строится на запросе как есть).

## 8. Профили (R1)

```python
class Profile(BaseModel):
    name: str
    roles: dict[str, RoleConfig]            # модель, промпт, инструменты
    mcp_servers: list[McpServerConfig]
    routes: dict[str, RouteConfig]          # route -> роль + бюджет
    catalog_extensions: list[str]           # доп. шаблоны требований
    alembic: Literal["enabled","disabled"] = "enabled"
    integrations: list[str] = []            # опциональные внешние сервисы (§6.5)
    plugins: list[str] = []                 # entry points "coscientist.profiles"

class ProfilePlugin(Protocol):
    def register(self, reg: Registry) -> None   # только: tools, check kinds, templates, routes
```
- Профили: `research` (гипотезы + литература), `medicine` (маршрут medical + med_tools), `microfluidics`.
- Плагин не меняет машину состояний и не вешает callbacks. Новый CodeCheck kind регистрируется с функцией и схемой параметров; неизвестный kind в каталоге — ошибка старта.
- Study создаётся одной функцией для всех профилей: корень (REQUEST/STUDY) создаётся ядром до запуска любых агентов. Тест: `test_r1_every_profile_starts_and_has_root`.

## 9. API для веба

FastAPI, только чтение проекции и журнала + команды.
```
POST /studies                          {profile, request, hitl_policy} -> {study_id}
                                       # Idempotency-Key: repeat → same study_id (K13 / PR18)
GET  /studies/{id}                     -> StudyView (проекция)
GET  /studies/{id}/nodes               -> list[NodeView]  (статус + requirements)
GET  /studies/{id}/artifacts/{aid}     -> файл | 302 presigned S3
GET  /studies/{id}/report?format=md|html|json
POST /studies/{id}/hitl/{request_id}   HitlDecision
POST /studies/{id}/cancel
GET  /studies/{id}/events?after=<seq>  -> список событий
WS   /studies/{id}/stream?after=<seq>  -> события в порядке seq
GET  /studies/{id}/usage               -> UsageReport
```
- Веб не импортирует core-внутренности: только `api/` и схемы `schemas/` (публичные view-модели).
- Веб переписывается последним (M6); до этого — CLI и API.

## 10. Наблюдаемость

```python
class Usage(BaseModel):
    prompt_tokens: int; completion_tokens: int; cached_tokens: int = 0
    cost_rub: Decimal | None; model: str
class UsageRecorded(Event):   # на каждый LLM-вызов
    study_id: str; task_id: str | None; attempt_id: str | None; role: str; usage: Usage
class ToolCalled(Event):      # на каждый вызов инструмента
    ref: ToolCallRef; outcome: Literal["ok","error"]; error_kind: str | None; duration_ms: int
```
- `cost_rub=None` (нет цены модели): стоимость в отчёте = «неизвестно», токены считаются; порог стоимости в §12 считается по токенам × цене из pricing.yaml, а при отсутствии цены сценарий помечается `cost_unverified`, не pass.
- Цена из `pricing.yaml` (модель → ₽ за 1M токенов); при отсутствии ответа провайдера о стоимости.
- `UsageReport` = суммы по study, по task, по role, по model.
- `RunReport` (JSON + MD): статусы задач, check_results, инварианты, usage, tool calls, длительности, метрики ТЗ (время до плана, доля валидных артефактов).
- Трассировка: OpenTelemetry-спаны study → attempt → agent run → tool call (экспорт опционален; opik по ТП — опция).

## 11. Структура пакетов и бюджет размера

```
coscientist/
  core/        domain.py events.py store.py machine.py recover.py executor.py scheduler.py   ≤ 3 000
  checks/      catalog.py evaluate.py kinds/*.py migrate.py                                   ≤ 1 500
  tools/       contract.py router.py mcp.py local.py jobs.py coder/ alembic/                  ≤ 2 500 (+alembic pipeline ≤ 3 000)
  llm/         thin OpenAI-compatible client (no ADK; no core imports)                         ≤ 800
  agents/      adapter.py roles.py prompts/*.md schemas.py                                    ≤ 1 200
  profiles/    research/ medicine/ microfluidics/                                             ≤ 1 500 каждый
  reporting/   report.py nir.py render_md.py render_html.py                                   ≤ 1 200
  api/         app.py views.py stream.py                                                      ≤ 800
  obs/         usage.py tracing.py                                                            ≤ 400
web/           отдельно (M6)
```
- Файл ≤ 500 строк, функция ≤ 60. Превышение — ревью архитектуры, не исключение.
- Оценка ядра без профилей и alembic ~10–12k; это бюджет, не измерение.
- Зависимости: `core` ← `checks` ← `tools`; `llm` ↛ `core`; `agents` ← (`tools`, `llm`) ← `api`. import-linter в CI.

## 12. Паритет с эталоном

### 12.1 Какой эталон
- Сравнение идёт с ОЖИДАЕМЫМ поведением карты (поле expected), а не с числами старой системы.
- Эталонные прогоны: `rerun_cossessions/baseline_20261022` (старый код, 29c5831) — источник входов, артефактов и observed.
- Где observed ≠ expected (B1, B2, R1–R5) — цель = expected. «Исправленный старый» (после шагов 1–3) — дополнительный ориентир для стоимости, если появится.

### 12.2 Сценарии и пороги

| Сценарий | Вход | Проверка (expected) | Порог |
|---|---|---|---|
| two_tasks (×3) | запрос эталона | 2/2 задачи DONE(SUCCESS), verdict fulfilled; артефакты dataset_overview и chemical_space_clustering по схеме; 2/2 tool_found | 3/3 прогона; 0 ложных статусов |
| file_handoff (×3) | эталон | 4 задачи; артефакт задачи N читается задачей N+1 по artifact_id | 3/3 |
| borshevik | эталон | 6 задач; verdict fulfilled только если все required SUCCESS; провалившиеся зависимости → потомки на partial/stub, ≤ PARTIAL | 0 ложных fulfilled (B1) |
| article_small | эталон | 225 соединений = 225 → SUCCESS, verdict fulfilled | fulfilled (B2) |
| microfluidics | эталон | старт без ошибки, задача выполнена | (R1) |
| hypothesis_check (headless) | эталон | ревью без человека; вывод «гипотеза опровергнута» (медиана −6 vs −4) | (R2) |
| alembic_nomcp | фикстура EmptyMcpCatalog, профиль research | ToolResolved=tool_built, инструмент зарегистрирован и вызван | (R3) |
| coder_timeout | coder job дольше deadline | попытка failure(retryable) ≤ deadline+тик | (R4) |
| continue_session | borshevik + уточнение к одной задаче | перезапущены только она и потомки; ключи стабильны; 0 дублей | — |
| recovery | kill -9 после RouteReturned и после Dispatched; kill -9 после записи задачи внутри стадии | уникальные EXP-n, 0 дублей попыток, результат записан 1 раз; записанная задача не перезапускается | (R5) |
| стоимость | two_tasks | токены и ₽ по прогону и задаче записаны | ≤ эталонной стоимости (после сбора токенов эталона) |

### 12.3 Тесты
- Юнит: машина состояний (каждая строка §3.3 и §3.7 — тест), compute_outcome, каталог, router, jobs.
- Характеризационные: входы/артефакты эталона как фикстуры; LLM заменён записанными ответами (cassette) — детерминированный CI.
- Живые: сценарии §12.2 с реальной моделью, ночью; текст LLM не сравнивается, сравниваются статусы, артефакты и проверки.
- Возможность принята = все её тесты зелёные + её сценарий 3/3.

### 12.4 Стабильность

Каждый эталонный сценарий §12.2 повторяется N ≥ 3 раз (ночные живые прогоны — N = 5) на новом и на старом коде (29c5831).

| Метрика | Как считается | Порог приёмки (новый код) |
|---|---|---|
| Согласие исходов | доля прогонов, где verdict study и outcome каждой required задачи = expected | 100 % при N=3; ≥ 0.9 при N=5 |
| Разброс стоимости/токенов/времени | CV = σ/μ по прогонам | токены и ₽ ≤ 0.25, время ≤ 0.35 |
| Разброс формы графа | число узлов по каждому NodeKind; max−min по прогонам | experiment, criterion, hypothesis: 0 при неизменном плане; evidence/claim: ≤ 25 % от медианы |
| Отказы записи | число отклонённых записей графа/результата | 0 |
| Сравнение со старым | те же метрики на старом коде | новый код не хуже по каждой метрике; в `stability.json` и RunReport — обе колонки |

Источники случайности, убранные по построению: состоянием владеет код (агент не пишет граф и статусы); типизированные выводы вместо свободного текста и `research_commit`; статусы вычисляются (compute_outcome, node_status); конечные повторы и deadline; детерминированные id и ключи (R5); нет защитных плагинов, меняющих ход прогона.
Остаются: сэмплирование LLM (текст гипотез, состав плана, выбор инструментов), внешние сервисы и сеть, alembic-сборки.
Меры: temperature = 0–0.2 по умолчанию для planner/critic/reviewer/executor (выше — только hypothesizer, задаётся профилем); seed, где провайдер поддерживает; structured outputs (JSON-схема) у всех ролей; кассеты для CI; кеш ответов инструментов read_only в пределах study.

## 13. Порядок работ

Сначала вертикальный срез two_tasks, потом ширина.

| Веха | Содержание | Критерий выхода |
|---|---|---|
| M0 | репо, CI, domain, EventStore(SQLite), writer queue, lease+epoch fencing, machine, отмена по deadline, recover, import-linter | все строки §3.3/§3.7 покрыты тестами; `test_every_study_closes_by_deadline` и `test_llm_hang_past_deadline` зелёные; нагрузочный тест §3.5a (20 попыток в одном study); core без ADK |
| M1 | executor + record + checks (CodeCheck) + артефакты §3.8 + проекции/upcasters §3.5a + ToolRouter(MCP) + adapter + planner/executor-route + CLI | two_tasks 3/3 по §12.2; usage записан; B1/B2 тесты зелёные; §3.4 тесты зелёные |
| M2 | continue_session §3.6 + critic + reviewer + HITL policy + каталог Тома Б/R + миграция критериев | hypothesis_check headless (R2); article_small (B2) живой; borshevik (B1) |
| M3 | coder jobs + alembic fallback + recovery живой | coder_timeout (R4), alembic_nomcp (R3), recovery (R5), file_handoff 3/3 |
| M4 | профили research (гипотезы, литература), medicine, microfluidics | возможности #2, #3, #12, #13 по CAPABILITIES.md; R1 |
| M5 | отчёт (MD/HTML/JSON/НИР), S3, API + stream, A2A/MCP-фасад | #16, #17, #21; API контракт-тесты |
| M6 | новый веб на API | #19 по критерию CAPABILITIES.md |

Параллельные задачи для облачных агентов (после M0, интерфейсы зафиксированы в `core/ports.py`):
- A: checks/ (каталог, kinds, миграция) — зависит только от domain.
- B: tools/mcp + router — от contract.
- C: tools/jobs + coder backend — от contract.
- D: agents/adapter + roles + schemas — от ports.
- E: obs/ usage + pricing — от events.
- F (после M1): alembic pipeline; G: профили; H: reporting; I: api; J (последним): web.
Правило: одна задача = один пакет + его тесты; изменение `ports.py` — только отдельным PR.

## 14. Открытые вопросы

1. Сценарий «Запрос заказчика» (Economics, Optimization, campaign_a2a): откуда агенты, входит ли в объём.
2. Граф исполнения / аудит записей (record_audit, D01–D13): нужен ли продукту.
3. Проверки для problem, model, application, R05, R28, R30 — согласовать с владельцами методики; PDF Тома Б не найдены.
4. Семантика evaluator_kind (python / llm / expert) в старом коде — не проверена.
5. max_plan_revisions, max_attempts, deadline маршрутов, HITL timeout по умолчанию — значения?
6. ~~Хранилище, ТЗ п.8~~ Решено: SQLite WAL, БД на study, порт EventStore; ≥10 параллельных задач внутри одного study (§3.5a).
7. Цены AITunnel по моделям для pricing.yaml; отдаёт ли AITunnel стоимость в ответе.
8. Литература: один MCP-сервис вместо paper_analysis + papers_processing_refactoring — подтвердить.
9. ~~FEDOT.MAS и codesynapse~~ Решено: все интеграции — опциональные внешние сервисы за контрактом Tool, включаются в профиле (§6.5).
10. PDF-отчёт и ГОСТ 7.32 — в M5 или отдельно.
11. grace_s после простоя (60 с) — подтвердить.
12. При пустом required в рантайме — PARTIAL; нужен ли HITL-запрос.
13. Какие ещё выходы помечать `simulate=forbid` (медицина?).
14. study.deadline и deadline экспериментов по умолчанию (plan = 600 с от старта — задано).
15. Продолжение сессии: уточнение без цели — перепланировать всё (как сейчас) или просить LLM указать затронутые задачи?

## 15. Review log

### Ревью крокодила (ce59fde), с учётом решений пользователя (2026-10-23)

| # | Замечание | Статус |
|---|---|---|
| 1 | BLOCKED не терминален | устарело: BLOCKED удалён; зависимые запускаются на partial/stub входах (§3.4) |
| 2 | пробелы таблицы переходов (FAILED со стадий, reject критика, выходы AWAITING_HUMAN, флаг HITL, таймаут ROUTE_RETURNED, review_deadline, T5, отмена задачи, гонка T8/S9) | устарело: стадии — задачи одного DAG с 3 состояниями; любой выход — P4/P5/P6/P7; у каждой не-DONE задачи deadline покрывает route_returned и ревью; DONE финален, гонки нет. Property-тест сохранён: `test_every_study_closes_by_deadline` |
| 3 | InvalidStatus | сделано: попытка FAILED(retryable) (§3.1) |
| 4 | пустой required | сделано (§4.2) |
| 5a/5b | понижение с причиной; UNASSESSED не понижает | сделано (§4.2), + потолок PARTIAL для stub |
| 6 | артефакты | сделано (§3.8, §3.7); + флаг stub |
| 7 | писатель и журнал | сделано; пересчитано под ≥10 задач в одном study, тест на 20 (§3.5a) |
| 8 | HITL | переопределено решениями пользователя (v0.3): шлюзы plan, hypotheses, costly_or_nonreal, report; «реши сам» → critic; on_timeout по умолчанию delegate («timeout→agent»), у costly_or_nonreal — fail (FAILED(human_timeout) + fork), опционально delegate_capped с Budget(delegated_costly) в ₽ или delegate; headless → critic (§7.3) |
| 9 | убрать forced | сделано |
| 10 | jobs | сделано (§6.4) |
| 11 | #1, #21, #22, Budget, cost_rub=None | сделано; #1 — задача kind=spec |

Оговорки:
- 5b: правило действует, только если есть ≥1 required CodeCheck и все MET; только-ReviewCheck и все UNASSESSED → PARTIAL (иначе SUCCESS без доказательств).
- 8: grace после recover вместо сдвига всех дедлайнов на время простоя (иначе решение откладывается бесконечно).

### Ревью крокодила (d6c2e43) — условия старта M0

| # | Замечание | Статус |
|---|---|---|
| a | HitlDeadlineExtended ограничен deadline задачи/study; таймауты гейтов по 10-мин бюджету плана | сделано (§7.3) |
| b | запись в closed-попытку отклоняется; отмена по deadline (asyncio cancel, httpx таймауты, поток/subprocess с kill, освобождение слота) | сделано (§3.3, §4.2) |
| c | lease fencing по epoch в каждой транзакции | сделано (§3.5a) |
| d | property-тест с FakeClock, инварианты по шагам, правила HITL/fork/lease; интеграционный тест зависшего LLM | сделано (§3.3) |
| текст | artifact_id един (§2, §3.8); устаревшая строка UNASSESSED→PARTIAL исправлена; B1-тест одно имя `test_b1_closed_only_when_all_tasks_done`; где ловится CatalogError | сделано |
| HITL | interactive → FAILED(human_timeout), fork; headless → critic; потомки без заглушек | сделано (решение пользователя) |
| M1-1 | понижение ссылается на check id / EvidenceRef | записано (§4.2) |
| M1-2 | provenance на попытке; partial тоже ограничивает; stub_share/verdict stubbed; llm_stub выкл.; дорогие маршруты forbid или отдельный бюджет | записано (§3.4) |
| M2-4 | fork: task_key+spec_digest; nonce; JCS; GC родителя; stub/STALE при смене каталога | записано (§3.6) |

Несогласий нет. Уточнение: 10 мин ТЗ трактуются как бюджет от создания study до DONE задачи plan, включая HITL; если человек не уложился, план проваливается с human_timeout, а не продлевается.

### M0 implementation notes (2026-10-22)

| # | Choice | Rationale |
|---|---|---|
| 1 | Generic `KindRegistry` + `GraphNode`/`GraphEdge`; kinds loaded from packaged `coscientist/schema/graph_schema.yaml` (12 nodes / 16 edges) — not hard-coded in Python | Matches RESEARCH_MODEL single source of truth; core stays domain-agnostic (§1). No research semantics beyond registration in M0. |
| 2 | `RequirementTemplate.text_ru` + `check` on the same template | One source for human-readable text and the machine check (§5); unknown `kind` / `reviewer_role` / unregistered `applies_to` → `CatalogError`. |
| 3 | Digests = sha256 of RFC 8785 JCS via `rfc8785` | Matches DESIGN §3.8 / §3.6 digest requirements. |
| 4 | `fork()` computes affected subgraph only (stub); no child journal yet | Property-test coverage of the rule; full §3.6 inheritance is M2. |
| 5 | Load test asserts ≥100 events in CI (p99 ≤ 50 ms, 20 parallel attempts, fsync); scale to ≥5000 locally by raising fake-tool sleep | Keeps CI fast; same code path as §3.5a. |
| 6 | `StudyRuntime` split across `machine.py` / `attempts.py` / `local_project.py` / `lifecycle.py`; HITL/fork in `experimental/` | Honour ≤500 lines/file budget (§11); core API = M0 only. |
| 7 | `test_llm_hang_past_deadline` uses a hanging asyncio task (no httpx/OpenAI server) | M0 forbids LLM/ADK/httpx in core; cancellation + late-write rejection still covered. |
| 8 | `Attempt.provenance: real\|partial\|stub`; `GateConfig.on_timeout: fail\|delegate_capped\|delegate` (costly_or_nonreal default fail); `Budget.delegated_costly` + `spent_rub` on HITL events | Aligns with RESEARCH_MODEL v0.3 / DESIGN §7.3; recovery + spent_rub report tests pass. |
| 9 | PR #1 review: all writes via StudyWriter; Result+Close+Done atomic; recover never re-runs after ResultRecorded; finish_attempt outcome removed; lease owner=uuid, BEGIN IMMEDIATE, fence uses ttl; P3 asyncio backoff (*replaced by #15*); InputBinding modes; delegate→critic | See commits after review. |
| 10 | Structural cleanup after PR #1: `machine.py` split into `attempts` / `local_project` / `lifecycle` (each ≤500); HITL gates, `DelegatedBudget`/`costly_gate`, report moved to `coscientist.experimental/` (pre-M2/M5); catalog stays in `coscientist.checks` (M1). Core API = M0 state-core only. | PR #2. |
| 11 | `costly_gate(..., critic_accepts=True)` is a **stub**: no real critic agent until M2; the bool simulates accept/reject. Documented on the function and in oracle `tests/refmodel.py`. | Explicit until M2 critic role. |
| 12 | `test_paper_dedupe_two_lits_same_doi` is refmodel-only (`Graph.add_paper`); dual `impl=core` would still call refmodel.Graph. Core paper dedupe lands with GraphBuilder in M1. | Honest skip of false dual-impl. |
| 13 | **Core drops HITL; experimental is unchanged.** Core `tick` does not process HITL timeouts; `view()` always reports `open_hitl=0`; there is no `recover_after_idle`. Opening/recovering a journal that still has open HITL gates with core raises `HitlUnsupported` (checked **before** lease acquire). `invariant_open_attempts` / `invariant_deadlines` live on core `StudyRuntime`. Dual `runtime_kind` covers integration + deadline property; pure HITL tests stay experimental-only (no artificial core skips). | Review fix on PR #2. |
| 14 | **Owner nonce on lease.** Same owner name with a different process nonce cannot renew/steal a live lease (`LeaseLost`); crash reopen and migrated `owner_nonce=''` wait for TTL. `release_lease`/`renew_lease` both raise `LeaseLost` on missing/mismatched nonce; fencing includes `owner_nonce`. | PR #3. |
| 15 | **Core correctness tails.** (1) P3 retries: persisted `AttemptClosed.retry_at`, fired by `tick` vs injected `Clock` (no `asyncio.sleep`/timers); `_retry_at` cleared on fire (exactly once); crash reopen honors pending `retry_at`. (2) Store JSON strict — drop `_json_default`; non-schema types raise at write; AttemptClosed→tasks projection is JSON-only. (3) SQLite writes via `asyncio.to_thread` + RLock; reads on a separate WAL read-only connection (also off-loop); fenced `upsert_projection`; `open_runtime` releases when `owner_nonce` is set after cancel-during-acquire. (4) `DelegatedBudget.remaining_rub` restored from journaled `BudgetDebited` after reopen (experimental; core ↛ experimental) — regression coverage + post-recover gate blocked when cost > remaining. | This PR. |

## 16. Научная модель (граф)

Научные сущности (study, constraint, question, criterion, hypothesis, plan, experiment, artifact, evidence, claim, conclusion, report, hitl — 13 типов, 19 рёбер; источник истины `coscientist/schema/graph_schema.yaml`) — узлы одного графа, который рисует веб и на котором проверяется каталог. Узлы и рёбра строит код (`GraphBuilder`) из типизированных выводов ролей; `research_commit` нет; статусы узлов вычисляются. `NodeKind` в §2 совпадает со схемой. Подробно: [RESEARCH_MODEL.md](RESEARCH_MODEL.md).

### Ревью крокодила v0.2 научной модели (2026-10-23)

| # | Замечание | Статус |
|---|---|---|
| 1 | §7 по реальным rule_id | сделано (RESEARCH_MODEL §7, из reviewed-rules.json dev_1) |
| 2 | единый источник истины | `coscientist/schema/graph_schema.yaml` + `scripts/gen_model_tables.py` + `tests/test_graph_schema.py` |
| 3 | правило вердикта | RESEARCH_MODEL §4.1 (confirmations_needed, provenance, «по литературе») |
| 4 | упрощение | goal/constraint → поля study (constraint-узел только с проверкой); run → attempts[]; claim к критериям убран |
| 5 | отрицательный вывод | based_on → experiment; experiment.limitations[] |
| 6 | рёбра и валидность | produced от experiment; addresses без умолчания; informs удалён; SourceQuote; Attempt.provenance |
| 7 | Том Б | Code-проверки п.1, 8, 11, 12; п.7 config_digest |
| 8 | приёмочные тесты | RT1, H1, recovery внутри стадии (§4.2, §12.2) |
| + | решения пользователя | гипотезы и литература опциональны (HYP-n, LIT-n, plan.hypotheses/literature); HITL-шлюзы с «реши сам» и on_timeout=delegate |

### Ревью крокодила v0.3 (2026-10-23)

| # | Замечание | Статус |
|---|---|---|
| 1 | stance вычисляется; k из разных попыток/артефактов; потолок по q; «по литературе» на пустом списке; claim не создаётся задачей report | сделано (RESEARCH_MODEL §4.1, tests/refmodel.py) |
| 2 | PlanValidator: каждый question покрыт LIT/EXP; fulfilled ⇔ нет untested (PR13: `final_type=artifact` ⇔ рубрика MET) | сделано (§3.2, RESEARCH_MODEL §5, DESIGN_M1 §6) |
| 3 | max_replans, задачи PLAN-n с deadline и долей бюджета | сделано (§2, §7.3) |
| 4 | HITL по умолчанию; строка 8 §15 | решено пользователем; строка 8 обновлена |
| 5 | CI, вендоринг с sha256, пары рёбер, структурированные поля | сделано (.github/workflows/ci.yml, vendor/ai4research, schema) |
| 6 | синхронизация §2, CodeCheck.kind, §7.1 | сделано |
| + | ревизия plan — supersedes; таблица Тома Б генерируется; тесты RT1, H1, дедупликация paper — реальные; recovery + spent_rub — runtime в M0 PR #1 | сделано |

Ревью v0.4: stance — computed (во входе роли запрещён, extra=forbid); модели frozen, Ref[NodeKind], datetime, сверка с §2 тестом; «реши сам» в режиме fail — по бюджету, счётчик Budget(delegated_costly); фикстура `impl` (refmodel | core, API — tests/README.md); check_cardinality; ключи PLAN-0..n; версии в CI закреплены.
