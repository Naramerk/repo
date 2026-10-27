# HYP v2 — гипотезы как в легаси, в нашей архитектуре

Срез: `lab/hypothesis-v2` (base 4cab544). Только чтение легаси-бандла; вся запись —
в нашей событийной архитектуре (GraphNodeUpserted / claims / k-верdict-движок PR8b).
Статус: implemented (mock-verified; живых научных прогонов не было).

Источники легаси-референса (все факты — из файлов, пути указаны):

- `graphs/research_active.json` из бандла
  `/Users/nargiza/Downloads/Борщевик_Сосновского_инвазивное_растен_20261021_152316_cossession/`
  (schema v3, revision 340, 257 узлов; узлы `H1`–`H4` type=Hypothesis, `CC1`–`CC4`
  type=ConfirmationCriteria);
- `bench/lab/glm/live/bor7/LEGACY-BASELINE.md` (разбор легаси/rerun/bor7).

---

## 1. Легаси-референс: точная структура H1–H4 / CC1–CC4

### 1.1 Узел Hypothesis (легаси, `type: "Hypothesis"`)

Поля уровня узла: `id` (`H1`…`H4`), `status` (`formulated` → `under_verification`
через `status_history` с переходами и reason), `source` (`HypothesesAgent`),
`created_at/updated_at`, `validation_origin`.

`attrs` (точные имена):

| attr | пример (H1) | примечание |
|---|---|---|
| `formulation` | «Метаболиты … лежат внутри домена применимости моделей прогноза LD50 (мышь): не менее 70% соединений набора получают оценку applicability domain не ниже 0.6, а прогноз LD50 для ксантотоксина отличается от экспериментального значения 423 мг/кг не более чем в 3 раза…» | утверждение с числами внутри текста |
| `priority` | `high` / `medium` | |
| `status` (в attrs) | `formulated` | плюс `postponed_reason` у отложенных |
| `contributors` | `[{agent, basis: commit|delegation|status, at, exec_id}]` | provenance-цепочка |
| `discriminating_observation` | «доля соединений с AD ≥ 0.6 и кратность отклонения прогноза ксантотоксина от 423 мг/кг» | чем H отличается от соперников |
| `proposed_check` | «predict_molecule_profile для каждого метаболита (LD50 мышь + applicability domain) + name2smiles …; сверка с экспериментальным LD50 ксантотоксина 423 мг/кг» | чем проверять (инструменты) |
| `rationale` | «Роль applicability (ровно одна в наборе)… Отделяется от H3 одним наблюдением — распределением AD…» | почему гипотеза живёт, как конкурирует |
| `role` | `applicability` / `extrapolation` | роль в конкурирующем наборе |
| `selected` | `"true"` (строка!) | выбор набора |
| `report_artifact_id`, `report_stamp`, `report_lang`, `report_status`, `report_error` | | артефакт отчёта узла (у H2–H4) |

Численные предсказания зашиты В ТЕКСТ `formulation` (и повторены в CC):

- H1: ≥70% соединений с AD ≥ 0.6 И прогноз LD50 ксантотоксина в пределах 3× от
  эксперимента 423 мг/кг.
- H2: угловые (7,8)-фуранокумарины вне AD: медиана AD < 0.6 И ΔAD ≥ 0.2 против
  линейных (6,7)/простых кумаринов.
- H3: провал домена у всего класса: медиана AD ≤ 0.5, разброс медиан между
  кластерами < 0.2.
- H4: гепато/DILI/канцерогенность дают ложноотрицательный сигнал (< 0.5) несмотря
  на P450-механизм.

Все 4 — `selected: "true"` (competing set, «ровно одна в наборе» в rationale H1
означает одну роль applicability, а не один выбор). Статусы: `under_verification`,
вердиктов нет (OP-1 упал; LEGACY-BASELINE §1: «все under_verification, вердиктов
нет», CC-статусы `not_met` = «не вычислялись»).

### 1.2 Узел ConfirmationCriteria (легаси, `type: "ConfirmationCriteria"`)

Поля уровня узла: `id` (`CC1`…`CC4` — по одному на гипотезу), `status`
(`not_met`), `source` (`HypothesesAgent`), `created_at/updated_at`.

`attrs`:

| attr | пример (CC1) |
|---|---|
| `threshold` | «Подтверждение H1: доля соединений набора с AD ≥ 0.6 составляет ≥ 70% И прогноз LD50 (мышь, в/в) для ксантотоксина отличается от экспериментальных 423 мг/кг не более чем в 3 раза (\|log10(прогноз/423)\| ≤ 0.477). Опровержение: доля AD ≥ 0.6 < 70% ИЛИ кратность отклонения по ксантотоксину > 3 — тогда оценка экстраполирует и в пользу H2 или H3.» |
| `metric` | «доля соединений с AD ≥ 0.6 (0–1) и кратность отклонения прогноз/423 мг/кг» |
| `covered_norms` | «E5 (точность глобальных QSAR-моделей LD50 в пределах 2–3×…), C3 (методология applicability domain…), E4 (приоритет экспериментального значения 423 мг/кг…), C4 (только in silico…)» |
| `note` (опц.) | CC2: «Тип фуранокумарина присваивается по структуре, а не по AD: иначе критерий тавтологичен.» |

Ключевые легаси-числа: AD-пороги 0.6 / 0.5 / Δ0.2, доля ≥ 70%, кратность 3×
(|log10(pred/423)| ≤ 0.477), вероятности эндпоинтов 0.5, сверка LD50 ксантотоксина
с экспериментом 423 мг/кг (E7: oral 423 самки / 449 Swiss Webster / i.p. 250 /
s.c. 860).

CC в легаси так и не вычислялись (`not_met` с рождения; в rerun H1–H3 тоже
«отложены» без вердиктов — LEGACY-BASELINE §6 п.6). Это то, что v2 обязана
довести до реального вердикта.

## 2. Наша архитектура: что уже есть (смёржено до этого среза)

| механизм | где | состояние |
|---|---|---|
| Схема узла `hypothesis` (statement/prediction/rationale/rank/selected/source_task/verdict) | `coscientist/schema/graph_schema.yaml` (kind hypothesis, v0.9) | fields объявлены; `prediction`/`selected` required — **прод не создаёт узлы вообще** |
| Гейты RC4/RC5/RC6 | `core/plan/need.py` (`validate_hypotheses_artifact`: ≥3, ровно один `selected`, `prediction`+`rank`+`statement` строго), `core/plan/foreach.py` (`_hypothesis_artifact_rows` читает граф-узлы kind=hypothesis c `source_task`) | смёржено (RC8-матрица в `core/execution/machine.py` + `local_project`) |
| foreach/loop по гипотезам | `foreach.py` (`select_foreach_items`, rank-sort), `loop_predicates.py` (`hypothesis_verdict_scoped`) | работает по узлам графа |
| k-верdict движок | `core/claims/verdict.py` (`hypothesis_verdict`: supported/survived/refuted/inconclusive/untested + rule), `core/claims/claims.py` (`hypothesis_verdicts`), `core/claims/dispositions.py` (`report_hypothesis_verdicts`) | реальный, claims-based |
| Численные критерии по prediction | `CheckProposal(target_kind="hypothesis", check=json_pointer|metric_threshold|…|rubric)` → `checks/evaluate.py::_evaluate_proposed_check` → claim `checked_against` → вердикт | существующий движок, новых нет |
| Fold вердиктов | `StudyFinalized.payload.hypothesis_verdicts` → `local_project._fold_finalized_graph` → `machine.hypothesis_verdicts` | dict наполняется, но **на узлы attrs.verdict не попадает** (граф-дайджест читает `attrs.get("verdict")` — всегда None) |
| Journal HYP-стадии | `cli/journal.py::record_hypotheses` — только артефакт+values через `executor.record`; **граф-узлы гипотез не пишет никто** (в проде; в тестах узлы делают руками) | это и есть главный разрыв |

## 3. Контракт v2 (этот срез)

### 3.1 HypothesisOut v2 (`coscientist/agents/schemas.py`)

```
HypothesisDraft:  id, statement, rationale,        # v1
                  prediction, rank, question_ref, selected   # v2, required
HypothesisOut:    hypotheses: ≥3, ids unique, ranks unique, ровно один selected=True
```

- `prediction` — проверяемое численно/структурно предсказание (легаси
  `discriminating_observation` + числа из `formulation`/CC `threshold`).
- `rank` — порядок в конкурирующем наборе (легаси приоритезации; foreach-сортировка).
- `question_ref` — id вопроса исследования (легаси-привязка H→вопрос; наши id —
  `q1…` из `derive_plan`). Не ссылка на узел во время записи: вопросы
  материализуются в accept_plan, позже (§3.3).
- `selected` — ровно один True (гейт RC5 уже требует; повторён валидатором схемы —
  fail-closed на записи).
- Запись — строго v2 (нет `prediction`/`rank`/`question_ref`/`selected` → отказ
  валидации). Чтение старых v1 CAS — только при чтении:
  `hypothesis_rows_v1(raw)` распознаёт v1-форму (id/statement/rationale без
  обязательных v2-полей) и возвращает помеченные v1-строки для отображения/диагностики;
  гейты RC5/RC6 v1-строки по-прежнему отвергают (fail-closed сохранён — v1 не
  проходит в план/foreach).

### 3.2 Регистрация в граф (`cli/journal.py::record_hypotheses`)

При записи HYP-стадии к артефакту добавляются события (пакетом, до
`executor.record`):

- `GraphNodeUpserted` kind=`hypothesis` на каждую гипотезу:
  `node_id = {study_id}:hypothesis:{id}`, attrs = `{statement, prediction,
  rationale, rank, selected, question_ref, source_task: task_key}` — через
  `core/claims/graph_writer.graph_node_upserted` (валидация по схеме; новые
  optional-поля `question_ref` добавлены в `graph_schema.yaml`, additive).
- study_id берётся из attempt (fail-closed: нет → CliRefuse).
- Idempotency: digest-idem `graph_node_upserted`; повтор после краша между
  append и record даёт те же idem-ключи (dedup store).
- Ребра `addresses` при записи НЕ создаются: вопросных узлов до accept_plan ещё
  нет (HYP-стадия идёт до accept; fold GraphEdgeAdded отказывает на висячих
  концах — journal bug). Привязка материалится в §3.3.

Ровно один `selected` проверяет и схема, и RC5; вердикт не влияет на selected
(`test_pr10_foreach_quorum.py` — анти-паттерн «selected от вердикта» запрещён).

### 3.3 Привязка к вопросу (`core/plan/lifecycle.py`, участок accept_plan)

После регистрации вопросных узлов PlanAccepted: для каждого узла kind=hypothesis
с непустым `question_ref`, совпадающим с id вопроса принятого плана —
`GraphEdgeAdded(addresses, hypothesis → question)`. Инвариант схемы
`addresses.out_by_src = {hypothesis: "1..n"}` получает реальное наполнение.
`question_ref`, не совпавший ни с одним вопросом плана, остаётся на узле как
честная метадата (ребро не выдумывается); в отчёте такая гипотеза имеет
`question_id=""`.

### 3.4 Durable вердикты (fold = реальный, не заглушка)

Цепочка (вся существующая, ничего нового не выдумано):

1. Эксперимент записывает claims с `target_kind="hypothesis"` → claim-узлы +
   `about`-ребра (`resolve_existing_target` находит зарегистрированные §3.2 узлы —
   раньше им не к чему было привязаться).
2. Численная сверка prediction — существующие check-kind'ы. Два маршрута, один
   движок оценки (`checks/evaluate.py`):
   - каталожный численный критерий (`metric_threshold`/`value_matches` с порогом —
     легаси-форма «AD ≥ 0.6», «кратность ≤ 3×») в `TaskSpec.criteria`; claim с
     `checked_against=<template_id>` → `claim_outcome` (consistent/inconsistent);
   - preregistered `CheckProposal(target_kind="hypothesis")` (тот же
     `propose_check` → HITL → `_evaluate_proposed_check`; срез-владелец механизма —
     PR20).
   Исход проверки → k-верdict движок (`hypothesis_verdict`).
3. `report_hypothesis_verdicts` (finalize) → `StudyFinalized.hypothesis_verdicts`.
4. Fold (machine/local_project): помимо `self.hypothesis_verdicts`, вердикт
   прошивается в `attrs.verdict` узла гипотезы — durable, виден граф-дайджесту и
   отчету (`reporting/graph_digest.py` уже читает `attrs.get("verdict")`).

Покрыто тестом `tests/core/test_hyp_v2_numeric_check.py`: порог выполнен →
вердикт учитывает claim; порог провален → `refuted` (валидный научный результат);
claims нет → честный `untested`.

**verdict ≠ научный успех**: `refuted` попадает в отчёт как есть
(`hypothesis_report_row` пишет исходы claims; `study_view.verdict` не выводится из
вердиктов гипотез). Опровергнутая гипотеза — валидный научный результат.

**Недостаточно Evidence → честное отсутствие вердикта**: в нашем словаре это
существующие `untested` (rule=`no_counted`) / `inconclusive` — движок
`hypothesis_verdict` уже возвращает их и помечает `not_tested` (finalize:
`refute_unresolved`-маркеры). Легаси-понятие «unverifiable» ≡ наш `untested(no_counted)`
+ `not_tested`-маркер; новое значение enum НЕ вводится (схема
`verdict: enum(supported|survived|refuted|inconclusive|untested)` не расширяется —
это контракт PR8b/RC8-матрицы). Фантазировать численные исходы запрещено:
нет claims/проверок — нет вердикта, только честный маркер.

## 4. Миграция / совместимость

| было | стало | совместимость |
|---|---|---|
| v1 CAS/артефакт HYP (id/statement/rationale) | v2 (см. §3.1) | чтение v1 — `hypothesis_rows_v1` (только отображение); гейты v1 отвергают (уже так: `need.py` требует `prediction`) |
| прод без гипотезных узлов | `record_hypotheses` пишет узлы | старые журналы: узлов нет → foreach/RC5 честно отказывают как раньше; fold-апкачка не требуется |
| `attrs.verdict` всегда None | прошивается fold-ом из StudyFinalized | существующие дайджесты не ломаются (поле появилось только там, где есть вердикты) |
| `graph_schema.yaml` hypothesis без `question_ref` | +optional `question_ref: str` | additive; старые события валидны (fold не ревалидирует attrs по схеме) |

## 5. Границы среза и что осознанно НЕ сделано

- Не тронуто: `tools/coder/**`, `bench/lab/glm/**` (вкл. checks_catalog.json),
  цикл планирования/диспетчер `cli/run.py`, `api/**`, `web/**`.
- Движок проверок не расширялся: только существующие check-kind'ы
  (`checks/evaluate.py`), предложенные через существующий `propose_check`.
- `question_ref` не сверяется с вопросами на записи HYP (вопросов ещё нет —
  сверка на accept_plan, §3.3). Расхождение видно в отчёте (`question_id=""`).
- Гипотезный report-артефакт узла (легаси `report_artifact_id`) — не переносился:
  в нашей архитектуре отчёт живёт в report/conclusion-узлах finalize.
- Живых прогонов нет; всё mock-verified (кассетные тесты + юнит-матрицы).
