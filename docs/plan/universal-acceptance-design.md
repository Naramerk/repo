# Универсальная приёмка: любой домен без операторского каталога

Статус: **S1 + S2 (closure-половина) реализованы** (S1: ветка
`lab/universal-acceptance-s1`, base `1139bb0` = origin/main; резолвер маршрутов
в `derive._resolve_acceptance_routes`, `claim_class` на
`QuestionIntent`/`QuestionDraft`/`QuestionSpec`, `acceptance_route` в плане +
`validate_final_types`. S2: ветка `lab/acceptance-route-closure`, base
`e25a66e`; dispositions потребляет `questions[].acceptance_route` —
`catalog_verified`/`evidence_grounded` закрываются `answered` на proven
receipts при отсутствии counting claims (единое `SUBSTANTIVE_CHECK_KINDS` в
`core/execution/domain.py`, derive импортирует оттуда; §8.v back-compat:
план без маршрута — прежнее поведение). Потребление проштампованных строк
отчётом (раздел evidence/hypothesis/unverified в minimal_report,
unverified_questions, провенанс порогов, reviewer-раздел) — следующой срез S2;
S3–S5 — не писаны).
Дата: 2026-10-27.
## 0. Закон системы (требование владельца, 2026-10-27)

**«Задача выполняется ВСЕГДА. Всегда можно взять кодер. Не всегда под задачу
есть MCP. Может — Alembic (кодер/песочница), может — кодер, может —
литература, может — гипотезы».**

Тупиковых отказов вида «нет подходящего MCP» не существует. Порядок маршрутов
исполнения: MCP-тулы — когда подходят; иначе **coder — универсальный фолбэк**
(модель пишет код в песочницу, артефакты/stdout в CAS с effect-receipts —
база evidence_grounded); литература и гипотезы — need-стадии, а не препятствия.
Отказ остаётся только для честно неисполнимого (falsifiable без вердикт-движков,
нет ни одной исполнительной поверхности). Ниже: evidence_grounded + coder
provenance — это и есть механизация закона; миграция (§2) обязана гарантировать,
что пустой satisfiable-набор НЕ уводит в отказ, если задача закрываема через
coder/grounded. Sanctions freeze-фикс песочницы — PR #277, параллельный срез
(не часть этого дизайна).

### 0.1. Converged laws (законы владельца подтверждены, 2026-10-27)

Владелец свёл таблицу расхождений с легаси: «всё остальное — как в старом».
Зафиксировано (полное проектирование новых возможностей — отдельными срезами
после S1): (1) отчёт при ЛЮБОМ терминале с объяснением что не так —
подтверждено отчасти, это S2, приоритет поднят (issue #279); (2) заморозка
порогов — как в легаси (порог замораживается после старта измерений, `met`
только с измерением) — входит в S3; (3) провенанс чисел — остаётся наш строгий
Evidence-only; (4) человек в контуре — HITL как есть; (5) мультиюзер — частично
(issue #284); (6) остальное — паритет со старым: websearch (#280), литература
parsed (#281), cost-metrics (#282), sandbox session reuse (#283), конкурирующие
гипотезы с вердикт-устранением (#285 — может пересмотреть RC5
exactly-one-selected, нужен отдельный дизайн-раунд до троганья need.py).
Влияние на срезы: S2 — приоритет поднят (#279); S3 — приобретает
порог-заморозку из легаси; S1 — без изменений.

Проблема: попытки 12/15/16 — derive-гейт «scientific answer requires a substantive
catalogue check» честно отказывает любой план, чьи verdict-вопросы не покрыты
операторским каталогом (satisfiable-набор пуст ⇒ enum criteria = max_length=0 ⇒
стена). Хозяин: «любую задачу система выполняла» — полный честный пайплайн без
предпосылки «оператор собрал каталог под домен».

## 0. Легаси-модель (эталон семантики)

Из /tmp/legacy-acceptance-semantics.md (легаси completion.py / contracts.py /
validator.py / jev.py):

1. Вопрос закрывается **grounded evidence** — Evidence-узлом с провенансом
   (зарегистрированные результаты, sha256 receipts); «a report does not close an
   open question». Вердикт-требований у нефальсифицирующих вопросов НЕТ.
2. Потребность в вердикте определяется **классом утверждения**, а не формой
   вопроса (JEV-triage): «запрос значения, описания, регламентного расчёта или
   обзора гипотезы не требует; утверждение для проверки — верификация».
3. Научная истина живёт в **контуре гипотез**: CC (численные критерии) создаёт
   hypothesizer ВМЕСТЕ с гипотезой; порог замораживается до/при начале измерения
   («a bar that moves after the measurement is not a bar»); `met` только с
   измерением; вердикт пишет независимый судья по CC+Evidence (confirmed /
   refuted / inconclusive). Оркестратор и исполнитель не судят.
4. Каталог-шаблоны и «never synthesize a numerical threshold»: критерии только
   заявленные; отсутствие вычислимого критерия — видимый setup-gap, а не повод
   придумать порог. Universal-минимум приёмки — ENG:registration_integrity
   (канонический receipt), не научный критерий.

Вывод: текущий гейт в новой системе пере-тяжел — он требует вердикт-критерий у
ВСЕХ final_type=verdict/both, т.е. смешивает «форма вопроса» с «классом
утверждения» и делает операторский каталог предпосылкой науки.

## 1. Целевая семантика

Три маршрута приёмки (acceptance routes), детерминированно выводимых в derive:

| route | кто поставил критерий/истину | чем закрыт | отметка в отчёте |
|---|---|---|---|
| `catalog_verified` | операторский каталог (method_sources) | substantive-критерий MET на effect-receipt | evidence-backed |
| `hypothesis_claimed` | модель: CC гипотезы (заморожены при HYP-accept) | claims-движок: вердикт по Evidence (k confirm/refute) | hypothesis-claimed |
| `evidence_grounded` | нет численного критерия | grounded receipt(s) с провенансом | evidence-backed, вопрос НЕ «проверен» |

Правила маршрутизации:

1. **Любой вопрос (любой final_type) закрывается grounded evidence** —
   зарегистрированные tool receipts / CAS-артефакты с provenance. Это универсальный
   пол приёмки (аналог легаси ENG:registration_integrity): целостность
   регистрации обязательна всегда, сегодня и так есть (artifact_exists-рубрики
   derive вешает на каждый output автоматически).
2. **Класс утверждения — новый typed-поля вопрос-интента** `claim_class ∈
   {value, review, falsifiable}` (легаси JEV-классы). Ставит планировщик
   (интент), derive не переписывает.
3. **verdict/both + falsifiable** ⇒ обязателен один из вердикт-движков:
   - (a) satisfiable substantive-критерий каталога (нынешний путь, не меняется);
   - (b) контур гипотез: `hypotheses.needed=true` → RC4 HYP-n → RC5 (≥3,
     prediction, ровно одна selected) → RC6/foreach EXP-n, где EXP-проверки
     компилируются из **CC выбранной гипотезы** (см. §3). Нет ни (a), ни (b) ⇒
     `PlanDeriveError` (fail-closed сохраняется; стена остаётся только для
     действительно непроверяемых утверждений).
4. **verdict/both + value/review** ⇒ route=evidence_grounded, честно
   маркируется; закрывает стены попыток 15 (lit_grounding) и 16 (synth_cost):
   это запросы значения/обзора, им не нужен фальсифицируемый порог.
5. **Операторский каталог — опциональное усиление**: непустой satisfiable-набор
   открывает (a); пустой не блокирует ничего, кроме falsifiable-без-гипотез.

## 2. Миграция derive-гейта

- `coscientist/agents/derive.py`: бинарный отказ заменяется резолвером маршрута
  (порядок: catalog → hypothesis → grounded → refuse). После PR264 гейт —
  per-question (верdict-вопрос без substantive-критерия у адресующих задач
  отказывает поимённо, ~L230-248); S1 ребейзится на эту семантику (§8.ii).
  Множество `_SUBSTANTIVE_CHECK_KINDS` (json_pointer, metric_threshold, …) и
  запрет считать существование (artifact_exists/schema_valid/source_link)
  substantive — **не меняются** (тесты
  `test_presence_or_unimplemented_content_checks_cannot_accept_science`,
  `test_empty_content_cannot_be_answered_by_met_artifact_rubric` живы).
- `coscientist/core/plan/plan_validate.py::validate_final_types`: правило
  «verdict ⇒ ≥1 criterion» расширяется до «verdict ⇒ criterion ИЛИ
  маршрут=гипотезы/grounded, записанный в плане явно» (не молча).
- **Закон §0 в миграции (обязательная проверка S1):** пустой satisfiable-набор
  сам по себе НЕ основание для отказа. Отказ допустим только если вопрос не
  закрываем ни одним маршрутом: falsifiable без (a)/(b) ИЛИ нет ни MCP, ни
  coder-биндинга на поверхности. При живом coder-биндинге (#261/#263 — coder в
  automatic-поверхности) без-MCP задача маршрутизируется через coder
  (tools_hint=[coder], output=coder, grounded-чеки на coder-receipt) — прецедент
  alembic_nomcp: fulfilled живым клоном cytopus 303/199 через кодер. Подтверждающий
  тест S1: «empty satisfiable set + coder ⇒ план принимается, questions
  routed grounded/hypothesis; refuse только falsifiable-без-движков».
- `coscientist/agents/schemas.py` (criteria enum, PR255 max_length=0): при пустом
  satisfiable-наборе планировщик вместо criteria-enum получает enum
  `claim_class` + рычаг `hypotheses.needed` (метаданные StudyContext, не
  промпт-заплатка); criteria остаются пустым кортежем — свободный текст по-прежнему
  запрещён.
- **RC4/RC5/RC6/foreach/require_hypotheses — без ослабления.** RC4 продолжает
  требовать HYP-n при hypotheses.needed; require_hypotheses продолжает форсировать
  Need (профильный уровень). Меняется доступность рычага: планировщик МОЖЕТ
  ставить hypotheses.needed на любом домене — контур гипотез становится
  универсальным вердикт-движком (вместо «профиль борщевика требует гипотез»).
- Затронутые тесты: `tests/checks/test_catalog_v5_bench.py:206`
  (gate-refuse — обновить: отказ только falsifiable-без-движков; `:173` passes —
  без изменений), `tests/agents/test_scientific_acceptance.py:85-98` (остаётся),
  `tests/agents/test_derive.py` (+ кейсы маршрутов), `tests/test_rc4_hyp_n.py`,
  `tests/test_rc5_hypotheses_artifact.py`, `tests/test_rc6_selected_ref.py`
  (семантика неизменна + новые кейсы derive-компиляции EXP из CC), кейсы
  plan_validate и планировщикных схем (route/claim_class).

Сценарий «домен без каталога» end-to-end: ввод задачи → планировщик видит в ctx
«satisfiable-набор пуст» → для вопросов-значений ставит claim_class=value
(маршрут grounded), для проверочных утверждений — claim_class=falsifiable +
hypotheses.needed=true → HYP-n (RC5-артефакт с CC) → EXP-n с проверками из CC →
исполнение с effect-receipts → claims-движок судит вердикты → отчёт с разделением
классов. Ни на одном шаге не нужен операторский шаблон.

## 3. Контур гипотез как вердикт-движок (HYP CC)

- `HypothesisOut` (PR260: id/statement/rationale/prediction/rank/selected)
  расширяется опциональными `confirmation_criteria`: metric-json-pointer +
  operator + target + band, **автор — модель**, вместе с prediction.
- Заморозка: CC фиксируются в HYP-accept-событии ДО любых измерений (легаси
  «бар не двигается после измерения»); EXP-задачи получают их как immutable
  проверк-контракт (derive-слой строит — прецедент «derive строит HYP-n из
  HYP-записи», спроектирован в HANDOVER §11.3, здесь — до конца).
- «Never synthesize a numerical threshold» соблюдён: порог приходит либо из
  каталога (method_sources), либо из явного CC гипотезы; система сама чисел не
  придумывает. Происхождение порога всегда восстановимо из журнала.
- Вердикт — существующий claims-движок (`hypothesis_verdict`,
  `report_hypothesis_verdicts` уже пишут вердикты в StudyFinalized/отчёт):
  считает только prediction-role claims с countable-effect (SESOI-машинария
  `refute_capable`), существование/validity не считаются — Duhem–Quine уже в коде.
- Отдельные вердикт-исходы в отчёте маркируются hypothesis-claimed: «истина по
  критериям гипотезы, не по внешнему методу».

## 4. Честность: почему это не «existence-заглушка проходят как наука»

Упрёв аудита 2026-10-26: schema_valid/source_link — заглушки (evaluate.py
430–473, §12.4), rubric принимал первый MET; derive сбрасывал критерии.
Компенсаторы в дизайне:

1. Существование никогда не научно: artifact_exists/schema_valid/source_link
   исключены из substantive-множества (консервация теста); их MET создаёт
   receipt-факт, не claim.
2. Обязательные отчётные поля, разделяющие три класса:
   - `question_dispositions[qid].acceptance_route` ∈ {catalog_verified,
     hypothesis_claimed, evidence_grounded, unverified} — рядом с существующими
     disposition/reason;
   - `evidence_backed` / `hypothesis_claimed` / `unverified` — сводка-раздел
     отчёта (списки вопросов и чисел по классам);
   - `unverified_questions` — обязателен, когда verdict/both закрыт через
     evidence_grounded (читатель видит: ответ есть, проверка — нет);
   - провенанс каждого порога: template_id+version+method_sources (каталог) или
     hypothesis_id + CC + HYP-accept-событие (гипотеза);
   - `hypothesis_verdicts` (уже существует) + `excluded_claims_report` (почему
     claim не в счёте) — без изменений, попадают в тот же раздел.
3. Grounded-route не производит claims: вердикт-поля (answered по счёту claims)
   остаются только у движков; evidence_grounded-вопрос получает disposition
   answered с reason=grounded_receipt и route-меткой — никогда «verified».
4. Отчёт/reviewer не только при fulfilled (§13 DoD): раздел классов пишется при
   любом терминале, иначе unverified нельзя показать.

## 5. Адверсариальный разбор (ложноположительные прохождения)

| FP | сценарий | компенсатор |
|---|---|---|
| existence как наука | план закрывает falsifiable вопрос existence-проверкой | исключение existence из substantive (тест); derive-маршрутизатор не даёт route=catalog_verified; отчёт — unverified |
| само-подтверждение | модель пишет CC-порог «>0» и «подтверждает» свою гипотезу | CC-заморозка до измерения; CC-линт (мёртвая банда, самопротиворечие — легаси criteria_lint, warnings); critic/reviewer видит prediction↔CC; refute_capable требует SESOI/prediction-role |
| обход маршрута | falsifiable маскируется под value ради лёгкого grounded-закрытия | claim_class — typed интент; derive: falsifiable без движков = отказ; распределение route в отчёте; критик сверяет класс утверждения с текстом вопроса |
| циркулярность | CC проверяется тем же receipt, что породил prediction | разделение HYP-n (формулировка) и EXP-n (измерение, свои effect-receipts); счёт claims только с EXP-проверок |
| первый MET (rubric) | rubric-агрегат принимает минимум | без изменений: вердикт по k=confirmations_needed/refutations_needed, счёт только prediction-claims |

Остаточный риск: качество модельных CC на слабом планировщике (glm-4.7) —
пороги могут быть тривиальны даже при заморозке. Это честно видимая слабость
(hypothesis_claimed в отчёте), а не нечестность системы; усиление — модель
сильнее на hypothesizer-роли, не гейт.

## 6. План реализации по срезам

| срез | объём | суть | порядок |
|---|---|---|---|
| S1 маршруты | ~250-350 строк кода+тесты | claim_class в QuestionDraft/QuestionSpec; резолвер маршрута в derive вместо бинарного гейта; validate_final_types; enum в схемах планировщика | 1-й — снимает стены 15/16 |
| S2 честный отчёт | ~200-250 | acceptance_route в dispositions-rows; раздел evidence/hypothesis/unverified в minimal_report; unverified_questions; провенанс порогов; reviewer видит раздел | 2-й (после S1) |
| S4 ctx-метаданные | ~50-80 | StudyContext: satisfiable-сводка + рычаги (hypotheses.needed, final_type/claim_class) как метаданные | параллельно S2 |
| S3 HYP CC v3 | ~500-700 (крупнейший) | confirmation_criteria в HypothesisOut; freeze при accept; derive строит системные CC-шаблоны (инвариант критерий-источника — §8.iii); CC-линт (criteria_lint-аналог) в срезе; claims-интеграция; RC-кейсы | 3-й — полные вердикты в новых доменах |
| S5 e2e без каталога | прогон | bench-fixture домена без каталога; живая попытка (дедлайн 2400, require_hypotheses по сценарию); сверка отчётного раздела | после S1+S2 (частично), финал после S3 |

Попытки bor7 разблокируются уже S1 (grounded-маршрут для lit/cost), научные
вердикты в новом домене — после S3. Существующие hogweed-сценарии с v5-каталогом
не меняют поведения (route=catalog_verified — прежний путь).

## 7. Контекст-инструменты (обязательная фиксация)

- Preflight: `codegraph --version` → 1.6.2; `lean-ctx --version` → 3.11.4;
  `codegraph status` → 2 modified → `codegraph sync` exit 0.
- CodeGraph: `query "substantive catalogue check scientific answer derive gate"`;
  `callers derive_plan` (19 хитов: tests/agents/test_derive.py,
  tests/checks/test_catalog_v5_bench.py:173/206, test_scientific_acceptance.py,
  continue/autonomous roundtrip); `callers derive_task_specs` (14).
- LeanCTX: `read coscientist/checks/evaluate.py -m signatures`;
  `read coscientist/core/claims/dispositions.py -m lines:600-720`
  (report_hypothesis_verdicts); чтения need.py (RC4/RC5/RC6),
  plan_validate.py (validate_final_types), schemas.py (criteria enum),
  cli/run.py (_satisfiable_criteria_templates), stance.py (claim_stance),
  kinds/rubric.py (judge), claims.py (prediction-role counting, refute_capable).
- Дополнение (ревью k=3): перечитан derive.py на main eb43112 (PR264 —
  per-question coverage, `_SUBSTANTIVE_CHECK_KINDS`/`_carries_substantive_criterion`),
  tests/agents/test_scientific_acceptance.py:182/194/287.

## 8. Дополнение по ревью (k=3, 2026-10-27)

### i. Правило grounded-закрытия ДО S2 (какие чеки привязываются к вопросу)

Grounded-маршрут — не « receipt существует ». На вопрос (его адресующие задачи)
обязательно вешается ≥1 **required content-bearing** чек над зарегистрированным
receipt'ом: json_pointer/value_matches/quote_in_source на содержимом output.
Голое существование (artifact_present-класс, auto-рубрики derive) доставляет
evidence, но никогда не закрывает вопрос. Прецедент уже в коде: coverage-тест
`test_empty_content_cannot_be_answered_by_met_artifact_rubric`
(tests/agents/test_scientific_acceptance.py:287) — пустое содержимое + MET
artifact-rubric = status failed. В S1 (до S2-отчётности) derive-резолвер для
route=evidence_grounded требует цитирование такого содержательного шаблона,
если satisfiable-набор непуст, а при пустом наборе — требует, чтобы хотя бы
одна адресующая задача произвела непустой content-bearing output; иначе
честный typed refuse (см. v-доп про безтульные вопросы).

### ii. Interaction with per-question coverage (PR264, мёржится/смёржена ПЕРВОЙ)

Ветка lab/acceptance-question-coverage уже смёржена (#264, main `eb43112`):
per-question планка «verdict/both-вопрос ⇒ ≥1 адресующая задача с required
substantive-критерием» — это готовый маршрут (a) резолвера S1. Правила:
- S1 ребейзится на eb43112; хелперы `_SUBSTANTIVE_CHECK_KINDS` /
  `_carries_substantive_criterion` (derive.py:81/95) переиспользуются как есть —
  новых параллельных определений substantive не появлятся.
- Coverage-тест «uncovered verdict question refuses»
  (test_scientific_acceptance.py:182/194) уточняется до falsifiable-семантики:
  отказ поимённо называет вопрос только для claim_class=falsifiable без
  маршрута; value/review-вопрос без критериев проходит с route=evidence_grounded.
- Правило одного автора: derive.py меняет только автор S1 (никаких
  параллельных правок гейта из S2/S3; S2/S3 потребляют резолвер, не дублируют).

### iii. Инвариант «catalog-only ⇒ criterion-source-only» (критично для S3)

Сегодня роль критерия авторитетна только из каталога
(`role_from_template`: "Authoritative role from the catalog template only"),
а счётные claims требуют `check_role == "prediction"` (claims.py ~L690,
Duhem–Quine). EXP-проверки из CC — это **системные шаблоны вне операторского
каталога**: без изменения инварианта они получат role=unknown ⇒ claims
uncountable ⇒ `hypothesis_verdict` не выстрелит — стена слоем глубже. Решение:
derive регистрирует CC-derived шаблоны в study-scoped системной секции ЕДИНОГО
Catalog (не второй каталог/ledger; Catalog один на study) с провенансом
hypothesis_id + HYP-accept-событие и явной ролью prediction; разрешение роли
расширяется типизированно (system-источник sanctioned ⟺ шаблон из замороженной
гипотезы). CC-линт (аналог легаси criteria_lint: мёртвая банда,
самопротиворечие) входит в S3; реалистичный объём S3 — 500–700 строк.

### iv. Sanction по claim_class (остаточный риск)

Принято: S1 выходит с **явно задокументированным остаточным риском**
(маскировка falsifiable под value), симметрично уже принятому риску качества
модельных CC. Компенсатор включается в S2: reviewer-роль читает распределение
маршрутов из отчёта; расхождение текста вопроса и claim_class — находка ревью
(consultative, как в file_handoff). Отдельного блокирующего гейта на S1 нет —
это осознанный выбор, а не упущение.

### v. Back-compat и границы

- Журналы: замороженные планы без claim_class/acceptance_route (старые прогоны,
  replay) ⇒ отчёт по умолчанию ставит acceptance_route=legacy/unverified —
  никогда не молча «catalog_verified».
- Replan между попытками: флип claim_class в новой ревизии плана — отчёт и
  вердикты считаются по **последней принятой** ревизии (исполнявшейся).
- Пустой MCP-набор (закон §0): тупика «нет подходящего MCP» нет — при живом
  coder-биндинге (#263) вопрос закрывается через coder-маршрут (код в
  песочнице → зарегистрированные receipts → grounded/CC-чеки); честный typed
  refuse остаётся только при полном отсутствии исполнительной поверхности
  (ни MCP, ни coder — инфра-дыра, маркируется явно). Need-гейты (LIT/HYP)
  не ослабляются — это need-стадии, не препятствия.
- Multi-catalog: Catalog один на study; CC-шаблоны живут в системной секции
  того же Catalog (§iii), второй каталог/реестр не создаётся.
- S2 ripple: новые поля отчёта (acceptance_route, unverified_questions,
  сводка классов) — изменения payload StudyFinalized/minimal_report +
  отображение в API/console web-ui; touchpoints перечисляются в S2-PR, UI —
  читатель, не источник истины.
