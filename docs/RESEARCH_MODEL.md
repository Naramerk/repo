# CoScientist-next — научная модель исследования (граф)

Статус: черновик v0.4 (2026-10-23). Дополняет DESIGN.md §2, §5, §7.3. Один граф = научная модель = то, что рисует веб = то, на чём проверяется каталог требований.
Источник истины для типов, рёбер, кардинальностей и правил — `coscientist/schema/graph_schema.yaml` (единственная копия, упакована в wheel); таблицы §2, §3, §7 (Том Б и правила R) между маркерами `gen:*` генерирует `scripts/gen_model_tables.py`; поля и кардинальности структурированы (name, type, required), из них строятся pydantic-модели (`gen_model_tables.models()`). CI (`.github/workflows/ci.yml`): ruff, `gen --check`, pytest. Тесты сверяют таблицы, rule_id и 14 id Тома Б с вендоренными `vendor/ai4research/*.json` (sha256 в SHA256SUMS), допустимые пары (from,to) рёбер, CodeCheck.kind и NodeKind с DESIGN. Независимый оракул правил — `tests/refmodel.py` (сверяется с `coscientist.core.claims.rules` через `impl=`).

## 0. Глоссарий (простыми словами)

Стадии DAG: `spec → plan → [HYP-n | LIT-n | EXP-n | PLAN-n]* (`PLAN-0` — первый план; ключи всех серий с 0) → report`; после HYP/LIT planner может перепланировать: каждое перепланирование — своя задача PLAN-n (не больше `max_replans` из профиля; deadline ≤ study.deadline; доля бюджета study), результат — новая ревизия plan + `supersedes`. Гипотезы и литература не обязательны: planner решает (`plan.hypotheses`, `plan.literature` = {needed, reason}), профиль может обязать.

| Понятие | Что это | Когда появляется (стадия DAG) | Кто создаёт | Что от него зависит |
|---|---|---|---|---|
| Исследование (study) | Корень графа: один запрос пользователя и всё, что сделано по нему. Цель (goal) и ограничения (constraints[]) — поля study. | До старта агентов; goal/constraints — spec | Код (ядро); текст цели — tz_spec | Всё остальное; итоговый вердикт |
| Ограничение (constraint) | Рамки: бюджет, данные, нормы, этика. Обычно поле study; отдельный узел — только если по нему есть проверка (measured_by). | spec | tz_spec → код | Критерии, методы |
| Исследовательский вопрос | Что именно мы хотим узнать (≥1). Без гипотез вердикт получает ответ на вопрос. | spec | tz_spec → код | Гипотезы, эксперименты, вывод |
| Критерий успеха | Измеримая планка: метрика, сравнение, порог. Замораживается до первой evidence; статус — из NodeRequirement. | spec (study), plan (experiment) | tz_spec / planner → код; проверяет код | Статус эксперимента, вердикт |
| Гипотеза (опционально) | Проверяемое утверждение с предсказанием. Создаётся задачей HYP-n, если planner (или профиль) решил, что она нужна. | HYP-n | hypothesizer → код; выбор/правка — HITL | Эксперименты (tests), вердикт |
| Литературный поиск | Эксперимент с `method=literature_search` (отдельного типа узла нет): по вопросам, гипотезам и плану запросов (query_plan) найти статьи и выписать цитаты-доказательства. Планируется и повторяется как любой эксперимент (LIT-n), проверяется своими критериями: min_sources, question_coverage, recency, quote_valid. Бывшая стадия literature — просто первая такая задача LIT-0. | plan (LIT-n), в т.ч. до гипотез | planner планирует; выполняет researcher через инструменты поиска; узлы создаёт код | artifact(role=paper), evidence(literature), claim; эксперименты могут depends_on LIT-n |
| План | Маршрут: решения plan.literature/plan.hypotheses с причинами + задачи LIT/HYP/EXP с зависимостями. Правка = новая ревизия (supersedes). | plan, перепланирование | planner + critic → код; одобряет человек или critic | Эксперименты |
| Эксперимент (задача) | Один шаг плана: метод, инструмент, данные, критерии; попытки (attempts[]) и ограничения (limitations[]) — внутри узла. | plan | planner → код (ключ даёт код) | Артефакты, доказательства |
| Попытка (attempt) | Одно выполнение эксперимента: outcome, provenance real/partial/stub, config_digest. Не узел. | выполнение | Код | Outcome задачи |
| Артефакт | Файл или статья с ролью paper/data/code/figure/table/log/report. У paper идентичность — DOI/URL (дедупликация); у data — version и hash. Заменяет прежний узел source. | любая | Слой инструментов (код) | Доказательства ссылаются на него |
| Доказательство (evidence) | Наблюдение с адресом: computational/experimental — artifact_id + JSON-указатель + digest; literature — SourceQuote (статья, страница/раздел, цитата, digest). | LIT-n, EXP-n | Агент указывает адрес, код проверяет | Утверждения |
| Утверждение (claim) | «Доказательство поддерживает/опровергает гипотезу или ответ на вопрос». Агент указывает цель, доказательства и замороженный критерий; позицию (stance) вычисляет код из статуса критерия: met→supports, not_met→refutes, unassessed→inconclusive. | LIT-n, EXP-n | Агент предлагает, код принимает и вычисляет stance | Вердикт, вывод |
| Вывод (conclusion) | Ответ на вопрос: что установлено, как, в каких границах; может опираться на FAILED/inconclusive эксперимент. | report | reporter → код | Отчёт |
| Отчёт | Документ из графа. | report | Код + reporter | Конечный продукт |
| HITL-шлюз | Точка, где человек одобряет/правит или говорит «реши сам» (тогда решает critic). Шлюзы: plan, hypotheses, costly/non-real эксперимент, report; включает профиль; по таймауту — delegate, кроме costly_or_nonreal (fail, §5.2). Решение — событие с автором, на узле `approved_by` (human/critic), `delegated_by`. | plan, после HYP, перед дорогим EXP, report | Человек или critic; код записывает | Продолжение DAG |
| Требование (NodeRequirement) | Пункт каталога (Том Б / R / критерий), привязанный к узлу; статус MET/FAILED/… | при создании узла | Код (по каталогу) | Статус узла и критерия |
| Provenance real/partial/stub | Метка попытки: посчитано на реальных, неполных данных или заглушке. | выполнение | Код | Потолок outcome PARTIAL; вердикт не выше inconclusive |

## 1. Принципы

1. Агент никогда не пишет узлы и рёбра. Агент возвращает типизированный вывод роли (`ResearchSpecOut`, `HypothesesOut`, `LiteratureOut`, `PlanOut`, `AgentTaskOutput`, `ReportProse`); `GraphBuilder` (код) превращает его в узлы/рёбра в одной транзакции с событием результата. Инструмента `research_commit` нет.
2. Тип, статус, id, рёбра — из enum и pydantic-схем, сгенерированных из `schema/graph_schema.yaml`; ссылка агента — только на id из входного контекста (`Ref[NodeKind]`), обязательная там, где ребро обязательно (умолчаний нет); неизвестная или отсутствующая ссылка = ошибка схемы вывода (один повтор).
3. Статус узла не хранится: выводится из состояния задачи, попыток, проверок и рёбер (§4).
4. Узлы неизменяемы (frozen); правка = новая ревизия + ребро `supersedes` (plan, hypothesis, criterion); `derived_from` — только evidence → artifact.
5. Граф — проекция журнала событий (DESIGN §3.5): `NodeAdded`, `EdgeAdded` пишутся только `GraphBuilder`.
6. Модель не зависит от предметной области: 12 типов узлов покрывают любое эмпирическое или вычислительное исследование. Доменное — только в профилях: значения `method`, `route`, инструменты, CodeCheck kinds, шаблоны каталога, включённые HITL-шлюзы; новых NodeKind профиль не добавляет.

| Исследование | question / hypothesis | experiment (method) | evidence / criterion |
|---|---|---|---|
| Воспроизведение ML-бенчмарка | «воспроизводится ли accuracy статьи?» / «±1 п.п.» | обучение+оценка (computational), baseline = число статьи | метрики из JSON-лога; criterion `|Δacc| ≤ 1` |
| Анализ протокола wet-lab | «корректен ли протокол X?» / «шаг 3 даёт выход ≥ 70 %» | статистика по опубликованным данным (statistical) | таблица выходов; criterion по выходу |
| Обзор литературы | вопрос обзора; гипотез нет (plan.hypotheses.needed=false) | LIT-n (literature_search) | evidence(literature) + artifact(paper); criterion: min_sources, question_coverage |
| Симуляционное исследование | «как p влияет на y?» / «монотонно» | серия запусков (computational) | выход симулятора по указателю; criterion: знак тренда |

## 2. Узлы (NodeKind)

Общие поля: `node_id` (префикс+ulid, код), `kind`, `study_id`, `task_id` (кто создал), `attempt_n | None`, `revision`, `created_seq`, `provenance: real|partial|stub` (вычисляется, §6). Узлов `goal`, `source`, `run` больше нет: goal/constraints[] — поля study, source = artifact(role=paper), run = experiment.attempts[].

<!-- gen:nodes:start -->
| NodeKind | Префикс | Поля (обязательные — жирным) | Создаёт задача | Из вывода | Смысл |
|---|---|---|---|---|---|
| `study` | ST | **request**: str; **goal**: obj(Goal) (statement, research_form, trl); constraints: list(obj(Constraint)); **profile**: str; **catalog_version**: str; confirmations_needed: int (профиль, по умолч. 1); max_replans: int (профиль, по умолч. 2); parent_study_id: str | ядро (до DAG); goal/constraints заполняет spec | ResearchSpecOut.goal, .constraints[] | Корень графа; цель и ограничения — его поля |
| `constraint` | C | **subtype**: enum(resource\|data\|norm\|ethics\|domain\|profile); **content**: str; limit: str | spec | ResearchSpecOut.constraints[] с проверкой | Узел только если на него направлен measured_by; иначе — поле study.constraints |
| `question` | Q | **formulation**: str; gap: str; scope: str; final_type: enum(verdict\|artifact\|both) (M1 PR13; artifact ⇔ рубрика MET; verdict ⇔ claim; both — оба) | spec | ResearchSpecOut.questions[] (≥1, иначе schema_invalid); Plan.questions[] (core, PR 2) | Исследовательский вопрос; без гипотез вердикт получает ответ на него |
| `criterion` | CR | **metric**: str; **op**: enum(<\|<=\|==\|>=\|>\|in); **target**: str; tolerance: float; **check**: enum(code\|review\|rubric) (rubric = qualitative OutputSpec.rubric (PR13); never feeds claim stance); frozen_at_seq: int; rubric_digest: digest (M1 PR13 sha256(JCS(items+aggregate))); sesoi: float (PR8b smallest effect size of interest); min_power: float (PR8b e.g. 0.8; frozen with criterion); alpha: float (PR8b) | spec (study), plan (experiment) | ResearchSpecOut.success_metrics[], TaskSpec.criteria | Измеримая планка; статус = NodeRequirement (met/not_met/unassessed) |
| `hypothesis` | H | **statement**: str; **prediction**: str; rationale: str; rank: int; **selected**: bool; approved_by: enum(human\|critic); delegated_by: enum(human\|timeout); verdict: enum(supported\|survived\|refuted\|inconclusive\|untested) [computed] (PR8b projection; also persisted via VerdictSnapshotsRecorded) | задача HYP-n (опционально) | HypothesesOut (+ правка в HITL-шлюзе hypotheses) | Проверяемое утверждение; вердикт вычисляется (PR8b projection + VerdictSnapshotsRecorded) |
| `plan` | PL | **revision**: int; **hypotheses**: obj(Need) ({needed, reason}); **literature**: obj(Need) ({needed, reason}); critic_decision: enum(accept\|revise\|reject); approved_by: enum(human\|critic) (HITL plan gate; human\|critic); delegated_by: enum(human\|timeout) («реши сам»/timeout; budget gate does NOT set this on plan) | задача PLAN-0 и перепланирования PLAN-1..n (n ≤ max_replans) | PlanOut + CriticVerdict + HitlDecision | Ревизия плана (маршрута); правка = новая ревизия + supersedes |
| `experiment` | X | **task_key**: str (LIT-n \| EXP-n); **title**: str; **goal**: str; method: enum(computational\|statistical\|laboratory\|literature_search\|expert\|simulation) (optional on TaskSpec/plan stubs; simulation = scientific model run (not stub); input provenance still caps); query_plan: list(str) (для literature_search); **route**: str; **required**: bool; costly: bool; baselines: list(str); tool_resolution: enum(tool_found\|tool_built\|coder\|tool_missing); addresses: list(str) (question id[]; by plan (TaskSpec.addresses)); budget_share: float (M1 PR12; None = equal remainder; Σ ≤ 1); deadline_share: float (M1 PR12; critical-path Σ ≤ 1); **attempts**: list(obj(Attempt)) (n, outcome, close_reason, provenance=real\|partial\|stub, config_digest); limitations: list(obj(Limitation)) | PLAN-n (при accept) | TaskSpec | Задача DAG (LIT-n/EXP-n) с попытками внутри |
| `artifact` | A | **artifact_id**: str; **role**: enum(paper\|data\|code\|figure\|table\|log\|report); kind: enum(experiment_design\|contradiction_map) (M1 PR15; stamps core from explicit OutputSpec.kind (PlanValidator)); media_type: str; sha256: digest; doi_or_url: str (обяз. для paper; идентичность); title: str; year: int; status: enum(parsed\|download_failed) (paper); version: str (обяз. для data); hash: digest (обяз. для data) | любая (слой инструментов) | ToolOutput; LiteratureOut.papers (только найденные инструментом) | Файл или статья (бывший source); paper дедуплицируется по DOI/URL |
| `evidence` | E | **kind**: enum(literature\|computational\|experimental\|expert\|derivation) (M1 PR15 derivation = first principles); **statement**: str; **ref**: obj(EvidenceRef\|SourceQuote); measured_on: str | experiment (LIT-n, EXP-n) | LiteratureOut.quotes, AgentTaskOutput.evidence | Адресуемое наблюдение |
| `claim` | CL | **target**: ref(hypothesis\|question); **checked_against**: ref(criterion); **stance**: enum(supports\|refutes\|inconclusive) [computed] (вычисляет код: met→supports, not_met→refutes, unassessed→inconclusive; во входе роли запрещено); reason: str; validity_ok: bool [computed] (PR8/PR8b; false → invalid); claim_outcome: enum(consistent\|inconsistent\|indeterminate) [computed] (PR8b; null when invalid); severe: bool [computed] (PR8b Mayo severe gate); independence_key: str [computed] (PR8b sha256(experiment_id); merge by root-data overlap / shared templates); data_digests: list(str) [computed] (PR8b root data content digests (artifact meta; transitive walk)); criterion_template_ids: list(str) [computed] (PR8b structural independence share-any (no goal/title/plan_id)); p_value: float; power: float; sesoi: float (from catalog template uses_sesoi); uses_sesoi: bool [computed]; used_sesoi: bool [computed] | experiment (LIT-n, EXP-n) | AgentTaskOutput.claims[], LiteratureOut.claims[] (target + cites + checked_against; stance вычисляет код) | Утверждение со ссылкой на доказательство и критерий |
| `conclusion` | CN | **synthesis**: str; how_established: str; **validity_bounds**: str; open_questions: list(str) | report | ReportProse | Ответ на вопрос |
| `report` | RP | **artifact_id**: str; **format**: str; approved_by: enum(human\|critic) (HITL: human\|critic who approved); delegated_by: enum(human\|timeout) (HITL «реши сам»/timeout → critic; else absent) | report | рендер кода + HITL-шлюз report | Документ из графа |
| `hitl` | HITL | approved_by: enum(human\|critic) (кто утвердил: human или critic после делегирования); delegated_by: enum(human\|timeout) (human=«реши сам»; timeout=on_timeout=delegate; иначе нет); kind_gate: str (исходный HitlOpened.kind (plan\|budget\|…)) | HITL-шлюз (HitlDecided / delegate) | HitlDecided | Узел решения HITL; budget-шлюз пишет approved_by/delegated_by только сюда (не на plan) |
<!-- gen:nodes:end -->

`result` не узел: запись Result (DESIGN §2), видна как outcome попытки. `tool`/`resource` — атрибуты `experiment` и бюджеты. `requirement` — значок на узле (§7). HYP-n — задача DAG (TaskKind hypotheses), не experiment: её выход — узлы hypothesis. PLAN-n — задача TaskKind plan: её выход — ревизия plan.

Ссылки доказательства:
```python
class EvidenceRef(BaseModel):    # computational / experimental
    artifact_id: str; json_pointer: str; digest: str          # json_pointer обязателен ("" = весь документ); digest = sha256(JCS(значение))
class SourceQuote(BaseModel):    # literature (M1 PR14 / K9)
    artifact_id: str             # artifact(role=paper) или OCR-текст (свой sha256)
    locator: str                 # страница или раздел, напр. "p.6" / "§3.2"
    quote: str; digest: str      # digest = sha256(JCS(quote))
    source_sha256: str           # обяз.; = SourceFetched.digest = artifact.sha256
    lang: str | None = None
    normalization_version: str | None = None  # rules+unidata; None → UNASSESSED
    translation: str | None = None            # не проверяется кодом
```



## 3. Рёбра

Все рёбра создаёт код; допустимы только пары (from,to) из схемы (иначе ошибка записи, например evidence → plan); концы обязаны существовать (FK в проекции). Удалены: `has_goal` (goal — поле), `informs` (дублировал derived_from), `ran_as` (попытки внутри experiment).

<!-- gen:edges:start -->
| Ребро | Пары (from → to) | out / in | Кем выводится | Примечание |
|---|---|---|---|---|
| `asks` | study → question | 1..n / 1..1 | spec |  |
| `constrained_by` | study → constraint | 0..n / 1..1 | spec |  |
| `measured_by` | study → criterion, constraint → criterion, experiment → criterion | 1..n (experiment: 0..n) / 1..1 | spec/plan | experiment: ≥1 если required |
| `addresses` | hypothesis → question, experiment → question | 0..n (hypothesis: 1..n) / 0..n | HYP-n/plan (TaskSpec.addresses) | hypothesis ≥1; ref обязателен, умолчания нет |
| `plans` | plan → experiment | 1..n / 1..1 | PLAN-n |  |
| `tests` | experiment → hypothesis | 0..n / 0..n | plan | у experiment ≥1 из tests/addresses; refute+met → survived; attrs.intent: enum(confirm\|refute) (default confirm) |
| `depends_on` | experiment → experiment | 0..n / 0..n | plan | ацикл. |
| `uses_input` | experiment → artifact | 0..n / 0..n | ядро (P2) | provenance на ребре |
| `uses_claim` | experiment → claim | 0..n / 0..n | ядро (P2, InputSpec.kind=claim) | M1 PR7; научный вход claim |
| `contradicts` | claim → claim | 0..n / 0..n | ядро (противоположный stance к одной цели) | M1 PR15 §6.5; одна запись на неупорядоченную пару (min,max); повтор → Idempotent |
| `produced` | experiment → artifact | 0..n / 1..1 | слой инструментов | attempt_n на ребре; повторно найденная paper — то же ребро |
| `derived_from` | evidence → artifact | 1..1 / 0..n | код по EvidenceRef/SourceQuote |  |
| `about` | claim → hypothesis, claim → question | 1..1 / 0..n | код |  |
| `cites` | claim → evidence | 1..n / 0..n | код |  |
| `answers` | conclusion → question, conclusion → study | 0..n / 0..n | finalize/report | после финала: question in 1..1; study in 0..1; код enforce |
| `based_on` | conclusion → claim, conclusion → experiment | 0..n / 0..n | report | ≥1 based_on если disposition ∈ {answered,refuted,survived} — код; not_attempted → пусто |
| `renders` | report → conclusion | 1..n / 0..1 | report |  |
| `supersedes` | plan → plan, hypothesis → hypothesis, criterion → criterion, question → question | 0..1 / 0..1 | ревизия (PLAN-n, PlanExpanded, HITL-правка); question→question = C10/H-d |  |
| `decomposes` | question → question | 0..1 / 0..n | PLAN/PlanExpanded subquestions; origin=explicit|implicit|emergent; ацикл. (DESIGN_M1 §10) |  |
<!-- gen:edges:end -->

## 4. Жизненный цикл узла (вычисляемый)

| Узел | Статусы | Правило |
|---|---|---|
| question, constraint, criterion(study), hypothesis, plan, experiment-до-запуска | `draft` → `final` | `final`, когда создавшая задача DONE(SUCCESS/PARTIAL) и пройден HITL-шлюз (если включён) |
| experiment | = TaskState/TaskOutcome (pending/running/success/partial/failed/skipped) | DESIGN §3; outcome последней попытки |
| criterion | `pending` / `met` / `not_met` / `unassessed` | = статус NodeRequirement (claim к критерию не создаётся) |
| evidence | `valid` / `invalid` | §6.1 |
| claim | `accepted` / `rejected`; stance вычисляется | accepted ⇔ все cites → valid evidence и checked_against — замороженный criterion; stance = f(статус criterion) |
| hypothesis / question (вердикт) | `untested` / `supported` / `survived` / `refuted` / `inconclusive` / `untestable_by_agent` | §4.1 / `hypothesis_verdict`; `survived` при severe confirm without full support gate |
| conclusion | `draft` / `final` | final ⇔ задача report SUCCESS и шлюз report пройден |

### 4.1 Вердикт

Цель вердикта T — гипотеза; если гипотез нет (`plan.hypotheses.needed=false`), — ответ на вопрос (claim `about` question).

**Canon:** DESIGN_HYP_DELTA as on main. Sole implementation:
`coscientist.core.claims.verdict.hypothesis_verdict` (first-match; via `claims.hypothesis_verdicts`).
No second verdict path under `hypotheses/` / `tests/refmodel.py` (adapter
`verdict=hypothesis_verdict` directly; no refmodel oracle).
Symbols (do not restate counting formulas): `supported_needed`, `refutations_needed`,
`refutation_priority`, `refute_capable` / `refute_intent_counts`, `independence_key`,
`claim_counts_for_verdict`, `distinct_by_key`.

**Study thresholds on main today:** Field / kw defaults still exist on
`Study.confirmations_needed` / `refutations_needed` / `refutation_priority` /
`supported_needed` (`domain.Study`), on the claims bind path when `study is None`,
and on `hypothesis_verdict` / `rules.verdict` / `lifecycle.create_study` kwargs.
This section does not claim “no defaults” as already true (removal is out of scope here).

**Distinct** = by `independence_key` (`verdict.distinct_by_key` / HYP_DELTA §5).
Not `min(|attempt_id|, |artifact_id|)` / «из разных попыток и артефактов».

**Count filter** = `claim_counts_for_verdict` (DESIGN_M1 countable filter / HYP_DELTA V2).
Stub/partial/invalid/lit outside the countable path never tip a scientific verdict.

**Pre-branch (same fn):** build `use_inc` / `use_con` / `use_severe`; when
`catalog_tied` and delegated `proposed_check` would tip the catalog tie, prefer catalog-only
counts.

**Branches** (`hypothesis_verdict`, first-match code order; rule = 4th return):

| # | Condition | Verdict / rule |
|---|---|---|
| 1 | no about claims | `untested` / `no_counted` |
| 2 | no counted ∧ derivation review-only | `inconclusive` / `derivation_review_only` |
| 3 | `use_inc ≥ k_ref` ∧ `refutation_priority` | `refuted` / `k_ref` |
| 4 | `use_inc ≥ k_ref` ∧ ¬priority ∧ `use_severe` | `inconclusive` / `k_ref` |
| 5 | `use_inc ≥ k_ref` ∧ ¬priority ∧ ¬`use_severe` | `refuted` / `k_ref` |
| 6 | `use_inc > 0` | `inconclusive` / `inc_below_k_ref` |
| 7 | unresolved ∧ idle | `untestable_by_agent` / `idle_timeout` |
| 8a | `use_con ≥ k_sup` ∧ ≥1 capable refute ∧ unresolved | `inconclusive` / `refute_unresolved` |
| 8b | `use_con ≥ k_sup` ∧ ≥1 capable refute ∧ no unresolved | `supported` / `support_min` |
| 9a | `use_con ≥ 1` ∧ unresolved ∧ idle | `untestable_by_agent` / `idle_timeout` |
| 9b | `use_con ≥ 1` | `survived` / `single_severe` |
| 10 | unresolved ∧ idle | `untestable_by_agent` / `idle_timeout` |
| 11 | unresolved | `inconclusive` / `refute_unresolved` |
| 12 | counted remain (below support) | `inconclusive` / `no_counted` |
| 13 | only invalid remain | `inconclusive` / `validity_failed` |
| 14 | else (excluded-only bag) | `inconclusive` / `no_counted` |

`apply_derivation_ceiling` may rewrite supported/survived/refuted → `inconclusive` /
`derivation_review_only` inside `_ret`.

**Timeouts on main today:** sandbox/executor already set `timeout_kind` /
`refute_unresolved` on check effects. Paths that still omit those flags are a
documented gap (owned outside this doc); this section does not invent a second
classification path.

**Removed (was documented, not on main):** post-selection provenance≠real downgrade.
Non-real claims are excluded by the count filter before branching.

Disposition without hypotheses (HYP_DELTA §8): supported→`answered`, survived→`survived`,
refuted→`refuted`, else `inconclusive`.

Study `fulfilled` только если все required задачи SUCCESS и у каждого question вердикт (своей гипотезы или ответа) ≠ untested (DESIGN §3.2).

Агент не может «подтвердить гипотезу» — только дать claim со ссылкой на доказательство и критерий.

## 5. Задачи DAG → узлы

| Задача | Вход (читает) | Вывод роли | Узлы/рёбра (создаёт GraphBuilder) |
|---|---|---|---|
| (ядро) | запрос | — | study |
| spec | study | ResearchSpecOut | поля study.goal/constraints[], constraint*, question+, criterion(study)+; asks, constrained_by, measured_by |
| PLAN-0..n | study, question, (hypothesis, выходы LIT/HYP при перепланировании) | PlanOut + CriticVerdict (+ HitlDecision) | plan(hypotheses, literature), experiment+, criterion(experiment)+; plans, tests, addresses, depends_on, measured_by, supersedes (plan→plan) |
| HYP-n (опц.) | question, constraint, выходы LIT | HypothesesOut | hypothesis+; addresses (ref обязателен) |
| LIT-n (experiment, literature_search) | question, hypothesis?, query_plan, выходы depends_on | LiteratureOut | artifact(paper)*, evidence(literature)*, claim*; produced, derived_from, cites, about |
| EXP-n | experiment, его входы | AgentTaskOutput | artifact*, evidence*, claim*; uses_input, produced, derived_from, cites, about |
| report | вся проекция (read-only) | ReportProse | conclusion, report; answers, based_on, renders |

Валидация вывода (до записи): ссылки ⊂ id входного контекста; enum; обязательные поля и ссылки. `PlanValidator` отклоняет план, если experiment не связан ровно с одним plan (RT1) или какой-то question не покрыт ни одним LIT/EXP (через addresses или tests → hypothesis → addresses). Ошибка → повтор с ошибкой схемы, затем Result FAILED(schema_invalid). Частично валидный вывод не пишется.

### 5.1 Литературный поиск

Отдельного NodeKind нет: `experiment` с `method=literature_search`, route `literature`, ключи `LIT-n` (нумерация отдельно от `EXP-n`). Planner решает `plan.literature = {needed, reason}`; первая задача — `LIT-0`, профиль может её обязать. LIT можно ставить до HYP (обоснование гипотез), между EXP (baselines, параметры) и после (сверка).

- Вход: question, hypothesis (если есть), `query_plan` (из TaskSpec или строит researcher в той же попытке), выходы depends_on.
- Выход: `artifact(role=paper)` с DOI/URL — идентичность для дедупликации (DOI нормализуется; статья, найденная вторым LIT, — тот же узел, ребро `produced` остаётся от первого; тест `test_paper_dedupe_two_lits_same_doi`); `evidence(kind=literature)` с `ref = SourceQuote`; цитата обязана находиться в тексте статьи (CodeCheck `quote_in_source`), иначе evidence invalid. Нескачанная статья — artifact со status=download_failed, без evidence.
- Повторы — обычные попытки; fork перезапускает задачу как любую другую.
- Критерии (criterion на experiment, пороги — профиль): `min_sources` (≥ N paper с DOI/URL), `question_coverage` (каждый question имеет ≥1 valid evidence через claim), `recency` (доля статей не старше K лет ≥ p), `quote_valid`.

### 5.2 HITL-шлюзы

Совместимо с DESIGN §7.3. Профиль задаёт, какие шлюзы включены, и `on_timeout` для каждого: по умолчанию `delegate`, у `costly_or_nonreal` — `fail`.

| Шлюз | Когда | Что видит человек | Что может |
|---|---|---|---|
| `plan` | после critic, до запуска задач плана | plan.literature и plan.hypotheses с причинами, список LIT/HYP/EXP с depends_on, критерии | одобрить; править маршрут (добавить/удалить LIT/HYP/EXP, менять зависимости) → новая ревизия plan + `supersedes`; «реши сам» |
| `hypotheses` | после HYP-n | гипотезы с рангом и предсказанием | выбрать (selected), править текст (ревизия + supersedes), отклонить; «реши сам» |
| `costly_or_nonreal` | перед попыткой EXP с `costly=true` или с входом provenance≠real | метод, бюджет, входы и их provenance | разрешить, пропустить (SKIPPED, reason=human_rejected); «реши сам» |
| `report` | перед финализацией conclusion/report | черновик отчёта, вердикты, limitations | принять, вернуть на доработку (новая попытка report); «реши сам» |

- Решение — событие `HitlDecided(gate, node_id, author, decision, delegated)`; на узле: `approved_by: human | critic`, `delegated_by: human | None`.
- «Реши сам»: решает critic (агент) → `approved_by=critic, delegated_by=human`.
- Interactive, истёк deadline шлюза: `on_timeout=delegate` → решает critic, отметка «timeout→agent» (`approved_by=critic`, `delegated_by=timeout`); `on_timeout=fail` → задача FAILED(human_timeout), зависимые SKIPPED(input_unavailable), заглушки запрещены, продолжение — fork.
- Шлюз `costly_or_nonreal`, режимы `on_timeout` (ими же подчиняется явное «реши сам»):
  - `fail` (по умолчанию): таймаут → FAILED(human_timeout), зависимые SKIPPED, продолжение — fork. Явное «реши сам» допустимо, только если в профиле задан `Budget(delegated_costly)` и оценка стоимости ≤ остатка: тогда решает critic, `delegated_by=human`. Иначе событие reason=delegation_disabled, шлюз ждёт человека, и человеку сразу показывается причина: «нет бюджета», «превышает остаток» или «перерасход: делегирование заблокировано»;
  - `delegate_capped`: решает critic (по таймауту или «реши сам») в пределах того же бюджета; при отказе → DONE(SKIPPED, reason=budget);
  - `delegate`: critic без лимита — только если явно включено в профиле.
  - `Budget(delegated_costly)` — счётчик с состоянием: после каждой попытки списывается фактическая сумма (не оценка); если факт > оценки, дальнейшие делегирования блокируются до решения человека.
  - В каждом режиме пишется событие с reason; отчёт и веб показывают, кто решил (`decided_by`, `delegated_by`) и `spent_rub`. Исполняемая версия — `refmodel.costly_gate`, `DelegatedBudget`, `settle`; тесты `test_costly_gate_modes`, `test_delegated_budget_counter`.
- Headless: решает critic (`approved_by=critic`); для costly_or_nonreal — по тем же режимам (fail → задача не запускается, FAILED(human_timeout)).

## 6. Провенанс и валидность доказательств

- Источник истины — `Attempt.provenance: real | partial | stub` (DESIGN §2, §3.4); порядок real < partial < stub. Не-real ограничивает outcome попытки до PARTIAL.
- `provenance(n)`: artifact, evidence — от попытки-производителя (`produced`.attempt_n / `derived_from`); claim — max по `cites`; вердикт — max по accepted claims; conclusion — max по `based_on`; study verdict `stubbed` (DESIGN §3.2).
- Ребро `uses_input` хранит provenance входа; по нему вычисляется provenance попытки.
- Evidence с provenance≠real: criterion не сильнее PARTIAL у задачи; non-real claims do not enter
  `claim_counts_for_verdict` (§4.1 / HYP_DELTA V2); в отчёте — раздел «Выполнено на заглушках».

### 6.1 Валидность evidence
- literature: `ref` — SourceQuote; источник только `is_fetch` tool via `invoke_tool` (`FetchCapability` → `put_fetched` + журнал `SourceFetched`, не effect index; reject agent put / sandbox; сеть остаётся `tainted=True`, journal ≠ trusted); `quote_in_source` — token-boundary + digit-edge (`5 mg` ⊄ `-5 mg`/`0.5 mg`), CJK/Thai ≥8 символов без word-boundary, ≥4 letter-tokens иначе; super/sub→`^n`/`_n` до NFKC; `-\n` только между буквами (оба варианта); full casefold; presence≠polarity контекста; `fetched_digests` study-scoped; `source_sha256` обязателен; `translation` не проверяется.
- computational / experimental: `ref` — EvidenceRef с artifact_id, json_pointer (резолвится) и digest значения (совпадает при re-read).
- expert: ссылка на HitlDecided или ReviewCheck. Иначе — invalid.

### 6.2 Ограничения эксперимента
`experiment.limitations[]` (текст + ref на evidence/attempt) пишет код из Result (downgrade, partial входы, невыполненные критерии) и агент; conclusion.validity_bounds обязаны их учитывать. `conclusion.based_on` может указывать на FAILED/inconclusive эксперимент — отрицательный результат тоже вывод.

## 7. Привязка каталога требований

`RequirementTemplate.applies_to: list[NodeKind]` — ровно из §2. `CodeCheck` kind `graph_shape` — проверка структуры графа.

Том Б (14 пунктов; id = `research_metamodel_sources.json`; check_kinds ⊂ CodeCheck.kind):

<!-- gen:volume_b:start -->
| # | id | Узлы | CodeCheck.kind | Проверка |
|---|---|---|---|---|
| 1 | problem | study, question, hypothesis, experiment | graph_shape | ≥1 question; у каждого непустой gap и ≥1 входящее addresses; Review: проблема сформулирована |
| 2 | goals | study, criterion | graph_shape | study.goal непуст; study −measured_by→ ≥1 criterion с metric/op/target |
| 3 | planning | plan, experiment, question | graph_shape, rubric | DAG ацикличен; у required experiment ≥1 criterion и ≥1 из tests/addresses; каждый question покрыт ≥1 LIT/EXP (PlanValidator); final_type=artifact ⇒ OutputSpec.rubric; final_type=verdict ⇒ ≥1 criterion; plan.hypotheses/literature с reason |
| 4 | resources | constraint, experiment | graph_shape | tool_resolution ≠ tool_missing у required; бюджет не превышен |
| 5 | knowledge | experiment, artifact, evidence | min_sources, question_coverage, recency, quote_in_source | если plan.literature.needed: §5.1 на LIT-n |
| 6 | data | artifact, experiment | schema_valid | входы schema_valid; data имеет version и hash; provenance=real у required |
| 7 | config | experiment | graph_shape | attempt.config_digest = sha256(JCS(resolved config)) записан (R14) |
| 8 | model | experiment, artifact | graph_shape | method ∈ profile.methods; computational ⇒ produced artifact(code) или tool_found; Review: метод соответствует вопросу |
| 9 | execution | experiment | graph_shape | ≥1 попытка с outcome; нет открытых попыток у DONE задачи |
| 10 | validation | criterion, claim | metric_threshold, value_matches | по EvidenceRef → статус criterion; stance claim из него |
| 11 | comparison | experiment, evidence, claim | json_pointer, metric_threshold | для каждого baseline valid evidence с тем же metric и claim с checked_against на criterion сравнения; Review |
| 12 | application | conclusion, experiment | graph_shape | validity_bounds непуст; каждая limitation based_on-экспериментов упомянута; Review |
| 13 | trace | evidence, artifact | source_link, json_pointer | evidence ровно 1 derived_from, digest совпадает; artifact ровно 1 produced |
| 14 | report | report, conclusion, question | graph_shape | conclusion −answers→ все question; разделы real/stub/human; решения HITL |
<!-- gen:volume_b:end -->

Правила R (по реальным rule_id из `reviewed-rules.json`, CoScientist_dev@dev_1):

<!-- gen:rules:start -->
| rule_id | Узлы | Проверка |
|---|---|---|
| `R02:profile_applicability` | study, NodeRequirement | Code: каждый NOT_APPLICABLE имеет решение профиля (версия + rationale); отсутствие ресурса ≠ N/A; plan.hypotheses/literature.needed=false — с reason |
| `R04:process_instance_binding` | experiment.attempts[] | Code: попытка привязана к task_key, n, experiment и роли агента; вызов агента без попытки — не экземпляр |
| `R05:iteration_reactivation` | experiment, plan | Code: повтор = новая attempt или ревизия + supersedes (plan — новая задача PLAN-n); прежние не перезаписываются; task_key уникален |
| `R06:checkpoint_contract` | experiment, plan | Code: у задачи есть вход (depends_on DONE), завершение (criterion), доказательства (evidence), возврат (P3/HITL-шлюз) |
| `R11:evidence_locator` | evidence | Code: computational/experimental → artifact_id + json_pointer + digest; literature → SourceQuote + quote_in_source |
| `R11:json_pointer_evidence_locator` | evidence(computational) | Code: json_pointer резолвится в зарегистрированном JSON-артефакте, digest совпадает |
| `R14:configuration_snapshot` | experiment.attempts[] | Code: attempt.config_digest = sha256(JCS(resolved config)) записан до запуска |
| `R16:verification_validation_split` | criterion, claim | Code: verification (CodeCheck → criterion) и validation (вердикт по claim) — разные статусы; SUCCESS ≠ supported |
| `R17:predeclared_check` | criterion, claim | Code: criterion.frozen_at_seq < seq первой evidence; claim.checked_against — замороженный criterion; stance вычислен из его статуса; правка = supersedes |
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
| Штриховка/значок | provenance partial/stub; «по литературе» на вердикте |
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
| W3C PROV-O | produced / derived_from / attempt как Activity; провенанс stub |
| OBI / EXPO (Soldatova & King 2006) | experiment: goal, method, baselines, criterion |
| Google AI co-scientist (2025) | hypothesis rank, критик/турнир → plan critic |
| Sakana AI Scientist (2024), Agent Laboratory (2025) | линейный пайплайн idea→experiment→paper ≈ наш DAG; вердикт по метрикам кода |
| Toulmin / argument graphs | claim–evidence–warrant; warrant = criterion |

## 11. Открытые вопросы

Решено в v0.3: stance вычисляется из статуса критерия; distinct подтверждений — по `independence_key` (HYP_DELTA §5 / `distinct_by_key`), не по attempt/artifact; перепланирование — задачи PLAN-n (max_replans); ревизия plan — supersedes; HITL costly_or_nonreal: fail | delegate_capped | delegate. Решено в v0.2: claim к критериям убран (статус из NodeRequirement); порог supported = `supported_needed` (+ capable refute; `confirmations_needed` = PR8 snapshot `k` only); литература даёт вердикт только через countable filter (HYP_DELTA V2); гипотезы и литература опциональны (plan.hypotheses/literature); проверки Тома Б 1, 8, 11, 12 получили Code-часть.

1. Подвопросы (decomposition) question → question — вводить ли ребро.
2. Формулировки Тома Б (PDF нет) — сверить Code-части п.1, 8, 11, 12.
3. CostModel/EfficiencyMetric для «Запроса заказчика» — поля experiment или узлы.
4. Конвертер старых research_active.json в новый граф — нужен ли.
5. Значения N/K/p для min_sources/recency по профилям.
