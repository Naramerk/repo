# ПЕРЕДАЧА: CoScientist-next (Naramerk/repo) — 05.11.2026 ~14:30 МСК

Прочти этот файл ЦЕЛИКОМ, потом AGENTS.md, потом разделы ниже по мере надобности.
Файлы репо читать ТОЛЬКО через lean-ctx (см. «Инструменты»).

---

## 1. ГЛОБАЛЬНАЯ ЦЕЛЬ ПРОЕКТА

**Новый CoScientist = ФУНКЦИОНАЛ СТАРОГО + ЧИСТЫЙ КОД + АРХИТЕКТУРА + СТАБИЛЬНОСТЬ.**

Старый Косциентист (~/CoScientist-ug-upstream, ADK + FEDOT.MAS; борщевик за
40 мин) — эталон ПОВЕДЕНИЯ, но легаси-код. Новый — переписывание с нуля на
своём ядре: задача текстом → ТЗ (ГОСТ 19.201-78) → план → гипотезы с
критериями опровержения → литература → эксперименты (живые MCP: heracleum-tox
+ песочница OpenHands) → проверки → вердикты по гипотезам → научный отчёт +
ГОСТ 7.32 НИР. HITL-гейты. Evidence-цепочка с provenance. Fail-closed вместо
тихих фейков. **Превзойти старый**: fulfilled на GLM, стабильнее, без
операторских подпорок.

Директивы владелицы сейчас (05.11): «продолжай до фуллфилд» + «отчёт должен
создаваться даже если что-то не получилось» — обе закреплены кодом, см. §4.

## 2. ИНСТРУМЕНТЫ — ЧИТАТЬ ФАЙЛЫ БЕЗ НИХ НЕЛЬЗЯ

LeanCTX и CodeGraph обязательны при КАЖДОМ чтении исходников (правило
AGENTS.md, владелица аудитит диспатчи).

```bash
export PATH="$HOME/.local/share/coscientist-context-tools/node_modules/.bin:$PATH"
lean-ctx --version   # 3.11.4
codegraph --version  # 1.6.2
codegraph status "$PWD"  # после смены ветки: codegraph sync "$PWD"

lean-ctx read <файл> -m signatures | lines:N-M | full
codegraph query "символ" / codegraph callers <sym> / codegraph impact <sym>
```

Прочее: uv (`uv run --extra dev --extra mcp python -m pytest ...`), gh не
залогинен (токен через git credential fill). Прогон живьём — только через
серверный API (nohup-CLI умирает с сессией).

## 3. ЧТО ЕСТЬ СЕЙЧАС

### Репо и ветки
- **cleanup/refactor = 353cd39** — база чистки (⊇ main, CI зелёный).
- **lab/tick-loop-pacing** — рабочая ветка, открыт **PR #462 →
  cleanup/refactor** (ревью чистки ещё не гонялось: мутации/mypy не гнались,
  помечено в PR). Вершина — `3fb7d0e1` (сплит смешанных hint'ов). Волна
  02–05.11 = 19 коммитов (§4). НЕ СМЁРЖ до ревью по правилам _rules.md.

### Живое состояние
- **Сервер 8487** жив на коде ветки. Рестарт — только при нулях
  running-станций (убивает run-корутины) И обязателен после коммитов
  (сервер импортирует код при старте; ответ
  «preset_configuration_changed_restart_required» на запуск = сигнал, что
  конфиг изменился с последнего старта).
- **№29 st_idem_bfdffb38 RUNNING** на полном стеке фиксов (старт 10:35 UTC
  05.11, дедлайн ~11:50 UTC): план с критериями на всех задачах, судейская
  `hypotheses_verdicts` с тулами predict_ld50+ad_flags+clusters.
- 29 живых прогонов hyp-cluster-toxicity в `bench/lab/glm/live/bor8/`
  (№15–№29 — волна этой недели, см. §5).

### Issues — вес и баги
- #273 universal-acceptance — механика реализована (§5); осталась
  эмпирика fulfilled (первый же прогон с честным счётом).
- #290 — решения владелицы по осям законов; #333 — параметр-критика до
  запуска (спека одобрена, мелкий PR); #272 — lit-server 401 (операторская).
- Флаки: test_hitl_stage_gates report-gate ×2 падают и на HEAD (macOS,
  проверено ворктри) — НЕ твоя регрессия.

## 4. ЧТО СДЕЛАНО ЗА ВОЛНУ 02–05.11 (19 коммитов, каждый с живым доказательством)

**Механика #273 (verdict = судейский лейн, claims обязательны):**
- b7ec8157 — развилка из передачи реализована: (1) accept-strip снимает
  coder-hint с любой задачи с вердикт-покрытием; (2) drive-цикл решает
  доставку по ЖУРНАЛЬНОМУ спеку, payload из журнала; (3) judge carve-out —
  hypotheses-задача с вердикт-покрытием идёт в executor; (4) claims
  finish-гейт: финиш без resolvable-клейма на каждый claim_target → один
  bounded repair → typed refuse.
- be4afa79 — **каталог v6** (bench/lab/glm/checks_catalog_v6.json): два
  prediction-шаблона value_matches из публикации (E most toxic; 225
  compounds) — до этого в каталоге не было НИ ОДНОГО prediction-критерия, и
  счёт был невозможен конфигурационно; v5 остаётся loadable; пресеты → v6.
  Плюс hypothesis claim targets («гипотезы+CC → судья»): задача с
  hypothesis_claimed-вопросом И замороженным comparison-критерием получает
  H-ids в claim_targets.
- cbc9e9c1 — declared outputs = explicit names ∪ tool receipt ids (№19:
  голые output_names сносили все цитаты пин-правилом).
- 877d2529 — honesty rails: hypothesis_claimed-escape требует выходов у
  addressing-задач и цитаты, если substantive-проверка цитируема.
- c62544ca — **сплит смешанных hint'ов**: GLM упорно планировала «тулы +
  код» одной задачей; три механизма отказа не сошлись (№22/27/28) — derive
  теперь разворачивает её в пару (инструменты + `-code` с зависимостью),
  критерии по пинам разъезжаются честно, PlanOut.mixed_splits → нота
  accept'а. Parse-валидатор исключён в пользу нормализации.
- 2c24cbc2 — route-wins на уровне derive (вердикт-задача теряет coder-маркер
  до смешанного гейта).

**Живые дефекты, найденные и закрытые:**
- c8915d59 — premise_idx:null из ExecutorClaimDraft-дефолта отказывал
  реестру на ЧТЕНИИ → отравленный журнал (№15 умер; такие станции в bor8
  дают 500 на /console — не чинить, списывать). whitelist больше не пишет
  явные None.
- 1b33de3d — зарезервированный 'coder' скрыт из тул-поверхности executor-лейна
  (№17: судейская задача зациклилась на вызовах неисполняемого тула).
- 9a9ec6fb — граф-схема 0.11: рёбра criterion→question (адреса критериев) +
  толерантный парс question_ref («Q1: …» — №14 имел 15 узлов и 0 рёбер).
- 65179f6a + c8915d59(граф) — **граф консоли читаемый** (директива владелицы):
  фокус «Научный граф» по умолчанию, заворот вееров в колонки-змейки,
  аллокация колонок полос без коллизий. Проверено живьём в браузере.

**Отчёты (директива «отчёт должен создаваться даже если что-то не получилось»):**
- 4f7fcc06 — защищённая финализация: FINALIZE_WINDOW_S=480 (диспетч не
  стартует первые попытки в последнем окне) + REPORT_LANE_GRACE_S=420
  (отчётные рендеры живут за границей дедлайна задач). До этого отчёт
  соревновался с задачами за одни секунды и умирал («отчёты мимо» — неделя).
- e67ac374 — post-finalize лейн идёт при ЛЮБОМ вердикте (№26: дедлайн-скип
  REPORT-барьера → тонкий report.md; partial никогда не получал полный
  отчёт). LlmError на partial не маскирует честный отказ.
- 1a5087bd — repair-сообщение клеймов: краткость причин (GLM-финиши
  трейдятся по длине) и два независимых основания на гипотезу.
- f2626fd8 — retryable-отказы попыток логируются в server-8487.log
  («attempt failed (retryable) for sid/task: detail») — незаменимо для дебага.

**Доказательства живьём:** claims ПРОИЗВОДЯТСЯ (№15–№26: узлы с target и
полным научным reasoning, порядок E>D>A>B>C, RMSE 0.603), гейт принуждает,
счёт работает (№21: H1 counted=1; №23: q1 answered, 21 клейм-узел; №25:
q2 answered, отчёт прошёл гейт после дедлайн-грани). Остаток — эмпирика
схождения GLM (см. §5).

## 5. ОТКРЫТАЯ РАЗВИЛКА — ПОСЛЕДНИЙ ШАГ ДО FULFILLED

Механика полная; вердикт partial в последних прогонах — стохастика GLM,
каждый прогон закрывает часть хвоста:
- №21 — дедлайн срезал отчёт (тепер защищён: окно+grace);
- №23 — судейская задача умерла на трейдовом JSON-финише (теперь reasons
  короткие + ретраи);
- №25 — research_result_payload (json_pointer вглубь MCP-конверта) required-
  validity отказом убивал все клейма задачи → указатель исправлен на presence
  `/` в v6 (сейчас в каталоге);
- №24 — GLM-варианс (3 schema-ошибки финиша подряд на dataset).

**Очередь:** №29 идёт → если partial, разобрать диспозиции
(`sqlite3 bor8/st_*/events.db` + StudyFinalized payload) → следующий
точечный фикс → релонч. Все предыдущие «нерешаемые» стены решались одним
точным изменением — не отступать. После fulfilled: контрольный повтор
(воспроизводимость), потом очередь §9.

## 6. КОМАНДЫ

```bash
# Релонч прогона (сам пробит инфру .45:7336 и .2.2:8884, лаунчит + вотчер):
python3 bench/lab/glm/live/auto_relaunch.py   # состояние: autolaunch-state.json
# 409 Conflict / preset_configuration_changed = нужен рестарт сервера.

# Статус / журнал / причина отказа:
curl -s http://127.0.0.1:8487/console/runs/<sid>
# /studies/<sid> во время рана даёт LeaseLost 500 — для статуса только /console/runs.
sqlite3 bench/lab/glm/live/bor8/<sid>/events.db "SELECT ts,type,substr(entity_key,-18) FROM events ORDER BY seq DESC LIMIT 10;"
grep -a "run refused\|run failed\|attempt failed" bench/lab/glm/live/server-8487.log | tail -5

# Клеймы/рёбра/узлы станции:
sqlite3 bench/lab/glm/live/bor8/<sid>/events.db "SELECT substr(entity_key,-40), json_extract(payload,'$.attrs.target'), json_extract(payload,'$.attrs.exclusion_reason') FROM events WHERE type='GraphNodeUpserted' AND json_extract(payload,'$.kind')='claim';"

# Рестарт сервера (ТОЛЬКО при нулях running-станций!):
PID=$(lsof -tiTCP:8487 -sTCP:LISTEN); [ -n "$PID" ] && kill $(ps -p $PID -o ppid= | tr -d ' ') $PID; sleep 3
set -a; source ~/.config/cosnext/env; set +a
nohup uv run --extra api --extra mcp --with uvicorn --with websockets \
  python -m coscientist.api.serve --data-root bench/lab/glm/live/bor8 \
  --catalog bench/lab/glm/checks_catalog.json --host 127.0.0.1 --port 8487 \
  --stream-poll-interval-s 1 --run-wait-s 60 \
  --run-presets $PWD/bench/lab/glm/live/bor7-presets.json \
  > bench/lab/glm/live/server-8487.log 2>&1 &
sleep 14; curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8487/ui/  # 200

# Вотчер HITL (одобряет любой pending — директива «Решай все сам»):
nohup python3 bench/lab/glm/live/watch_and_approve.py <sid> > watcher-<sid>.log 2>&1 &

# Тесты (базлайн macOS: 44 failed sandbox/cgroup — паритет, не регрессия;
# сверяй список с HEAD, а не количество):
uv run --extra dev --extra mcp python -m pytest tests/cli tests/agents tests/checks -q -k "not slow"
uv run ruff check coscientist/
```

## 7. ЛОВУШКИ (каждая — час разбора; все пойманы живьём)

- Рестарт сервера после коммитов ОБЯЗАТЕЛЕН до релонча: сервер импортирует
  код при старте (№16 умер на фиксе, который не был на сервере).
- Гейт валидации payload — только на ЧТЕНИИ (upcast_strict); append не
  валидирует. Отравленная строка = станция списывается (500 на /console/runs
  этой станции навсегда). Билдеры fold'ов — единственная защита.
- GLM-квирки (все закрыты системными гарантиями accept/derive, не диспетчером):
  coder-маркер в смешанных hint'ах (теперь сплит), голые output_names
  (теперь union), флипы Need (RC4), 0-based индексы, трейд по длине финиша
  (краткость в repair-сообщении), schema-хвосты без принуждения (claims-гейт).
- КАТАЛОГ: счёт клеймов требует frozen criterion; prediction-шаблоны — v6.
  Добавление шаблона = SUPPORTED_CATALOG_VERSIONS + CATALOG_VERSION_HISTORY
  (catalog.py) + presets catalog_version/path. Пин-правило: цитата живёт,
  только если пин ∈ declared (output_names ∪ tools_hint).
- Дедлайн 4500с — факт владелицы (не поднимать); финализация защищена
  окном/grace, не увеличением дедлайна.
- max_llm_calls 200 впритык при плотных планах; max_attempts=3 на задачу.
- events.db живой станции читать обычным SELECT (WAL не блокирует).
- Инфра .45/.2.2 легла — операторская (auto_relaunch ждёт сам). VPN хозяйки.
- Аудит-гейт прогонов: audit_heuristics.py --vs-main ПОСЛЕ коммита, чистый $?.
- tests/.DS_Store, coscientist/.DS_Store — в .gitignore, не коммитить.

## 8. ПРАВИЛА (установлены владелицей; действуют постоянно)

1. Чистая архитектура важнее заплаток; системные решения — общими правилами
   через ревью; не подгонять под прогон; не ослаблять fail-closed.
2. Задача выполняется всегда: никакие тупики; ошибка валидации выхода =
   repair тому же агенту, не падение прогона.
3. **Отчёт создаётся при любом исходе** — даже если что-то не получилось
   (закон 05.11; закреплён e67ac374 + 4f7fcc06).
4. LeanCTX/CodeGraph при каждом чтении кода (§2) — владелица аудитит.
5. Один GLM-прогон одновременно; ≤4500с на прогон (факт владелицы).
6. Легаси-репо (~/CoScientist-*) — ТОЛЬКО читать.
7. Дословные цитаты владелицы в публичных текстах запрещены — пересказ.
8. Прерывание волны — только по сигналу владелицы; тогда «до куда дошло и
   откуда править» — сюда, в HANDOVER-NEXT-AGENT.md.

## 9. ОЧЕРЕДЬ

1. **№29 → разобрать диспозиции → fulfilled.** Точечные фиксы по мере
   вскрытия (см. §5); после первого fulfilled — контрольный повтор.
2. Ревью PR #462 цепочкой чистки (мутации + mypy по _rules.md) → мёрж в
   cleanup/refactor → main (владелица разрешила трогать main).
3. lit-vs-prediction, lit-phototoxicity (bench/lab/glm/live/mytasks/) той же
   схемой: релонч → дебаг-цикл → fulfilled.
4. Паритет/превосходство: ТЗ в docx; параллельные независимые EXP (легаси не
   умел — наш рычаг); #333 параметр-критика; сравнение с легаси-борщевиком
   (LEGACY-BASELINE.md в bor7, переносить только 4 вещи).
5. Хэндовер чистки от 30.10: .worktrees/* — мусор, удалить; рутины
   координатора чистки проверить на паузе.

## 10. ПАМЯТЬ И ССЫЛКИ

- Память ZCode (~/.zcode/cli/memories/projects/coscientist-next-…/memory/):
  cosnext-phase-d-status (точка возобновления, обновлена 05.11),
  cosnext-live-run-recipes, owner-always-executes-law,
  host-clean-architecture-rule, glm47-planner-quirks,
  owner-perf-and-multimodel, csn-experiments-module-mapping.
- PR #462 + прогресс-комментарий (6547610611).
- Легаси-эталон: ~/CoScientist-ug-upstream.
- Промпт следующему агенту: PROMPT-NEXT-AGENT.md (рядом).
- Отчёты читать: `/studies/<sid>/artifacts/<aid>` (имена report.science.md /
  report.md (минимальный) / tz.gost-19.201-78.md / nir.gost-7.32-2017.md);
  список — `/console/studies/<sid>/artifacts`.
