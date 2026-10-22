# CoScientist-next — научная модель исследования (граф)

Статус: черновик v0.2 (2026-10-23). Дополняет DESIGN.md §2, §5, §7.3. Один граф = научная модель = то, что рисует веб = то, на чём проверяется каталог требований.
Источник истины для типов, рёбер, кардинальностей и правил — `schema/graph_schema.yaml`; таблицы §2, §3, §7 между маркерами `gen:*` генерирует `scripts/gen_model_tables.py`, `tests/test_graph_schema.py` падает при расхождении или при rule_id вне `tests/reviewed_rule_ids.txt`.

## 0. Глоссарий (простыми словами)

Стадии DAG: `spec → plan → [HYP-n | LIT-n | EXP-n]* → report`; после HYP/LIT planner может перепланировать (новая ревизия plan). Гипотезы и литература не обязательны: planner решает (`plan.hypotheses`, `plan.literature` = {needed, reason}), профиль может обязать.

| Понятие | Что это | Когда появляется (стадия DAG) | Кто создаёт | Что от него зависит |
|---|---|---|---|---|
| Исследование (study) | Корень графа: один запрос пользователя и всё, что сделано по нему. Цель (goal) и ограничения (constraints[]) — поля study. | До старта агентов; goal/constraints — spec | Код (ядро); текст цели — tz_spec | Всё остальное; итоговый вердикт |
| Ограничение (constraint) | Рамки: бюджет, данные, нормы, этика. Обычно поле study; отдельный узел — только если по нему есть проверка (measured_by). | spec | tz_spec → код | Критерии, методы |
| Исследовательский вопрос | Что именно мы хотим узнать (≥1). Без гипотез вердикт получает ответ на вопрос. | spec | tz_spec → код | Гипотезы, эксперименты, вывод |
| Критерий успеха | Измеримая планка: метрика, сравнение, порог. Замораживается до первой evidence; статус — из NodeRequirement. | spec (study), plan (experiment) | tz_spec / planner → код; проверяет код | Статус эксперимента, вердикт |
| Гипотеза (опционально) | Проверяемое утверждение с предсказанием. Создаётся задачей HYP-n, если planner (или профиль) решил, что она нужна. | HYP-n | hypothesizer → код; выбор/правка — HITL | Эксперименты (tests), вердикт |
| Литературный поиск | Эксперимент с `method=literature_search` (отдельного типа узла нет): по вопросам, гипотезам и плану запросов (query_plan) найти статьи и выписать цитаты-доказательства. Планируется и повторяется как любой эксперимент (LIT-n), проверяется своими критериями: min_sources, question_coverage, recency, quote_valid. Бывшая стадия literature — просто первая такая задача LIT-0. | plan (LIT-n), в т.ч. до гипотез | planner планирует; выполняет researcher через инструменты поиска; узлы создаёт код | artifact(role=paper), evidence(literature), claim; эксперименты могут depends_on LIT-n |
| План | Маршрут: решения plan.literature/plan.hypotheses с причинами + задачи LIT/HYP/EXP с зависимостями. Правка = новая ревизия (derived_from). | plan, перепланирование | planner + critic → код; одобряет человек или critic | Эксперименты |
| Эксперимент (задача) | Один шаг плана: метод, инструмент, данные, критерии; попытки (attempts[]) и ограничения (limitations[]) — внутри узла. | plan | planner → код (ключ даёт код) | Артефакты, доказательства |
| Попытка (attempt) | Одно выполнение эксперимента: outcome, provenance real/partial/simulated, config_digest. Не узел. | выполнение | Код | Outcome задачи |
| Артефакт | Файл или статья с ролью paper/data/code/figure/table/log/report. У paper идентичность — DOI/URL (дедупликация); у data — version и hash. Заменяет прежний узел source. | любая | Слой инструментов (код) | Доказательства ссылаются на него |
| Доказательство (evidence) | Наблюдение с адресом: computational/experimental — artifact_id + JSON-указатель + digest; literature — SourceQuote (статья, страница/раздел, цитата, digest). | LIT-n, EXP-n | Агент указывает адрес, код проверяет | Утверждения |
| Утверждение (claim) | «Доказательство поддерживает/опровергает гипотезу или ответ на вопрос», проверено по замороженному критерию. | LIT-n, EXP-n | Агент предлагает, код принимает | Вердикт, вывод |
| Вывод (conclusion) | Ответ на вопрос: что установлено, как, в каких границах; может опираться на FAILED/inconclusive эксперимент. | report | reporter → код | Отчёт |
| Отчёт | Документ из графа. | report | Код + reporter | Конечный продукт |
| HITL-шлюз | Точка, где человек одобряет/правит или говорит «реши сам» (тогда решает critic). Шлюзы: plan, hypotheses, costly/non-real эксперимент, report; включает профиль. Решение — событие с автором, на узле `approved_by` (human/critic), `delegated_by`. | plan, после HYP, перед дорогим EXP, report | Человек или critic; код записывает | Продолжение DAG |
| Требование (NodeRequirement) | Пункт каталога (Том Б / R / критерий), привязанный к узлу; статус MET/FAILED/… | при создании узла | Код (по каталогу) | Статус узла и критерия |
| Provenance real/partial/simulated | Метка попытки: посчитано на реальных, неполных данных или заглушке. | выполнение | Код | Потолок outcome PARTIAL; вердикт не выше inconclusive |

## 1. Принципы

1. Агент никогда не пишет узлы и рёбра. Агент возвращает типизированный вывод роли (`ResearchSpecOut`, `HypothesesOut`, `LiteratureOut`, `PlanOut`, `AgentTaskOutput`, `ReportProse`); `GraphBuilder` (код) превращает его в узлы/рёбра в одной транзакции с событием результата. Инструмента `research_commit` нет.
2. Тип, статус, id, рёбра — из enum и pydantic-схем, сгенерированных из `schema/graph_schema.yaml`; ссылка агента — только на id из входного контекста (`Ref[NodeKind]`), обязательная там, где ребро обязательно (умолчаний нет); неизвестная или отсутствующая ссылка = ошибка схемы вывода (один повтор).
3. Статус узла не хранится: выводится из состояния задачи, попыток, проверок и рёбер (§4).
4. Узлы неизменяемы (frozen); правка = новая ревизия + ребро `supersedes` (для plan — `derived_from`).
5. Граф — проекция журнала событий (DESIGN §3.5): `NodeAdded`, `EdgeAdded` пишутся только `GraphBuilder`.
6. Модель не зависит от предметной области: 12 типов узлов покрывают любое эмпирическое или вычислительное исследование. Доменное — только в профилях: значения `method`, `route`, инструменты, CodeCheck kinds, шаблоны каталога, включённые HITL-шлюзы; новых NodeKind профиль не добавляет.

| Исследование | question / hypothesis | experiment (method) | evidence / criterion |
|---|---|---|---|
| Воспроизведение ML-бенчмарка | «воспроизводится ли accuracy статьи?» / «±1 п.п.» | обучение+оценка (computational), baseline = число статьи | метрики из JSON-лога; criterion `|Δacc| ≤ 1` |
| Анализ протокола wet-lab | «корректен ли протокол X?» / «шаг 3 даёт выход ≥ 70 %» | статистика по опубликованным данным (statistical) | таблица выходов; criterion по выходу |
| Обзор литературы | вопрос обзора; гипотез нет (plan.hypotheses.needed=false) | LIT-n (literature_search) | evidence(literature) + artifact(paper); criterion: min_sources, question_coverage |
| Симуляционное исследование | «как p влияет на y?» / «монотонно» | серия запусков (computational) | выход симулятора по указателю; criterion: знак тренда |

## 2. Узлы (NodeKind)

Общие поля: `node_id` (префикс+ulid, код), `kind`, `study_id`, `task_id` (кто создал), `attempt_n | None`, `revision`, `created_seq`, `provenance: real|partial|simulated` (вычисляется, §6). Узлов `goal`, `source`, `run` больше нет: goal/constraints[] — поля study, source = artifact(role=paper), run = experiment.attempts[].

<!-- gen:nodes:start -->
| NodeKind | Префикс | Поля (обязательные — жирным) | Создаёт задача | Из вывода | Смысл |
|---|---|---|---|---|---|
| `study` | ST | **request**, **goal** (statement, research_form, trl), constraints[] (subtype, content, limit), profile, catalog_version, parent_study_id, confirmations_needed (профиль, по умолч. 1) | ядро (до DAG); goal/constraints[] заполняет spec | ResearchSpecOut.goal, .constraints[] | Корень графа; цель и ограничения — его поля |
| `constraint` | C | **subtype** (resource/data/norm/ethics/domain/profile), **content**, limit | spec | ResearchSpecOut.constraints[] с проверкой | Узел только если на него направлен measured_by; иначе — поле study.constraints[] |
| `question` | Q | **formulation**, gap, scope | spec | ResearchSpecOut.questions[] (≥1, иначе schema_invalid) | Исследовательский вопрос; без гипотез ответ на него получает вердикт |
| `criterion` | CR | **metric**, **op**, **target**, tolerance, kind=code\|review, frozen_at_seq | spec (study), plan (experiment) | ResearchSpecOut.success_metrics[], TaskSpec.criteria | Измеримая планка; статус = NodeRequirement |
| `hypothesis` | H | **statement**, **prediction**, rationale, rank, selected, approved_by (human\|critic), delegated_by | задача HYP-n (опционально) | HypothesesOut (+ правка в HITL-шлюзе hypotheses) | Проверяемое утверждение; вердикт вычисляется |
| `plan` | PL | **revision**, **hypotheses** {needed, reason}, **literature** {needed, reason}, critic_decision, approved_by (human\|critic), delegated_by, approved_seq | plan; перепланирование после HYP/LIT и правка человеком = новая ревизия | PlanOut + CriticVerdict + HitlDecision | Ревизия плана (маршрута) |
| `experiment` | X | **task_key** (HYP-n не узел; LIT-n/EXP-n), **title**, **goal**, **method** (computational/statistical/laboratory/literature_search/expert), query_plan (literature_search), **route**, tools_hint, dataset, baselines, required, costly, tool_resolution, **attempts[]** (n, outcome, close_reason, provenance, config_digest), limitations[] | plan (при accept) | TaskSpec | Задача DAG с попытками внутри |
| `artifact` | A | **artifact_id**, **role** (paper/data/code/figure/table/log/report), media_type, sha256; paper: **doi\|url** (идентичность дедупликации), title, year, status (parsed/download_failed); data: **version**, **hash** | любая (слой инструментов) | ToolOutput; LiteratureOut.papers (только найденные инструментом) | Файл или статья (бывший source) |
| `evidence` | E | **kind** (literature/computational/experimental/expert), **statement**, **ref**: EvidenceRef (computational/experimental) \| SourceQuote (literature), measured_on | experiment (LIT-n, EXP-n) | LiteratureOut.quotes, AgentTaskOutput.claims[].evidence | Адресуемое наблюдение |
| `claim` | CL | **stance** (supports/refutes/inconclusive), **target** (hypothesis\|question), **checked_against** (criterion, frozen), reason | experiment, report | AgentTaskOutput.claims[], LiteratureOut.claims[] | Утверждение со ссылкой на доказательство |
| `conclusion` | CN | **synthesis**, how_established, validity_bounds, open_questions | report | ReportProse | Ответ на вопрос |
| `report` | RP | **artifact_id**, format, approved_by (human\|critic), delegated_by | report | рендер кода + HITL-шлюз report | Документ из графа |
<!-- gen:nodes:end -->

`result` не узел: запись Result (DESIGN §2), видна как outcome попытки. `tool`/`resource` — атрибуты `experiment` и бюджеты. `requirement` — значок на узле (§7). HYP-n — задача DAG (TaskKind hypotheses), не experiment: её выход — узлы hypothesis.

Ссылки доказательства:
```python
class EvidenceRef(BaseModel):    # computational / experimental
    artifact_id: str; json_pointer: str; digest: str          # sha256(JCS(значение по указателю))
class SourceQuote(BaseModel):    # literature
    artifact_id: str             # artifact(role=paper)
    locator: str                 # страница или раздел, напр. "p.6" / "§3.2"
    quote: str; digest: str      # digest = sha256(JCS(quote))
```

## 3. Рёбра

Все рёбра создаёт код; пары (from,to) фиксированы; концы обязаны существовать (FK в проекции). Удалены: `has_goal` (goal — поле), `informs` (дублировал derived_from), `ran_as` (попытки внутри experiment).

<!-- gen:edges:start -->
| Ребро | From → To | Кардинальность | Кем выводится |
|---|---|---|---|
| `asks` | study → question | 1..n | spec |
| `constrained_by` | study → constraint | 0..n (только проверяемые) | spec |
| `measured_by` | study\|constraint\|experiment → criterion | study ≥1; constraint ≥1; required experiment ≥1 | spec/plan |
| `addresses` | hypothesis\|experiment → question | hypothesis 1..n; ref обязателен, умолчания нет | HYP-n/plan |
| `plans` | plan → experiment | 1..n; experiment ровно 1 plan | plan |
| `tests` | experiment → hypothesis | 0..n; у experiment ≥1 из tests/addresses | plan |
| `depends_on` | experiment → experiment | 0..n, ацикл. | plan |
| `uses_input` | experiment → artifact | 0..n, provenance real/partial/simulated | ядро (P2) |
| `produced` | experiment → artifact | artifact ровно 1 producer; attempt_n на ребре | слой инструментов |
| `derived_from` | evidence\|plan → artifact\|plan | evidence → artifact ровно 1; plan → plan 0..1 (ревизия/правка HITL) | код |
| `about` | claim → hypothesis\|question | ровно 1 | код |
| `cites` | claim → evidence | ≥1 | код |
| `answers` | conclusion → question | 1..n; каждый question ≥1 | report |
| `based_on` | conclusion → claim\|experiment | ≥1; experiment может быть FAILED/inconclusive | report |
| `renders` | report → conclusion | 1..n | report |
| `supersedes` | hypothesis\|criterion → hypothesis\|criterion | 0..1, тот же kind | ревизия |
<!-- gen:edges:end -->

## 4. Жизненный цикл узла (вычисляемый)

| Узел | Статусы | Правило |
|---|---|---|
| question, constraint, criterion(study), hypothesis, plan, experiment-до-запуска | `draft` → `final` | `final`, когда создавшая задача DONE(SUCCESS/PARTIAL) и пройден HITL-шлюз (если включён) |
| experiment | = TaskState/TaskOutcome (pending/running/success/partial/failed/skipped) | DESIGN §3; outcome последней попытки |
| criterion | `pending` / `met` / `not_met` / `unassessed` | = статус NodeRequirement (claim к критерию не создаётся) |
| evidence | `valid` / `invalid` | §6.1 |
| claim | `accepted` / `rejected` | accepted ⇔ все cites → valid evidence и checked_against — замороженный criterion |
| hypothesis / question (вердикт) | `untested` / `supported` / `refuted` / `inconclusive` | §4.1 |
| conclusion | `draft` / `final` | final ⇔ задача report SUCCESS и шлюз report пройден |

### 4.1 Вердикт

Цель вердикта T — гипотеза; если гипотез нет (plan.hypotheses.needed=false), — ответ на вопрос (claim `about` question). k = `study.confirmations_needed` (профиль, по умолч. 1).

Claim **засчитывается**, если одновременно: accepted; cites evidence kind computational или experimental с provenance=real; checked_against — замороженный criterion; попытка-производитель SUCCESS.
Исключение «по литературе»: evidence(literature) из эксперимента `method=literature_search`, у которого есть свой criterion и попытка SUCCESS, засчитывается; вердикт помечается «по литературе». Литература вне такого эксперимента вердикта не даёт.

```python
def verdict(T):
    acc = accepted_claims(T); q = [c for c in acc if counts(c)]
    sup = [c for c in q if c.stance == "supports"]; ref = [c for c in q if c.stance == "refutes"]
    if not acc and not any_done(experiments_for(T)): return "untested"
    if sup and ref: v = "inconclusive"
    elif len(sup) >= k: v = "supported"
    elif len(ref) >= k: v = "refuted"
    else: v = "inconclusive"
    if any(c.provenance != "real" for c in acc) and v in ("supported", "refuted"): v = "inconclusive"   # потолок
    return v, {"по литературе": all(is_lit(c) for c in sup + ref)}
```

Агент не может «подтвердить гипотезу» — только дать claim со ссылкой на доказательство.

## 5. Задачи DAG → узлы

| Задача | Вход (читает) | Вывод роли | Узлы/рёбра (создаёт GraphBuilder) |
|---|---|---|---|
| (ядро) | запрос | — | study |
| spec | study | ResearchSpecOut | поля study.goal/constraints[], constraint*, question+, criterion(study)+; asks, constrained_by, measured_by |
| plan | study, question, (hypothesis, выходы LIT при перепланировании) | PlanOut + CriticVerdict (+ HitlDecision) | plan(hypotheses, literature), experiment+, criterion(experiment)+; plans, tests, addresses, depends_on, measured_by, derived_from (plan→plan) |
| HYP-n (опц.) | question, constraint, выходы LIT | HypothesesOut | hypothesis+; addresses (ref обязателен) |
| LIT-n (experiment, literature_search) | question, hypothesis?, query_plan, выходы depends_on | LiteratureOut | artifact(paper)*, evidence(literature)*, claim*; produced, derived_from, cites, about |
| EXP-n | experiment, его входы | AgentTaskOutput | artifact*, evidence*, claim*; uses_input, produced, derived_from, cites, about |
| report | вся проекция (read-only) | ReportProse | conclusion, report; answers, based_on, renders |

Валидация вывода (до записи): ссылки ⊂ id входного контекста; enum; обязательные поля и ссылки. Ошибка → повтор с ошибкой схемы, затем Result FAILED(schema_invalid). Частично валидный вывод не пишется.

### 5.1 Литературный поиск

Отдельного NodeKind нет: `experiment` с `method=literature_search`, route `literature`, ключи `LIT-n` (нумерация отдельно от `EXP-n`). Planner решает `plan.literature = {needed, reason}`; первая задача — `LIT-0`, профиль может её обязать. LIT можно ставить до HYP (обоснование гипотез), между EXP (baselines, параметры) и после (сверка).

- Вход: question, hypothesis (если есть), `query_plan` (из TaskSpec или строит researcher в той же попытке), выходы depends_on.
- Выход: `artifact(role=paper)` с DOI/URL — идентичность для дедупликации (повторно найденная статья = тот же узел, новое ребро `produced` не создаётся); `evidence(kind=literature)` с `ref = SourceQuote`; цитата обязана находиться в тексте статьи (CodeCheck `quote_in_source`), иначе evidence invalid. Нескачанная статья — artifact со status=download_failed, без evidence.
- Повторы — обычные попытки; fork перезапускает задачу как любую другую.
- Критерии (criterion на experiment, пороги — профиль): `min_sources` (≥ N paper с DOI/URL), `question_coverage` (каждый question имеет ≥1 valid evidence через claim), `recency` (доля статей не старше K лет ≥ p), `quote_valid`.

### 5.2 HITL-шлюзы

Совместимо с DESIGN §7.3. Профиль задаёт, какие шлюзы включены, и для каждого `on_timeout: delegate | fail` (по умолч. `delegate`).

| Шлюз | Когда | Что видит человек | Что может |
|---|---|---|---|
| `plan` | после critic, до запуска задач плана | plan.literature и plan.hypotheses с причинами, список LIT/HYP/EXP с depends_on, критерии | одобрить; править маршрут (добавить/удалить LIT/HYP/EXP, менять зависимости) → новая ревизия plan с `derived_from` к предыдущей; «реши сам» |
| `hypotheses` | после HYP-n | гипотезы с рангом и предсказанием | выбрать (selected), править текст (ревизия + supersedes), отклонить; «реши сам» |
| `costly_or_nonreal` | перед попыткой EXP с `costly=true` или с входом provenance≠real | метод, бюджет, входы и их provenance | разрешить, пропустить (SKIPPED, reason=human_rejected); «реши сам» |
| `report` | перед финализацией conclusion/report | черновик отчёта, вердикты, limitations | принять, вернуть на доработку (новая попытка report); «реши сам» |

- Решение — событие `HitlDecided(gate, node_id, author, decision, delegated)`; на узле: `approved_by: human | critic`, `delegated_by: human | None`.
- «Реши сам»: решает critic (агент) → `approved_by=critic, delegated_by=human`.
- Interactive, истёк deadline шлюза: `on_timeout=delegate` → решает critic, отметка «timeout→agent» (`approved_by=critic`, `delegated_by=timeout`); `on_timeout=fail` → задача FAILED(human_timeout), зависимые SKIPPED(input_unavailable), заглушки запрещены.
- Headless: решает critic (`approved_by=critic`).

## 6. Провенанс и валидность доказательств

- Источник истины — `Attempt.provenance: real | partial | simulated` (DESIGN §2, §3.4); порядок real < partial < simulated. Не-real ограничивает outcome попытки до PARTIAL.
- `provenance(n)`: artifact, evidence — от попытки-производителя (`produced`.attempt_n / `derived_from`); claim — max по `cites`; вердикт — max по accepted claims; conclusion — max по `based_on`; study verdict `simulated` (DESIGN §3.2).
- Ребро `uses_input` хранит provenance входа; по нему вычисляется provenance попытки.
- Evidence с provenance≠real: criterion не сильнее PARTIAL у задачи, вердикт не выше inconclusive (§4.1), в отчёте — раздел «Выполнено на заглушках».

### 6.1 Валидность evidence
- literature: `ref` — SourceQuote; artifact(role=paper) существует; `quote_in_source` проходит по тексту; digest = sha256(JCS(quote)).
- computational / experimental: `ref` — EvidenceRef с artifact_id, json_pointer (резолвится) и digest значения (совпадает при re-read).
- expert: ссылка на HitlDecided или ReviewCheck. Иначе — invalid.

### 6.2 Ограничения эксперимента
`experiment.limitations[]` (текст + ref на evidence/attempt) пишет код из Result (downgrade, partial входы, невыполненные критерии) и агент; conclusion.validity_bounds обязаны их учитывать. `conclusion.based_on` может указывать на FAILED/inconclusive эксперимент — отрицательный результат тоже вывод.

## 7. Привязка каталога требований

`RequirementTemplate.applies_to: list[NodeKind]` — ровно из §2. `CodeCheck` kind `graph_shape` — проверка структуры графа.

| Требование (Том Б) | Узлы | Проверка |
|---|---|---|
| 1 problem | study, question | Code: ≥1 question; у каждого непустой gap и ≥1 входящее addresses (от hypothesis или experiment); Review: проблема сформулирована |
| 2 goals | study, criterion | Code graph_shape: study.goal непуст; study −measured_by→ ≥1 criterion с metric/op/target |
| 3 planning | plan, experiment | Code: DAG ацикличен; у required experiment ≥1 criterion и ≥1 из tests/addresses; plan.hypotheses/literature заполнены с reason |
| 4 resources | constraint(resource), experiment.tool_resolution | Code: tool_resolution ≠ tool_missing у required; бюджет не превышен |
| 5 knowledge | experiment(literature_search), artifact(paper), evidence(literature) | Code: min_sources, question_coverage, recency, quote_valid (§5.1) — если plan.literature.needed |
| 6 data | artifact(data), uses_input | Code: schema_valid на входах; у data есть version и hash; provenance=real у required |
| 7 config | experiment.attempts[] | Code: attempt.config_digest = sha256(JCS(resolved config)) записан (R14) |
| 8 model | experiment(method), artifact(code) | Code: method ∈ profile.methods; method=computational ⇒ ≥1 produced artifact(code) или tool_found; Review: метод соответствует вопросу |
| 9 execution | experiment.attempts[] | Code: ≥1 попытка с outcome; нет открытых попыток у DONE задачи |
| 10 validation | criterion, claim | Code: metric_threshold/value_matches по EvidenceRef → criterion met |
| 11 comparison | experiment.baselines, evidence | Code: для каждого baseline есть valid evidence с тем же metric (json_pointer) и claim с checked_against на criterion сравнения; Review: корректность сравнения |
| 12 application | conclusion, experiment.limitations[] | Code: validity_bounds непуст; каждая limitation из based_on-экспериментов упомянута (ref); Review: границы применимости |
| 13 trace | evidence, artifact | Code: каждая evidence имеет ровно 1 derived_from, digest совпадает; каждый artifact — ровно 1 produced |
| 14 report | report, conclusion | Code: conclusion −answers→ все question; три раздела (real/stub/human); решения HITL перечислены |

Правила R (по реальным rule_id из `reviewed-rules.json`, CoScientist_dev@dev_1):

<!-- gen:rules:start -->
| rule_id | Узлы | Проверка |
|---|---|---|
| `R02:profile_applicability` | study, NodeRequirement | Code: каждый NOT_APPLICABLE имеет решение профиля (версия + rationale); отсутствие ресурса ≠ N/A; plan.hypotheses/literature.needed=false — с reason |
| `R04:process_instance_binding` | experiment.attempts[] | Code: попытка привязана к task_key, n, experiment и роли агента; вызов агента без попытки — не экземпляр |
| `R05:iteration_reactivation` | experiment, plan | Code: повтор = новая attempt или ревизия (derived_from/supersedes); прежние не перезаписываются; task_key уникален |
| `R06:checkpoint_contract` | experiment, plan | Code: у задачи есть вход (depends_on DONE), завершение (criterion), доказательства (evidence), возврат (P3/HITL-шлюз) |
| `R11:evidence_locator` | evidence | Code: computational/experimental → artifact_id + json_pointer + digest; literature → SourceQuote + quote_in_source |
| `R11:json_pointer_evidence_locator` | evidence(computational) | Code: json_pointer резолвится в зарегистрированном JSON-артефакте, digest совпадает |
| `R14:configuration_snapshot` | experiment.attempts[] | Code: attempt.config_digest = sha256(JCS(resolved config)) записан до запуска |
| `R16:verification_validation_split` | criterion, claim | Code: verification (CodeCheck → criterion) и validation (вердикт по claim) — разные статусы; SUCCESS ≠ supported |
| `R17:predeclared_check` | criterion, claim | Code: criterion.frozen_at_seq < seq первой evidence; claim.checked_against — замороженный criterion; правка = supersedes |
| `R17:registered_schema_binding` | artifact(data) | Code: schema_valid по schema_ref, закреплённой в OutputSpec до запуска |
| `R18:scientific_claim_trace` | claim, hypothesis, question, conclusion | Code graph_shape: claim → (hypothesis → question \| question); claim → evidence → artifact → experiment(method) → attempt; checked_against |
| `R23:unassessed_distinct` | criterion(review) | UNASSESSED, FAILED, NOT_APPLICABLE хранятся раздельно; не становятся 0 или N/A |
| `R24:critical_blocker` | все | открытое критическое несоответствие блокирует только затронутые использования; без усреднения (DESIGN §5.4) |
| `R28:corrective_action_closure` | NodeRequirement, experiment | Code: FAILED закрывается только новой попыткой/ревизией с evidence эффективности и решением; прежняя оценка хранится |
| `R29:report_completeness` | report, conclusion | Code: разделы из графа: покрытие, неопределённость, находки, limitations[], real/stub/human, решения HITL (кто, делегировано, timeout→agent) |
| `R30:information_access_integrity` | artifact | Code: блоб cas/<sha256> читается; paper без файла → download_failed, не пропадает |
| `ENG:registration_integrity` | artifact, evidence | Code: re-read digest совпадает; иначе зависимые использования блокируются |
<!-- gen:rules:end -->

Валидация каталога: каждый из 14 пунктов Тома Б и каждый rule_id покрыт ≥1 шаблоном с `applies_to` ⊂ NodeKind; каждый NodeKind имеет defaults; rule_id ⊂ reviewed_rule_ids (тест).

## 8. Веб

| Вид | Что рисуется |
|---|---|
| Колонки (ELK layered, слева направо) | 0 study/constraint · 1 question/criterion · 2 plan/hypothesis · 3 experiment (LIT-n — отдельная дорожка) · 4 artifact/evidence · 5 claim · 6 conclusion/report |
| Цвет рамки | node_status (§4) |
| Штриховка/значок | provenance partial/simulated; «по литературе» на вердикте |
| Значки | NodeRequirement: MET/FAILED/UNASSESSED/STALE; HITL: approved_by human/critic, «timeout→agent» |
| Свёртка | attempts[] и artifact сворачиваются в experiment; constraints[] — в чип study |
| API | `GET /studies/{id}/graph` → `{nodes: NodeView[], edges: EdgeView[]}` — та же проекция |

## 9. Сравнение со старой онтологией (schema.py, 31 тип, ~40 рёбер, 20 AgentPerm)

| Старое | Новое | Почему |
|---|---|---|
| ResearchFormulation, Framing(derived), Spec | study (goal, constraints[] — поля) + constraint (если проверяется) | одно корневое; framing — вид веба |
| ResearchQuestion | question | оставлено |
| ResearchTask, PlanStep, ExperimentTask | experiment (+plan) | три «намерения» разного зерна → одна задача DAG |
| VerificationMethod | поле experiment.method | метод без задачи не существовал на деле |
| ExecutionAttempt | experiment.attempts[] | = Attempt, не узел |
| Hypothesis (6 статусов, переходы агентом) | hypothesis, вердикт вычисляется | источник отказов (недопустимые переходы) |
| Evidence (subtype, measured_on) | evidence + EvidenceRef | measured_on заполняет код |
| supports/refutes/refines | claim(stance) | ребро-утверждение становится узлом со ссылками (микропубликация) |
| ConfirmationCriteria, AcceptanceCriterion | criterion (статус из NodeRequirement) | две сущности одного смысла |
| Conclusion, Outcome(derived) | conclusion; outcome = study verdict | — |
| CodeArtifact, GeneratedData, Report, Publication, EfficiencyJustification, источники литературы | artifact(role paper/data/code/figure/table/log/report) + report | 5 типов с единственным статусом created; source слит в paper |
| Tool, Resource, EmpiricalBase | experiment.tool_resolution, Budget, artifact(data) | состояние ToolRouter/бюджета, не наука |
| ProcessInstance, ActionAssessment, GateDecision, Nonconformity | Result.check_results, HitlDecision, NodeRequirement | качество — проверки на узлах, не узлы |
| CostModel, EfficiencyMetric | отложено (Запрос заказчика, DESIGN §14.1) | вне объёма |
| AGENT_PERMISSIONS, research_commit, commit_failure_guard | удалены | агенты не пишут граф |
| conditional_successor, reframes | supersedes | одна связь ревизии |

Причина отказов в прогонах 1,2,4 (`runarch`): unknown edge type (`addresses`), отсутствует attrs.subtype / measured_on, недопустимые переходы статуса, висячие ссылки, недопустимый начальный статус. Каждая устранена по построению: тип/ребро/статус ставит код, атрибуты — обязательные поля схемы вывода, ссылки — `Ref` на входной контекст.

## 10. Литература (что взято)

| Источник | Взято |
|---|---|
| ORKG research contribution (problem–method–result; orkg.org) | question/experiment(method)/evidence как «вклад» |
| Micropublications (Clark et al., J Biomed Semantics 2014) | claim → cites → evidence → derived_from data |
| Nanopublications (Groth et al. 2010) | атомарный claim + provenance как единица |
| SEPIO (monarch-initiative) | evidence line с stance supports/refutes |
| W3C PROV-O | produced / derived_from / attempt как Activity; провенанс simulated |
| OBI / EXPO (Soldatova & King 2006) | experiment: goal, method, baselines, criterion |
| Google AI co-scientist (2025) | hypothesis rank, критик/турнир → plan critic |
| Sakana AI Scientist (2024), Agent Laboratory (2025) | линейный пайплайн idea→experiment→paper ≈ наш DAG; вердикт по метрикам кода |
| Toulmin / argument graphs | claim–evidence–warrant; warrant = criterion |

## 11. Открытые вопросы

Решено в v0.2: claim к критериям убран (статус из NodeRequirement); порог supported = `confirmations_needed`; литература даёт вердикт только через literature_search с собственным критерием («по литературе»); гипотезы и литература опциональны (plan.hypotheses/literature); проверки Тома Б 1, 8, 11, 12 получили Code-часть.

1. Подвопросы (decomposition) question → question — вводить ли ребро.
2. Формулировки Тома Б (PDF нет) — сверить Code-части п.1, 8, 11, 12.
3. CostModel/EfficiencyMetric для «Запроса заказчика» — поля experiment или узлы.
4. Конвертер старых research_active.json в новый граф — нужен ли.
5. Значения N/K/p для min_sources/recency по профилям.
