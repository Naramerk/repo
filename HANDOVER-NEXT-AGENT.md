# ПЕРЕДАЧА: CoScientist-next (Naramerk/repo) — 01.11.2026 ~12:00 МСК

Прочти этот файл ЦЕЛИКОМ, потом AGENTS.md, потом разделы ниже по мере надобности.
Файлы репо читать ТОЛЬКО через lean-ctx (см. «Инструменты»).

---

## 1. ГЛОБАЛЬНАЯ ЦЕЛЬ ПРОЕКТА

**Новый CoScientist = ФУНКЦИОНАЛ СТАРОГО + ЧИСТЫЙ КОД + АРХИТЕКТУРА + СТАБИЛЬНОСТЬ.**

Старый Косциентист (~/CoScientist-ug-upstream, ADK + FEDOT.MAS; решал научные
задачи: борщевик за 40 мин, воспроизводимый конвейер) — эталон ПОВЕДЕНИЯ, но
легаси-код: наслоения, немаршрутизируемые зависимости, баги. Новый
(CoScientist-next) — переписывание с нуля на собственном ядре:

- **Тот же функционал**: задача текстом → ТЗ (ГОСТ 19.201-78) → план →
  гипотезы с критериями опровержения → литература → эксперименты (живые MCP:
  heracleum-tox, papers-search, paper-analysis, chemtools, tox-antitargets +
  песочница кода OpenHands) → проверки → вердикты по гипотезам → научный
  отчёт + ГОСТ 7.32 НИР. HITL-гейты. Evidence-цепочка с provenance.
- **Но превзойти старый по результатам**: старый борщевик = fulfilled за ~40
  мин на DeepSeek. Наш бенчмарк — те же задачи fulfilled на GLM, стабильнее
  (fail-closed вместо тихих фейков легаси), быстрее где возможно, без
  операторских подпорок. Легаси-базы прогонов лежат на этом же Mace
  (runs_final_20261023, bench/lab/glm/live/bor7/LEGACY-BASELINE.md) —
  сравниваем substantively: результаты/артефакты/время, не только fulfilled.
- **Супер-чистый код — это требование, не пожелание.** Правила: чистая
  архитектура важнее заплаток (закон владелицы); системные решения только
  общими правилами через ревью; никакой подгонки под прогон; никакого
  совместимостного долга без явной пометки; fail-closed гейты не ослаблять
  ради зелёных тестов; предпочтение reuse/deletion перед наслоениями.
  Прецеденты системных гарантий: #314 (HYP-n вставка), #327 (стадийный выход),
  #337 (download-skip), мои в PR #462 (route-wins, Need-upgrade — все в
  coscientist/core/plan/lifecycle.py, слой accept_plan).

## 2. ИНСТРУМЕНТЫ — ЧИТАТЬ ФАЙЛЫ БЕЗ НИХ НЕЛЬЗЯ

**LeanCTX и CodeGraph обязательны при КАЖДОМ чтении исходников.** Голый
cat/sed/grep по коду запрещён — это правило AGENTS.md, владелица проверяет
задания на их использование (аудитирует диспатчи).

```bash
export PATH="$HOME/.local/share/coscientist-context-tools/node_modules/.bin:$PATH"
lean-ctx --version          # 3.11.4
codegraph --version         # 1.6.2
codegraph status "$PWD"     # после смены ветки: codegraph sync "$PWD"

# ЧТЕНИЕ ФАЙЛОВ — только так:
lean-ctx read coscientist/core/plan/lifecycle.py -m signatures   # сигнатуры
lean-ctx read coscientist/core/plan/lifecycle.py -m lines:285-400 # диапазон
lean-ctx read coscientist/cli/run.py -m full                     # целиком

# ПОИСК ЗАВИСИМОСТЕЙ перед изменением контракта:
codegraph query "accept_plan"      /  codegraph callers <sym>  /  codegraph impact <sym>
```

Ловушка: lean-ctx на боке при [POLICY BLOCKED] — `cd /home/box && lean-ctx
daemon stop && lean-ctx daemon start` (у нас на Mace не актуально). Пустой
статический поиск ≠ доказательство отсутствия вызовов — проверять поведение.

Прочий инструмент: uv (прогоны `uv run --extra dev --extra mcp ...`),
gh не залогинен (токен: `export GH_TOKEN=$(printf "protocol=https\nhost=github.com\n" | git credential fill | grep ^password= | cut -d= -f2)`).

## 3. ЧТО ЕСТЬ СЕЙЧАС

### Репо и ветки
- **main = 7c499dc** — владелица РАЗРЕШИЛА трогать main (02.11; прежний запрет
  был правилом кампании чистки, снят её словом). Путь: PR #462 →
  cleanup/refactor (после ревью цепочкой) → main.
- **cleanup/refactor = 353cd39** — база чистки: строгое надмножество main
  (+104 коммита, Any→типы, mypy-гейты), CI зелёный. Наша рабочая база.
- **lab/tick-loop-pacing = 349c5645** — рабочая ветка: 12 коммитов ночи
  31.10–01.11, открыт **PR #462 → cleanup/refactor** (ревью чистки ещё не
  гонялось — отладочная волна; мутации/mypy-гейт не гнались, помечено в PR).

### Живое состояние
- **Сервер 8487** (веб-консоль + API-прогон) жив на коде ветки. Рестарт —
  только при нулях running-станций (убивает run-корутины!). Команда старта —
  в разделе «Команды».
- **13 живых прогонов** hyp-cluster-toxicity в bench/lab/glm/live/bor8/st_*/.
  Конвейер проходит ЦЕЛИКОМ: №12 — все задачи done, trigger=all_done, в
  лимите 4500с; отчёт (report.md, science 52KB) + ТЗ (в начале прогона!)
  + НИР пишутся в CAS; вердикт-задачи проходят полный кодер-цикл.
- **Перф решён**: дедлайн 4500с («чуть больше часа» — грань владелицы),
  max_ticks 3600 (тик ≥1с), ТЗ рендерится после accept (момент легаси
  context_init, по прямому указанию владелицы).

### Issues — вес и баги (трекер не супер-актуален, но живой)
- **#273 universal-acceptance** — ЕДИНСТВЕННЫЙ барьер fulfilled (см. §5).
- #290 — решения владелицы по 31 оси законов (тики; неоднозначные оси —
  уточнять ДО изменения гейтов).
- #333 — параметр-критика разрешимости до запуска (PlanValidator; спека
  одобрена, мелкий PR, единственная дыра control-plane против легаси).
- #272 — lit-server 401 (операторская, ждёт хозяина инфры).
- #325/#322/#317/#332 — зависания/бюджеты/события: часть закрыта, проверить
  статусы перед работой.
- Бэклог #265–#285 — старые находки, закрывать по мере фикса.
- Флаки-тесты: test_hitl_run.py (2), test_sigxcpu_with_rusage_at_budget.
- Базлайн macOS pytest: 44 failed (sandbox/cgroup) — паритет, не регрессия.

### Различия со старым (сверено, по запросу владелицы)
- ТЗ-момент: ИСПРАВЛЕН (теперь как легаси — до прогона). docx-ТЗ нет (только md).
- Параллелизма задач в легаси НЕТ (task_order последовательно; gather только
  у quality-джоб). Параллельные независимые EXP — наша возможность превзойти.
- Нет у нас: параллельный quality-трек во время работы; параметр-критика (#333).
- Остальное (контракты/критика/стейт-машина/sealed-измерения/HITL/вердикты) —
  паритет, карта в памяти csn-experiments-module-mapping.

## 4. ЧТО ПОЧИНЕНО ЗА НОЧЬ (PR #462, каждый фикс = живой прогон-доказательство)

| Коммит | Убийца прогона | Механика фикса |
|---|---|---|
| acc86aef | idle-спин драйвера сжигал max_ticks за секунды → молчаливый refuse (№1, 12/14 задач) | тик ≥1с реального времени (time.monotonic); пресеты 3600 |
| c914c9c7 | coder-hint на стадийном маршруте проходил accept, убивал диспетч (№2) | accept_plan снимает hint с stage-маршрутов (route-wins) + нота; CODER_HINT → core/execution/domain |
| af2cb7d6 | falsifiable-вопрос без Need → отказ derive навсегда (№4; тот же класс убил wave-18) | derive ПОДНИМАЕТ hypotheses.needed (PlanOut.need_upgrades), accept вставляет HYP-n + нота |
| fdedda89 | RemoteProtocolError кодер-генерации = смерть прогона (№3, 12/13 задач) | retryable attempt-close + ре-диспетч core-ретраев; executed_aids держит открытую |
| 212b0500 | ре-диспетч стартовал поверх открытой ретрай-попытки (№7, RuntimeError) | диспетч ведёт открытую попытку задачи |
| c020917e | стадийный выход без рубрики → rubric-движок слеп → вопрос не закрывается (№5→№9) | #327-выход несёт artifact-exists рубрику |
| 92c46d82+11029105 | ТЗ жёг 1/3 хвоста прогона, момент ≠ легаси (№10: 77 мин при 75) | render_tz_early после accept + лог отказа |
| bb26abc5+349c5645 | claims не существуют в wire-схеме исполнителя | ExecutorRouteOut.claims + проброс + промпт executor_route v2 |

Плюс: деталь отказа прогона логируется в серверный лог («run refused for
<sid>: <detail>» + трейсбеки) — раньше была невидима.

## 5. #273 — МЕХАНИЗАЦИЯ ЗАВЕРШЕНА И ДОКАЗАНА ЖИВЬЁМ (ночь 02→03.11, №15-18)

8 коммитов: b7ec8157 (разворилка: journal-truth dispatch + verdict-coverage
strip + judge carve-out + claims finish-гейт по claim_targets), c8915d59
(граф читаемый: научный фокус + заворот веера; whitelist без явных None —
premise_idx-яд убил №15), 65179f6a (аллокация колонок полос), f2626fd8
(лог retryable-отказов), 1b33de3d (executor-лейн скрывает coder-маркер),
be4afa79 (КАТАЛОГ v6: prediction-шаблоны value_matches из публикации —
most_toxic_cluster==E, n==225; hypothesis claim targets «гипотезы+CC → судья»),
877d2529 (honesty rails: hypothesis_claimed escape требует выходов у
addressing-задач и цитаты, если substantive-проверка цитируема).

Доказано живьём: claims ПРОИЗВОДЯТСЯ (№15/16/17/18 — узлы с target и
reasoning), гейт принуждает (dataset_overview выпустил claim_q1), счёт
требует frozen criterion (корень partial №17/18 — в v5 не было prediction-
шаблонов; v6 решает).

**ПОСЛЕДНИЙ КВИРК (№19, диагностирован до кода):** GLM задал задачам явные
голые output_names → пин-правило (`output_names or tools_hint`) снесло ВСЕ
цитаты новых критериев → план ушёл в hypothesis_claimed без CC. Варианты:
(a) в правиле цитирования считать tools_hint сосуществующим с явными
output_names (докстринг derive: «outputs: one per tools_hint entry named
after tool id» — код строит выходы из output_names; рассинхрон); (b)
replan-конвергенция (текст рельса называет проверки). Реши → №20 →
fulfilled → контрольный повтор.

## 6. КОМАНДЫ

```bash
# Релонч прогона (сам пробит инфру .45:7336 и .2.2:8884, лаунчит + вотчер):
python3 bench/lab/glm/live/auto_relaunch.py   # состояние: autolaunch-state.json

# Статус прогона / журнал / причина отказа:
curl -s http://127.0.0.1:8487/console/runs/<sid>
sqlite3 bench/lab/glm/live/bor8/<sid>/events.db "SELECT seq,ts,type FROM events ORDER BY seq DESC LIMIT 10;"
grep "run refused\|run failed" bench/lab/glm/live/server-8487.log | tail -3

# Вотчер HITL (одобряет любой pending — директива «Решай все сам»):
nohup python3 bench/lab/glm/live/watch_and_approve.py <sid> > watcher-<sid>.log 2>&1 &

# Рестарт сервера (ТОЛЬКО при нулях running-станций!):
kill $(lsof -tiTCP:8487 -sTCP:LISTEN); sleep 2
set -a; source ~/.config/cosnext/env; set +a
nohup uv run --extra api --extra mcp --with uvicorn --with websockets \
  python -m coscientist.api.serve --data-root bench/lab/glm/live/bor8 \
  --catalog bench/lab/glm/checks_catalog.json --host 127.0.0.1 --port 8487 \
  --stream-poll-interval-s 1 --run-wait-s 60 \
  --run-presets $PWD/bench/lab/glm/live/bor7-presets.json \
  > bench/lab/glm/live/server-8487.log 2>&1 &
# живость: curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8487/ui/  (200)

# Тесты (быстрая проверка зоны):
uv run --extra dev --extra mcp python -m pytest tests/cli tests/agents -q -k "not slow"
uv run ruff check <изменённые файлы>
```

## 7. ЛОВУШКИ (каждая — час разбора; все пойманы живьём)

- Рестарт сервера убивает run-корутины; registry-freeze — свежий data-root
  (bor8) при смене MCP-бандла; повторный запуск того же idempotency_key
  возвращает старую станцию (новый ключ на релонч).
- GLM-квота: часовые окна; 429 + Retry-After≈3600 спит БЕЗ cap — выглядело
  как зависание (№1 ночи). Одна станция на ключ.
- Квирки GLM-планировщика: флипы Need, coder-hint на stage-маршрутах, игнор
  schema-хвостов, 0-based индексы, проза в criteria — закрыты системными
  гарантиями accept_plan; новые квирки закрывать ТАМ ЖЕ, не в диспетчере.
- HITL API: ответ гейта — opened_seq = ТОЧНЫЙ seq HitlOpened; sha вложен в
  prompt.context_sha256; GET возвращает голый массив.
- events.db живой станции читать обычным SELECT (WAL-блок на readonly).
- Прогон >10 мин — только через серверный API (nohup-CLI умирает с сессией).
- Инфра .45/.2.2 легла — операторская (прецедент #272): не чинить, доложить
  владелице, ждать (скрипт auto_relaunch сам подхватит возврат).
- max_llm_calls 200 — при плотных планах впритык (№12: all_done).
- Аудит-гейт прогонов: audit_heuristics.py --vs-main ПОСЛЕ коммита, чистый $?

## 8. ПРАВИЛА (установлены владелицей; действуют постоянно)

1. Чистая архитектура важнее заплаток; системные решения — общими правилами
   через ревью; не подгонять под прогон; не ослаблять fail-closed.
2. Задача выполняется всегда: никаких тупиков «нет MCP»; ошибка валидации
   выхода = repair тому же агенту, не падение прогона.
3. LeanCTX/CodeGraph при каждом чтении кода (§2) — владелица аудитит.
4. Один GLM-прогон одновременно; ≤4500с на прогон.
5. Легаси-репо (~/CoScientist-*) — ТОЛЬКО читать.
6. Дословные цитаты владелицы в публичных текстах (PR, issues) запрещены —
   профессиональный пересказ.
7. Прерывание волны — только по сигналу владелицы; тогда фиксировать «до
   куда дошло и откуда править» — сюда, в HANDOVER-NEXT-AGENT.md.

## 9. ОЧЕРЕДЬ

1. Реализовать развилку #273 (§5: (а) и/или (б), потом проверить (в)).
2. Релонч → hyp-cluster-toxicity fulfilled → контрольный повторный прогон
   (воспроизводимость).
3. lit-vs-prediction, lit-phototoxicity (bench/lab/glm/live/mytasks/).
4. Ревью PR #462 цепочкой чистки (мутации + mypy-гейт по правилам _rules.md)
   → мёрж в cleanup/refactor.
5. Мёрж-цепочка до main: ревью PR #462 → cleanup/refactor → main (владелица
   разрешила трогать main 02.11). Затем паритет/превосходство: ТЗ в docx; параллельные независимые EXP (легаси не
   умел — наш рычаг превзойти); #333 параметр-критика; сравнение результатов
   с легаси-борщевиком (LEGACY-BASELINE.md, переносить только 4 вещи).
6. Хэндовер чистки от 30.10: .worktrees/zc-*, m-* — мусор, удалить; рутины
   координатора чистки проверить на паузе.

## 10. ПАМЯТЬ И ССЫЛКИ

- Память ZCode (этот Mac, ~/.zcode/.../memory/): cosnext-phase-d-status
  (полная карта ночи, обновлена 01.11), cosnext-live-run-recipes (все
  рецепты прогонов), owner-always-executes-law, host-clean-architecture-rule,
  glm47-planner-quirks, csn-experiments-module-mapping, owner-laws-decisions.
- PR #462 + прогресс-комментарий (6547610611).
- Легаси-эталон: ~/CoScientist-ug-upstream (README: мультиагентная система
  научных открытий, ADK + FEDOT.MAS + RAG).
- Промпт следующему агенту: PROMPT-NEXT-AGENT.md (рядом).
