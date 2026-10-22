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

Не-цели
- Защитные плагины, которые следят за LLM (repeat_call_guard, artifact_gate, enforce_pending_record_result, report_guard и т.п.).
- Перенос старых модулей или формата старого состояния (кроме миграции критериев плана, §5.5).
- Свой агентный цикл вместо ADK. ADK используется только в адаптере агентов.
- Камера лаборатории и реактор; сценарий «Запрос заказчика» до решения владельцев (§14).

## 2. Доменная модель

Все типы — pydantic v2, `frozen=True`. Id — строки с префиксом, генерирует ядро (не LLM).

```python
class NodeKind(StrEnum):
    REQUEST = "request"; STUDY = "study"; HYPOTHESIS = "hypothesis"; LITERATURE = "literature"
    PLAN = "plan"; PLAN_STEP = "plan_step"; EXPERIMENT = "experiment"; TASK = "experiment_task"
    REPORT = "report"

class Study(BaseModel):
    study_id: str              # "st_<ulid>"
    profile: str               # "research" | "medicine" | "microfluidics"
    request: str
    catalog_version: str       # фиксируется при создании
    hitl_policy: HitlPolicy    # §7.3
    status: StudyStatus        # §3.1

class Hypothesis(BaseModel):
    hypothesis_id: str; statement: str; rank: int; prediction: str; selected: bool

class Plan(BaseModel):
    plan_id: str; study_id: str; revision: int        # новая ревизия = новая запись
    hypothesis_id: str | None
    tasks: list[TaskSpec]                              # порядок = номер задачи

class TaskSpec(BaseModel):
    task_key: str              # "EXP-<n>", n назначает ядро при принятии плана, не меняется (R5)
    title: str; goal: str
    route: str                 # имя маршрута-агента ("experiment", "coder", "medical", ...)
    required: bool = True
    tools_hint: list[str] = []
    expected_outputs: list[OutputSpec]
    criteria: list[RequirementRef]                     # §5

class OutputSpec(BaseModel):
    name: str; media_type: str; schema_ref: str | None; required: bool = True

class Task(BaseModel):
    task_id: str               # = f"{study_id}/{plan_id}/{task_key}"; уникален (R5)
    spec: TaskSpec; status: TaskStatus; attempts: list[str]   # attempt_id

class Attempt(BaseModel):
    attempt_id: str            # = f"{task_id}#{n}"
    n: int; status: AttemptStatus
    dispatch_id: str           # ключ идемпотентности вызова маршрута
    route_returned: bool       # §3.4
    agent_output_digest: str | None
    deadline: datetime

class Result(BaseModel):
    result_id: str             # = f"{attempt_id}:result"; одна запись на попытку
    attempt_id: str
    outcome: Outcome           # success | partial | failure — ВЫЧИСЛЕН кодом (§4)
    agent_claim: Outcome | None   # что заявил агент; может только понизить
    artifacts: list[str]       # artifact_id
    check_results: list[CheckResult]
    payload_digest: str

class Artifact(BaseModel):
    artifact_id: str           # = "art_" + sha256(content)[:16] + "_" + name
    name: str; media_type: str
    sha256: str; size: int
    uri: str                   # file:// или s3://
    produced_by: ToolCallRef   # §6; источник обязателен (R30)
    schema_ref: str | None

class EvidenceRef(BaseModel):          # модель доказательства (п.2 ревью)
    artifact_id: str
    json_pointer: str | None = None    # RFC 6901, напр. "/rows/0/ld50"
    digest: str                        # sha256 значения по указателю (или всего артефакта)

class Node(BaseModel):                 # проекция для графа/метамодели/веба
    node_id: str; kind: NodeKind; parent_id: str | None; ref_id: str
```

Все сущности существуют только как проекция журнала событий (§3.5). Отношение «требование → доказательство»:
`NodeRequirement.evidence: list[EvidenceRef]`. Проверка ссылается на артефакт по id и, при необходимости,
по json_pointer; digest фиксирует значение, на котором проверка прошла (повторная проверка на другом значении — новый CheckResult).

## 3. Машина состояний

### 3.1 Статусы

```python
class StudyStatus(StrEnum):
    DRAFT; HYPOTHESES; LITERATURE; PLANNING; PLAN_REVIEW; AWAITING_HUMAN; EXECUTING
    REPORTING; DONE; UNFULFILLED; FAILED; CANCELLED
class TaskStatus(StrEnum):
    PENDING; RUNNING; SUCCEEDED; PARTIAL; FAILED; BLOCKED; SKIPPED; CANCELLED
class AttemptStatus(StrEnum):
    DISPATCHED; ROUTE_RETURNED; RECORDED; TIMED_OUT; CRASHED; CANCELLED
```
Нет статуса `unknown`. Любое значение, которое нельзя отобразить в enum, — ошибка `InvalidStatus`, задача остаётся RUNNING.

Терминальные статусы задачи: SUCCEEDED, PARTIAL, FAILED, SKIPPED, CANCELLED.

### 3.2 Инварианты (проверяются в `Core.apply()` на каждом событии)

- I1. Study → DONE только если все задачи терминальны, все required-задачи SUCCEEDED, нет нетерминальных задач. Иначе → UNFULFILLED (все терминальны, но есть required не SUCCEEDED) или остаётся EXECUTING. (B1)
- I2. У задачи не больше одной нетерминальной попытки.
- I3. Result записывается только для попытки в ROUTE_RETURNED; один result_id на попытку.
- I4. Result.outcome = f(check_results) (§4.2); agent_claim только понижает.
- I5. task_key уникален в плане и не меняется после принятия плана; восстановление не переназначает ключи. (R5)
- I6. Попытка с истёкшим deadline не остаётся DISPATCHED/ROUTE_RETURNED дольше одного тика планировщика. (R4)
- I7. Study в AWAITING_HUMAN имеет `hitl_deadline`; по истечении применяется политика (§7.3). (R2)
- I8. Каждый Artifact имеет sha256 и produced_by. (R30)

### 3.3 Таблица переходов

| # | Сущность: из → в | Событие | Кто | Условие |
|---|---|---|---|---|
| S1 | Study DRAFT → HYPOTHESES | `StudyCreated` | API (код) | профиль загружен, каталог валиден |
| S2 | HYPOTHESES → LITERATURE | `HypothesesAccepted` | код | агент вернул `HypothesesOut`, схема ок, ровно одна selected |
| S3 | LITERATURE → PLANNING | `LiteratureRecorded` | код | `LiteratureOut` записан, проверки литературы посчитаны (не обязательно met) |
| S4 | PLANNING → PLAN_REVIEW | `PlanProposed` | код | `PlanOut` валиден по схеме; ключи EXP-n назначены |
| S5 | PLAN_REVIEW → PLANNING | `PlanRejected` | код по вердикту критика | verdict=revise и revision < max_plan_revisions |
| S6 | PLAN_REVIEW → AWAITING_HUMAN | `HumanApprovalRequested` | код | политика требует человека |
| S7 | PLAN_REVIEW/AWAITING_HUMAN → EXECUTING | `PlanAccepted` | код (вердикт критика или HITL-решение или политика по таймауту) | §7.3 |
| S8 | PLAN_REVIEW → FAILED | `PlanRejectedFinal` | код | revision = max_plan_revisions и verdict≠accept |
| T1 | Task PENDING → RUNNING | `AttemptDispatched` | планировщик (код) | зависимости терминальны, лимит параллельности |
| T2 | Attempt DISPATCHED → ROUTE_RETURNED | `RouteReturned` | адаптер ADK (код) | агент маршрута вернул ответ; сохранён digest вывода |
| T3 | Attempt ROUTE_RETURNED → RECORDED; Task → SUCCEEDED/PARTIAL/FAILED | `ResultRecorded` | исполнитель (код) | §4 |
| T4 | Attempt DISPATCHED → TIMED_OUT; Task → PENDING (retry) или FAILED | `AttemptTimedOut` | планировщик (код) | now > deadline; retry если retryable и n < max_attempts |
| T5 | Attempt → CRASHED | `AttemptCrashed` | восстановление (код) | §3.4 |
| T6 | Task → BLOCKED | `TaskBlocked` | код | обязательная зависимость FAILED |
| T7 | Task → SKIPPED | `TaskSkipped` | HITL-решение | только required=false или решение человека с причиной |
| T8 | Task FAILED/PARTIAL → PENDING | `TaskRetried` | HITL или политика | n < max_attempts; старые попытки сохраняются (R05) |
| S9 | EXECUTING → REPORTING | `ExecutionClosed` | код | все задачи терминальны |
| S10 | REPORTING → DONE / UNFULFILLED | `StudyClosed` | код | I1 |
| S11 | любой нетерминальный → CANCELLED | `StudyCancelled` | API | — |

LLM не вызывает ни одного перехода напрямую: у агентов нет инструментов записи состояния.

### 3.4 Восстановление и route_returned

`route_returned` = «ответ агента маршрута сохранён в журнале (событие RouteReturned с digest и телом)».
Алгоритм `Core.recover(study_id)` после рестарта (идемпотентен, повторный вызов ничего не меняет):
1. Перечитать журнал → проекция.
2. Для каждой попытки:
   - ROUTE_RETURNED без ResultRecorded → выполнить запись §4 из сохранённого вывода (без повторного вызова агента).
   - DISPATCHED, deadline не истёк, инструмент с `idempotent=True` → повторить вызов с тем же `dispatch_id`.
   - DISPATCHED, иначе → `AttemptCrashed(retryable=True)`; новая попытка — только через T4/T8. Повторного выполнения той же попытки нет.
   - coder job: опросить `JobStore` по job_id (§6.4); не создавать новую задачу кодера.
3. task_key и номера попыток берутся из журнала, не генерируются заново (R5).

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

### 3.6 Что происходит при падении на каждом переходе

| Падение между | Состояние в журнале | Действие recover |
|---|---|---|
| до `AttemptDispatched` | задача PENDING | ничего; планировщик отправит заново |
| `AttemptDispatched` и ответом агента | DISPATCHED | idempotent-инструмент → повтор с тем же dispatch_id; иначе CRASHED(retryable) |
| ответом агента и `RouteReturned` | DISPATCHED (ответ потерян) | как строка выше |
| `RouteReturned` и `ResultRecorded` | ROUTE_RETURNED | запись из сохранённого вывода, без вызова агента |
| внутри `ResultRecorded` | транзакция откатилась → ROUTE_RETURNED | как строка выше |
| артефакт записан на диск, событие нет | файл без события | файл-сирота; сборщик удаляет по отсутствию artifact_id в журнале |
| `ExecutionClosed` и `StudyClosed` | REPORTING | пересчитать I1, сгенерировать отчёт заново (детерминированно) |
| AWAITING_HUMAN | ждём решения | дедлайн считается от события, не от рестарта (§7.3) |

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
1. Агент маршрута возвращает `AgentTaskOutput` (ADK `output_schema`). Невалидный JSON → один повтор с ошибкой схемы, затем Result(failure, reason=schema_invalid).
2. `ArtifactHandle` должен ссылаться на артефакт, уже зарегистрированный слоем инструментов (§6). Агент не создаёт артефакты текстом.
3. `Executor.record` → `Checks.evaluate(task, out)` → `outcome = compute_outcome(...)` → одно событие `ResultRecorded` с check_results.
4. Внутри той же транзакции ставятся в очередь ReviewCheck (§5). Это единственный путь записи, поэтому проверки качества идут всегда (замена хука plugin.py:351).
5. Инструмента `record_result` у LLM нет.

### 4.2 Вычисление статуса

```python
def compute_outcome(reqs: list[NodeRequirement], claim: Outcome | None) -> Outcome | None:
    """None = ещё ждём ReviewCheck; задача остаётся RUNNING. Чистая функция."""
    req = [r for r in reqs if r.required]
    if any(r.status == PENDING for r in req):
        return None
    ok = {MET, WAIVED, NOT_APPLICABLE}
    if all(r.status in ok for r in req):
        base = SUCCESS
    elif any(r.status in ok for r in req):
        base = PARTIAL          # FAILED / UNASSESSED / MIGRATION_ERROR среди required
    else:
        base = FAILURE
    return min(base, claim, key=RANK) if claim else base   # RANK: failure < partial < success
```
- Задача терминальна только когда все required-проверки получили вердикт или истёк review_deadline (тогда UNASSESSED → required → PARTIAL, R23 показывается отдельно).
- B2: `metric_threshold`/`value_matches` на сыром JSON-артефакте с EvidenceRef — достаточное доказательство (MET). ReviewCheck не может перевести MET CodeCheck в FAILED; он добавляет свой отдельный результат.

Тесты: `test_b1_no_done_with_open_or_unknown_tasks`, `test_b2_matching_value_in_raw_artifact_is_success`,
`test_agent_claim_only_downgrades`, `test_review_scheduled_on_every_record_path`.

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
                  "metric_threshold","value_matches"]
    output: str | None; schema_ref: str | None; pointer: str | None
    metric: str | None; op: Literal["<","<=","==",">=",">","in"] | None; target: Any = None
    tolerance: float | None = None

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
    read_only: bool = False; idempotent: bool = False
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

### 6.2 Правила повтора

| kind | значение | повтор |
|---|---|---|
| local | ошибка аргументов/схемы | нет; ошибка возвращается агенту один раз |
| transport_unsent | запрос точно не ушёл (connect refused, DNS) | да, backoff, до max_retries |
| transport_sent | запрос ушёл, ответа нет (timeout, reset) | только если idempotent=True или read_only=True |
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
- Политика маршрутизации — в профиле: `alembic: enabled|disabled|forced`. `forced` даёт проверяемый сценарий для R3.
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

## 7. Агентный слой

### 7.1 Роли (тонкие ADK LlmAgent, typed I/O)

| Роль | Вход | Выход (output_schema) | Инструменты |
|---|---|---|---|
| hypothesizer | StudyContext | HypothesesOut(hypotheses: list[Hypothesis]) | литература read_only |
| researcher | StudyContext, Hypothesis | LiteratureOut(sources, claims) | papers_search, download, parse |
| planner | StudyContext, LiteratureOut, Hypothesis | PlanOut(tasks: list[TaskSpecDraft]) | каталог инструментов read_only |
| critic | PlanOut | CriticVerdict(decision: accept\|revise\|reject, issues) | нет |
| executor-route | TaskSpec, контекст зависимостей | AgentTaskOutput | инструменты задачи (через ToolRouter) |
| reviewer | ReviewCheck, EvidenceRef[] | ReviewVerdict | read_artifact |
| reporter | проекция (только чтение) | ReportProse(sections с evidence ids) | нет |

- Порядок вызовов ролей задаёт ядро (`Orchestrator` — код, не LLM).
- Модели: OpenAI-совместимый API AITunnel через LiteLLM в ADK (`LiteLlm(model=..., api_base=AITUNNEL_BASE_URL)`), модель на роль + резерв в профиле. Ключ только из env.

### 7.2 Граница ADK

```
core/   — не импортирует google.adk (проверка: тест импорта + import-linter)
agents/adapter.py — единственное место, где создаются Runner/LlmAgent
```
```python
class AgentPort(Protocol):
    async def run(self, role: str, input: BaseModel, tools: list[Tool], budget: Budget) -> AgentRun
class AgentRun(BaseModel):
    output: BaseModel | None; error: str | None; usage: Usage; tool_calls: list[ToolCallRef]
```
Без ADK-плагинов и callbacks, кроме одного: учёт usage/tool_calls в адаптере (наблюдение, не контроль).

### 7.3 HITL как состояние (R2)

```python
class HitlPolicy(BaseModel):
    mode: Literal["interactive","auto","headless"]
    timeout_s: int = 1800
    on_timeout: Literal["accept_if_critic_accepted","reject","fail"] = "accept_if_critic_accepted"

class HitlRequest(BaseModel):
    request_id: str; kind: Literal["plan_approval","result_acceptance","skip_task"]
    payload_ref: str; deadline: datetime
class HitlDecision(BaseModel):
    request_id: str; decision: Literal["accept","revise","reject"]; by: Literal["human","policy"]; reason: str
```
- headless/auto: запрос к человеку не создаётся; решение = вердикт critic/reviewer; `by="policy"`.
- interactive: AWAITING_HUMAN с deadline; по истечении — `on_timeout`. Зависания нет (I7).
- Отсутствие ревьюера = ошибка конфигурации при старте, а не `fallback_reviewer_unavailable` в рантайме.
Тест: `test_r2_headless_plan_review_completes_without_human`.

## 8. Профили (R1)

```python
class Profile(BaseModel):
    name: str
    roles: dict[str, RoleConfig]            # модель, промпт, инструменты
    mcp_servers: list[McpServerConfig]
    routes: dict[str, RouteConfig]          # route -> роль + бюджет
    catalog_extensions: list[str]           # доп. шаблоны требований
    alembic: Literal["enabled","disabled","forced"] = "enabled"
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
  agents/      adapter.py roles.py prompts/*.md schemas.py                                    ≤ 1 200
  profiles/    research/ medicine/ microfluidics/                                             ≤ 1 500 каждый
  reporting/   report.py nir.py render_md.py render_html.py                                   ≤ 1 200
  api/         app.py views.py stream.py                                                      ≤ 800
  obs/         usage.py tracing.py                                                            ≤ 400
web/           отдельно (M6)
```
- Файл ≤ 500 строк, функция ≤ 60. Превышение — ревью архитектуры, не исключение.
- Оценка ядра без профилей и alembic ~10–12k; это бюджет, не измерение.
- Зависимости: `core` ← `checks` ← `tools` ← `agents` ← `api`. import-linter в CI.

## 12. Паритет с эталоном

### 12.1 Какой эталон
- Сравнение идёт с ОЖИДАЕМЫМ поведением карты (поле expected), а не с числами старой системы.
- Эталонные прогоны: `rerun_cossessions/baseline_20261022` (старый код, 29c5831) — источник входов, артефактов и observed.
- Где observed ≠ expected (B1, B2, R1–R5) — цель = expected. «Исправленный старый» (после шагов 1–3) — дополнительный ориентир для стоимости, если появится.

### 12.2 Сценарии и пороги

| Сценарий | Вход | Проверка (expected) | Порог |
|---|---|---|---|
| two_tasks (×3) | запрос эталона | 2/2 задачи SUCCEEDED; артефакты dataset_overview и chemical_space_clustering по схеме; 2/2 tool_found | 3/3 прогона; 0 ложных статусов |
| file_handoff (×3) | эталон | 4 задачи; артефакт задачи N читается задачей N+1 по artifact_id | 3/3 |
| borshevik | эталон | 6 задач; DONE только если все required SUCCEEDED, иначе UNFULFILLED | 0 ложных DONE (B1) |
| article_small | эталон | 225 соединений = 225 → SUCCEEDED, study DONE | fulfilled (B2) |
| microfluidics | эталон | старт без ошибки, задача выполнена | (R1) |
| hypothesis_check (headless) | эталон | ревью без человека; вывод «гипотеза опровергнута» (медиана −6 vs −4) | (R2) |
| alembic_nomcp | профиль alembic=forced, нужного MCP нет | ToolResolved=tool_built, инструмент зарегистрирован и вызван | (R3) |
| coder_timeout | coder job дольше deadline | попытка failure(retryable) ≤ deadline+тик | (R4) |
| recovery | kill -9 после RouteReturned и после Dispatched | уникальные EXP-n, 0 дублей попыток, результат записан 1 раз | (R5) |
| стоимость | two_tasks | токены и ₽ по прогону и задаче записаны | ≤ эталонной стоимости (после сбора токенов эталона) |

### 12.3 Тесты
- Юнит: машина состояний (каждая строка §3.3 и §3.6 — тест), compute_outcome, каталог, router, jobs.
- Характеризационные: входы/артефакты эталона как фикстуры; LLM заменён записанными ответами (cassette) — детерминированный CI.
- Живые: сценарии §12.2 с реальной моделью, ночью; текст LLM не сравнивается, сравниваются статусы, артефакты и проверки.
- Возможность принята = все её тесты зелёные + её сценарий 3/3.

## 13. Порядок работ

Сначала вертикальный срез two_tasks, потом ширина.

| Веха | Содержание | Критерий выхода |
|---|---|---|
| M0 | репо, CI, domain, events/store, machine, recover, import-linter | все строки §3.3/§3.6 покрыты тестами; core без ADK |
| M1 | executor + record + checks (CodeCheck) + ToolRouter(MCP) + adapter + planner/executor-route + CLI | two_tasks 3/3 по §12.2; usage записан; B1/B2 тесты зелёные |
| M2 | critic + reviewer + HITL policy + каталог Тома Б/R + миграция критериев | hypothesis_check headless (R2); article_small (B2) живой; borshevik (B1) |
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
6. Хранилище: SQLite на study достаточно для ≥10 параллельных задач (ТЗ п.8)? Альтернатива — Postgres.
7. Цены AITunnel по моделям для pricing.yaml; отдаёт ли AITunnel стоимость в ответе.
8. Литература: один MCP-сервис вместо paper_analysis + papers_processing_refactoring — подтвердить.
9. FEDOT.MAS и codesynapse: переносить как инструменты или как внешние сервисы.
10. PDF-отчёт и ГОСТ 7.32 — в M5 или отдельно.
