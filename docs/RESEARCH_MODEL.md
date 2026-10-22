# CoScientist-next — научная модель исследования (граф)

Статус: черновик v0.1 (2026-10-23). Дополняет DESIGN.md §2, §5. Один граф = научная модель = то, что рисует веб = то, на чём проверяется каталог требований.

## 0. Глоссарий (простыми словами)

| Понятие | Что это | Когда появляется (стадия DAG) | Кто создаёт | Что от него зависит |
|---|---|---|---|---|
| Исследование (study) | Корень графа: один запрос пользователя и всё, что сделано по нему. | До старта агентов | Код (ядро) | Всё остальное висит на нём; итоговый вердикт считается по нему |
| Цель (goal) | Чего хотим добиться в целом, одной фразой («построить токс-профиль…»). | spec | Агент tz_spec предлагает текст, узел создаёт код | Вопросы, критерии успеха, отчёт |
| Ограничение (constraint) | Рамки: бюджет, данные, нормы, этика, «без металлов». | spec | tz_spec → код | Методы и критерии (их нельзя нарушать) |
| Исследовательский вопрос | Что именно мы хотим узнать. Может быть несколько. | spec | tz_spec → код | Гипотезы, эксперименты, вывод (отвечает на вопрос) |
| Критерий успеха | Измеримая планка: метрика, сравнение, порог («R² ≥ 0.7»). Замораживается, как только по нему пошли данные. | spec (для цели) и plan (для эксперимента) | tz_spec / planner → код; проверяет код | Статус эксперимента, вердикт гипотезы, итог исследования |
| Гипотеза | Проверяемое утверждение с предсказанием («кластер A токсичнее B»). | hypotheses | hypothesizer → код | Литература, эксперименты, вердикт (подтверждена/опровергнута) |
| Источник литературы | Статья/датасет/база, найденные и прочитанные (DOI, PMC id). | literature | researcher (через инструмент поиска) → код | Доказательства из литературы |
| План | Набор экспериментов с зависимостями (ревизия, одобренная критиком/человеком). | plan | planner + critic → код | Эксперименты |
| Эксперимент (задача) | Один шаг плана: что делаем, каким методом и инструментом, на каких данных, с какими критериями. | plan (создаётся при принятии плана) | planner → код (ключ EXP-n даёт код) | Запуски, результаты, доказательства |
| Запуск (попытка) | Одно реальное выполнение эксперимента; бывает повтор. | experiment | Код (исполнитель) | Артефакты, результат |
| Артефакт | Файл: данные, код, таблица, отчёт. С хешем и происхождением. | любая стадия | Слой инструментов (код) | Доказательства ссылаются на него |
| Результат | Итог одной попытки: SUCCESS / PARTIAL / FAILED, посчитан кодом по проверкам. | experiment | Код | Статус задачи, вердикты |
| Доказательство (evidence) | Конкретное наблюдение с адресом: артефакт + JSON-путь + хеш значения (или цитата из источника). | literature, experiment | Агент указывает ссылку, код проверяет и создаёт узел | Утверждения, проверки требований |
| Утверждение (claim) | «Это доказательство поддерживает / опровергает гипотезу (или критерий)». | experiment, report | Агент предлагает, код принимает только со ссылкой на доказательство | Вердикт гипотезы, вывод |
| Вывод (conclusion) | Ответ на вопрос: что установлено, как, в каких границах. | report | reporter → код | Отчёт, следующее исследование (fork) |
| Отчёт | Документ (MD/HTML/НИР), собранный из графа. | report | Код + reporter (текст) | Ничего; конечный продукт |
| Требование (NodeRequirement) | Пункт каталога (Том Б / R / критерий плана), привязанный к узлу; статус MET/FAILED/… | при создании узла | Код (по каталогу) | Статус узла |
| Simulated / partial | Метка «посчитано на заглушке или неполных данных». | experiment | Код | Ограничивает результат до PARTIAL у всех потомков |

## 1. Принципы

1. Агент никогда не пишет узлы и рёбра. Агент возвращает типизированный вывод роли (`ResearchSpecOut`, `HypothesesOut`, `LiteratureOut`, `PlanOut`, `AgentTaskOutput`, `ReportProse`); `GraphBuilder` (код) превращает его в узлы/рёбра в одной транзакции с событием результата. Инструмента `research_commit` нет.
2. Тип, статус, id, рёбра — из enum и pydantic-схем; ссылка агента — только на id из входного контекста (`Ref[NodeKind]`); неизвестная ссылка = ошибка схемы вывода (один повтор), а не отказ записи графа.
3. Статус узла не хранится: выводится из состояния задачи, результатов, проверок и рёбер (§4).
4. Узлы неизменяемы (frozen); правка = новая ревизия + ребро `supersedes`.
5. Граф — проекция журнала событий (DESIGN §3.5): `NodeAdded`, `EdgeAdded` пишутся только `GraphBuilder`.

## 2. Узлы (NodeKind)

Общие поля: `node_id` (префикс+ulid, код), `kind`, `study_id`, `task_id` (кто создал), `attempt_id | None`, `revision`, `created_seq`, `provenance: real|partial|simulated` (вычисляется, §6).

| NodeKind | Префикс | Поля (обязательные — жирным) | Создаёт задача | Из вывода |
|---|---|---|---|---|
| `study` | ST | **request**, profile, catalog_version, parent_study_id | ядро (до DAG) | — |
| `goal` | G | **statement**, research_form (fundamental/exploratory/applied), trl | spec | ResearchSpecOut.goal |
| `constraint` | C | **subtype** (enum: resource/data/norm/ethics/domain/profile), **content**, limit | spec | .constraints[] |
| `question` | Q | **formulation**, gap, scope | spec | .questions[] (≥1; если пусто — код создаёт Q из goal) |
| `criterion` | CR | **metric**, **op**, **target**, tolerance, kind=code\|review, question (для review), frozen_at_seq | spec (уровень study), plan (уровень experiment) | .success_metrics[], TaskSpec.criteria |
| `hypothesis` | H | **statement**, **prediction**, rationale, rank, selected | hypotheses | HypothesesOut |
| `source` | S | **title**, **locator** (doi/pmcid/url), artifact_id (PDF), year | literature | LiteratureOut.sources (только найденные инструментом) |
| `plan` | PL | **revision**, hypothesis_id, critic_decision, hitl_decision | plan | PlanOut + CriticVerdict |
| `experiment` | X | **task_key** (EXP-n), **title**, **goal**, **method** (enum: computational/statistical/laboratory/literature_review/expert), **route**, tools_hint, dataset, baselines, required, tool_resolution (tool_found/tool_built/coder/tool_missing) | plan (при accept) | TaskSpec |
| `run` | RN | **attempt_n**, outcome, close_reason, simulated | experiment (ядро) | Attempt |
| `artifact` | A | **artifact_id**, media_type, sha256, role (data/code/figure/report/spec) | любая (слой инструментов) | ToolOutput |
| `evidence` | E | **kind** (literature/computational/experimental/expert), **statement**, **ref: EvidenceRef \| SourceQuote**, measured_on (обяз. для computational/experimental — код берёт из ToolCallRef/dataset) | literature, experiment | LiteratureOut.claims, AgentTaskOutput.claims |
| `claim` | CL | **stance** (supports/refutes/inconclusive), **target** (hypothesis\|criterion), reason | experiment, report | AgentTaskOutput.claims[].stance |
| `conclusion` | CN | **synthesis**, how_established, validity_bounds, open_questions | report | ReportProse |
| `report` | RP | **artifact_id**, format | report | рендер кода |

`result` не узел: это запись Result (DESIGN §2), видна как статус узла `run`. `tool`/`resource` — атрибуты `experiment` и бюджеты, не узлы. `requirement` — не узел, а значок на узле (§5).

## 3. Рёбра

Все рёбра создаёт код; пары (from,to) фиксированы; концы обязаны существовать (FK в проекции) — висячих рёбер нет.

| Ребро | From → To | Кардинальность | Кем выводится |
|---|---|---|---|
| `has_goal` | study → goal | 1..1 | spec |
| `constrained_by` | study → constraint | 0..n | spec |
| `asks` | study → question | 1..n | spec |
| `measured_by` | goal\|question\|experiment → criterion | goal ≥1, experiment ≥1 required | spec/plan |
| `addresses` | hypothesis → question | 1..n на гипотезу | hypotheses (ref из входа; нет ref → первый Q) |
| `informs` | source → evidence | 1 на lit-evidence | literature |
| `plans` | plan → experiment | 1..n; experiment ровно 1 plan | plan |
| `tests` | experiment → hypothesis | 0..n | plan (TaskSpec.hypothesis_refs) |
| `depends_on` | experiment → experiment | 0..n, ацикл. | plan |
| `uses_input` | experiment → artifact | 0..n, с source real/partial/simulated | ядро при P2 (InputBinding) |
| `ran_as` | experiment → run | 1..n (попытки) | ядро |
| `produced` | run\|task → artifact | 0..n; artifact ровно 1 producer | слой инструментов |
| `derived_from` | evidence → artifact\|source | ровно 1 | код по EvidenceRef |
| `about` | claim → hypothesis\|criterion | ровно 1 | код |
| `cites` | claim → evidence | ≥1 (без доказательства claim не создаётся) | код |
| `answers` | conclusion → question | 1..n | report |
| `based_on` | conclusion → claim | ≥1 | report |
| `renders` | report → conclusion\|study | 1..n | report |
| `supersedes` | X → X того же kind | 0..1 | ревизия (plan, hypothesis, criterion) |

## 4. Жизненный цикл узла (вычисляемый)

Хранимого статуса нет. `node_status(n)` — чистая функция проекции.

| Узел | Статусы | Правило |
|---|---|---|
| goal, question, constraint, criterion(study), hypothesis, source, plan, experiment-до-запуска | `draft` → `final` | `final`, когда создавшая задача DONE(SUCCESS/PARTIAL); иначе `draft` |
| experiment | = TaskState/TaskOutcome задачи (pending/running/success/partial/failed/skipped) | DESIGN §3 |
| run | open / success / partial / failed | Attempt + Result.outcome |
| criterion | `pending` / `met` / `not_met` / `unassessed` | = статус NodeRequirement, созданного из критерия |
| evidence | `valid` / `invalid` | CodeCheck `source_link` + digest совпадает (re-read) |
| claim | `accepted` / `rejected` | accepted ⇔ все cites → valid evidence и (для criterion) значение по указателю удовлетворяет op/target |
| hypothesis (вердикт) | `untested` / `supported` / `refuted` / `inconclusive` | по accepted claims с stance: есть refutes → refuted; есть supports и нет refutes и все `tests`-эксперименты ≥PARTIAL → supported; иначе есть tests-эксперименты DONE → inconclusive; нет → untested. При provenance≠real вердикт помечается `(simulated)` |
| conclusion | `draft` / `final` | final ⇔ report-задача SUCCESS |

Агент не может «подтвердить гипотезу» — только дать claim со ссылкой на доказательство.

## 5. Задачи DAG → узлы

| Задача | Вход (читает) | Вывод роли | Узлы/рёбра (создаёт GraphBuilder) |
|---|---|---|---|
| (ядро) | запрос | — | study |
| spec | study | ResearchSpecOut | goal, constraint*, question+, criterion(study)+; has_goal, constrained_by, asks, measured_by |
| hypotheses | goal, question, constraint | HypothesesOut | hypothesis+; addresses |
| literature | hypothesis(selected), question | LiteratureOut | source*, evidence(literature)*, claim*; informs, derived_from, about, cites |
| plan | всё выше | PlanOut + CriticVerdict | plan, experiment+, criterion(experiment)+; plans, tests, depends_on, measured_by |
| experiment EXP-n | experiment, его входы | AgentTaskOutput | run, artifact*, evidence*, claim*; ran_as, uses_input, produced, derived_from, cites, about |
| report | вся проекция (read-only) | ReportProse | conclusion, report; answers, based_on, renders |

Валидация вывода (до записи): ссылки ⊂ id входного контекста; enum; обязательные поля. Ошибка → повтор с ошибкой схемы, затем Result FAILED(schema_invalid). Частично валидный вывод не пишется.

## 6. Провенанс simulated/partial

- Источник истины — `Attempt.simulated` (DESIGN §3.4). `provenance(n)`:
  - artifact, evidence, claim, run: от попытки-производителя (`produced` / `derived_from` / attempt_id);
  - claim: max по `cites`; hypothesis-вердикт: max по accepted claims; conclusion: max по `based_on`; study verdict `simulated` (DESIGN §3.2).
  - порядок: real < partial < simulated.
- Ребро `uses_input` хранит source (real/partial/simulated); по нему вычисляется simulated попытки.
- Evidence с provenance≠real не может сделать `criterion` = met сильнее PARTIAL (потолок у задачи) и в отчёте попадает в раздел «Выполнено на заглушках».

## 7. Привязка каталога требований

`RequirementTemplate.applies_to: list[NodeKind]` — ровно из §2. `CodeCheck` получил kind `graph_shape` (наличие рёбер/кардинальности) — проверка структуры графа, а не текста.

| Требование (Том Б) | Узлы | Проверка |
|---|---|---|
| 1 problem | study, question | Code: ≥1 question с непустым gap; Review: проблема сформулирована |
| 2 goals | goal, criterion | Code graph_shape: goal −measured_by→ ≥1 criterion с metric/op/target |
| 3 planning | plan, experiment | Code: DAG ацикличен, у каждого required experiment ≥1 criterion; каждая selected hypothesis имеет ≥1 входящее `tests` |
| 4 resources | constraint(resource), experiment.tool_resolution | Code: tool_resolution ≠ tool_missing у required; бюджет не превышен |
| 5 knowledge | source, evidence(literature) | Code: ≥N source с locator; evidence −derived_from→ source (source_link) |
| 6 data | artifact(data), uses_input | Code: schema_valid на входах; source=real у required |
| 7 config | run, artifact | Code: параметры запуска = ToolCallRef.args_digest сохранён |
| 8 model | experiment(method), artifact(code) | Review: метод соответствует вопросу (open Q) |
| 9 execution | run | Code: ≥1 run с outcome; нет открытых run у DONE задачи |
| 10 validation | criterion, claim | Code: metric_threshold/value_matches по EvidenceRef → criterion met |
| 11 comparison | experiment.baselines, evidence | Code: при заданных baselines есть evidence по baseline-метрике; Review |
| 12 application | conclusion.validity_bounds | Review (open Q) |
| 13 trace | evidence, artifact, run | Code: каждая evidence имеет ровно 1 derived_from, digest совпадает; каждый artifact — produced |
| 14 report | report, conclusion | Code: conclusion −answers→ все question; три раздела (real/stub/human) |

| Правило R | Узлы | Проверка |
|---|---|---|
| R05 (уникальные ключи) | experiment | инвариант ядра: task_key уникален |
| R11 (критерий заморожен) | criterion | Code: значения metric/op/target не меняются после первой evidence; правка = supersedes |
| R14/R16 (гипотеза проверяема) | hypothesis | Code: prediction непусто; Review |
| R17 (вердикт только по доказательствам) | claim, hypothesis | конструктивно: вердикт выводится из accepted claim (§4) |
| R23 (unassessed видны) | criterion(review) | статус unassessed не скрывается |
| R24 (худший required решает) | все | агрегация DESIGN §5.4 |
| R28 (simulated не success) | run, claim | потолок PARTIAL (§6) |
| R30 (источник обязателен) | artifact, evidence | Code: produced / derived_from ровно 1 |
| plan_criteria | criterion(experiment) | миграция DESIGN §5.5 → NodeRequirement на experiment |

Валидация каталога: каждый из 14 пунктов Тома Б и каждый RuleRef покрыт ≥1 шаблоном с `applies_to` ⊂ NodeKind; каждый NodeKind имеет defaults.

## 8. Веб

| Вид | Что рисуется |
|---|---|
| Колонки (ELK layered, слева направо) | 0 study/goal/constraint · 1 question/criterion · 2 hypothesis · 3 source/plan/experiment · 4 run/artifact/evidence · 5 claim · 6 conclusion/report |
| Цвет рамки | node_status (§4) |
| Штриховка/значок | provenance partial/simulated |
| Значки | NodeRequirement: MET/FAILED/UNASSESSED/STALE |
| Свёртка | run и artifact сворачиваются в experiment (раскрытие по клику); constraint — в чип study |
| API | `GET /studies/{id}/graph` → `{nodes: NodeView[], edges: EdgeView[]}` — та же проекция; веб ничего не выводит сам |

## 9. Сравнение со старой онтологией (schema.py, 31 тип, ~40 рёбер, 20 AgentPerm)

| Старое | Новое | Почему |
|---|---|---|
| ResearchFormulation, Framing(derived), Spec | study + goal + constraint | одно корневое; framing — вид веба |
| ResearchQuestion | question | оставлено |
| ResearchTask, PlanStep, ExperimentTask | experiment (+plan) | три «намерения» разного зерна → одна задача DAG |
| VerificationMethod | поле experiment.method | метод без задачи не существовал на деле |
| ExecutionAttempt | run | = Attempt |
| Hypothesis (6 статусов, переходы агентом) | hypothesis, вердикт вычисляется | источник отказов (недопустимые переходы) |
| Evidence (subtype, measured_on) | evidence + EvidenceRef | measured_on заполняет код |
| supports/refutes/refines | claim(stance) | ребро-утверждение становится узлом со ссылками (микропубликация) |
| ConfirmationCriteria, AcceptanceCriterion | criterion | две сущности одного смысла |
| Conclusion, Outcome(derived) | conclusion; outcome = study verdict | — |
| CodeArtifact, GeneratedData, Report, Publication, EfficiencyJustification | artifact(role) + report | 5 типов с единственным статусом created |
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
| W3C PROV-O | produced / derived_from / run как Activity; провенанс simulated |
| OBI / EXPO (Soldatova & King 2006) | experiment: goal, method, baselines, criterion |
| Google AI co-scientist (2025) | hypothesis rank, критик/турнир → plan critic |
| Sakana AI Scientist (2024), Agent Laboratory (2025) | линейный пайплайн idea→experiment→paper ≈ наш DAG; вердикт по метрикам кода |
| Toulmin / argument graphs | claim–evidence–warrant; warrant = criterion |

## 11. Открытые вопросы

1. Нужен ли `claim` для критериев или достаточно NodeRequirement (сейчас — оба, claim только для гипотез обязателен)?
2. Порог «supported»: достаточно одного accepted supports или нужна `confirmations_needed`?
3. Подвопросы (decomposition) question → question — вводить ли ребро.
4. Литература: claim из литературы может ли давать вердикт без эксперимента (обзорные исследования)?
5. Проверки Тома Б 1, 8, 11, 12 — только Review; согласовать формулировки (PDF Тома Б нет).
6. CostModel/EfficiencyMetric для «Запроса заказчика» — узлы или поля experiment.
7. Конвертер старых research_active.json в новый граф (для сравнения прогонов) — нужен ли.
8. Значение N в knowledge (минимум источников) по профилям.
