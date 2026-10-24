# Аудит хардкодов и эвристик в coscientist/core (main aa6ab7f, только чтение)

## Рекомендация по апкастерам
Гибрид, близкий к A:
1. Все типы событий свести к v1. Апкастеры, которые подставляют угаданные значения, удалить. Механизм оставить: register, assert_upcaster_chain_continuous и синтетический тест на фиктивном типе.
2. upcast() (upcasters.py:46) должен отказывать, если версия выше writer, тип неизвестен или в цепочке есть пропуск.
3. Писатели берут версию только из константы. Константу выводить из UPCASTERS, включая ATTEMPT_STARTED=9 в schema_versions.py.
4. Сделать общий helper обхода spec → inputs/expected_outputs (сейчас он скопирован примерно 10 раз). После релиза апкастер добавляет поле только с доказуемо точным значением, иначе событие нечитаемо или нужна явная миграция.
5. Ввести маркер формата журнала (store format version) и отказываться открывать старые dev-журналы. Иначе повторная запись с тем же idem даст Conflict из-за payload_digest (store.py:326-337, 456-467).
6. Фикстуру tests/fixtures/journals/reclassified_fetch_old собирать закоммиченным генератором из json. Сейчас её events.db собран вручную. До и после пересборки сравнить проекции.

Почему B хуже: он узаконивает 13 версий TaskCreated до релиза, а угадывания в StudyCreated и AttemptStarted остаются без источника. ingest-tar не затронут, потому что пользовательские архивы хранятся как артефакты, а не как события.

## Апкастеры: угаданные смысловые значения
- TaskCreated: mode="use_partial" :87 (живой default другой); kind="artifact", ref=None :108-109.
- _coerce_final_type_default :260-270: отсутствующее или неизвестное значение превращается в verdict (используется в :318 и :417); rollup="all" :457.
- StudyCreated:
  - finalize_reserve_s=0 :557, хотя доменный default равен 30;
  - refutations_needed=1 и refutation_priority=False :564-565; confirmations_needed=1 :572;
  - evalue тихо превращается в count, evalue_alpha=0.05 :579-583;
  - clamp supported_needed до ≥2 :590-595;
  - max_expansions выводится из max_replans, а max_human_expansions=10, replan_reserve=0.1, max_tasks=64, max_depth=8, plan_timeout_s=600 угаданы :601-615;
  - max_foreach_children=32 :622; max_task_budget_share=1.0 :629; max_loop_iter=8 :636;
  - as_of=None и user_input_ids=[] :648-649; max_proposals=3 :656.
- AttemptStarted: share_renorm=False :507; **tainted_context=False :812 (касается безопасности)**; attempt_role="agent" :844.
- StudyFinalized: hypothesis_report :724.

Точно выводимые значения, оставить: simulated→stub (:751-769), reason (:729-748, raise), удаление role (:118-141), count из questions (:780-787), структурные None/[] со смыслом «поля не было».

## Литералы версий: 59 мест
- schema_version=3: executor.py:1561.
- schema_version=2:
  - executor.py:1570, 1592;
  - attempts.py:501, 530, 596, 617;
  - machine.py:517, 535;
  - runtime_factory.py:158;
  - proposed_check.py:625, 641;
  - plan_validate.py:268;
  - lifecycle.py:685;
  - expand_graph.py:289, 304;
  - cancel.py:72, 90.
- schema_version=1: ещё около 41 места в 14 файлах.

Заменить все на константы.

## Остальной core (файл:строка — что угадано → чем заменить)

### Классификация по строкам
- executor.py:777 — транзиентность по подстроке detail → типизированный флаг в исключении.
- executor.py:373 — startswith("unapproved_check") → поле exclusion_reason.
- rubric.py:216 — "timeout" in detail.lower() → result_class или timeout_kind (уже проверяются на :211-213).
- artifacts.py:24-42 и verdict.py:100-115 — роль артефакта угадывается по media_type → обязательный OutputSpec.role.
- foreach.py:342-351 — псевдонимы route; неизвестный route превращается в EXPERIMENT → enum TaskKind, иначе raise.
- expand_validate.py:21-29, :18, :35 и expand_trigger.py:190, 285, 389, local_project.py:329 — тип задачи по префиксу имени PLAN- → task.kind.
- domain.py:151 — псевдонимы gate → один канонический ключ.
- claims.py:307, 341, 361, 365, 942, 966 — тип цели по префиксу строки → target_kind.

### Вывод значения из формы данных
- executor.py:87-92 и 114-117 — единственный check или критерий привязывается без checked_against → требовать checked_against.
- input_limits.py:297-302 — подсчёт страниц PDF допустим только как лимит безопасности.

### Тихая подмена enum
- claims.py:144-147; verdict.py:969-972, 1169-1172 — значение по умолчанию "inconclusive" → raise.
- executor.py:109-112 — "unassessed" → raise.
- executor.py:121-125 и inputs.py:379-382 — "unknown" → raise.
- attempts.py:576-579 — значение по умолчанию CANCELLED → полная таблица, иначе raise.
- verdict.py:1296 → полная карта, иначе raise.
- verdict.py:646 и artifacts.py:354 — producer по умолчанию "agent" → обязательное поле.
- expand_validate.py:198, 268; rules.py:208; dispositions.py:259 — final_type по умолчанию "verdict" → обязательное поле.

### Доменные значения по умолчанию через .get и or
- **provenance по умолчанию "real"** → обязательное поле. Места:
  - quorum.py:111, 164;
  - inputs.py:100, 154, 206;
  - machine.py:147;
  - local_project.py:431;
  - attempts.py:555;
  - minimal_report.py:19;
  - dispositions.py:668.
- charged по умолчанию True (attempts.py:226, 510, 512; machine.py:152; executor.py:1421) → обязательное поле.
- inputs.py:101 и 371-378, contradicts.py:43 → строгая схема derived attrs.
- `X or 8/64/32/0.0` для полей Study (expand_validate.py:217, 281; lifecycle.py:428, 450; foreach.py:226, 259; shares.py:195) → прямой доступ к полю. Через `or` ноль молча превращается в 8.
- domain.py:157 — gate timeout 1800 → константа с документированным источником.
- attempts.py:31 — RETRY_BACKOFF_S=0.05 → указать источник.

### Клампы
- verdict.py:1049-1050 — max(1, ...) и max(2, ...) → валидировать при создании Study.
- quorum.py:89, 382 — max(1, int(q)) → валидировать в InputSpec.
- max(0.0, …) для остатка времени и бюджета допустим.

### Тихий fallback
- foreach.py:97 — KeyError даёт [] → raise.
- executor.py:470-473 — except Exception вокруг getattr.

### Допустимо (лимиты безопасности)
- archive_limits.py целиком;
- input_limits DEFAULT_MAX_*;
- MAX_FINALIZED_PAYLOAD_BYTES, MAX_BYTES, *_MAX, _MAX_PROVENANCE_DEPTH;
- обрезки текста в отчётах.

## Почему пропустила
Я смотрела только diff и мерила тесты по критерию «тест действительно утверждает результат», а правила «без эвристик» у меня не было. Апкастеры добавлялись по одному-два setdefault на PR, и в каждом диффе это выглядело разумно. Дубли и литералы видны только при обзоре всего модуля. Это моя ошибка. Теперь смотрю весь затронутый модуль и проверяю по правилу ниже.

## Пункты для REVIEW_CHECKLIST (grep-able)
1. `rg -n 'setdefault\(' coscientist/core` — каждое вхождение либо точный вывод, либо None со смыслом «поля не было».
2. `rg -n 'schema_version\s*=\s*\d' coscientist/core` — должно быть 0 вхождений.
3. `rg -n '\.get\([^,]+,\s*("|\x27|[0-9]|True|False)' coscientist/core` — на доменных данных запрещено.
4. `rg -n '\bor\s+([0-9]|"|\x27|True|False)' coscientist/core`, а также getattr со значением по умолчанию.
5. `rg -n '\b(max|min)\(\s*[0-9]' coscientist/core` — клампы.
6. `rg -n '" in (detail|reason|mt|route)|\.(startswith|endswith)\(|\.lower\(\)' coscientist/core` — кроме archive_limits и input_limits.
7. `rg -n -A2 'except (KeyError|ValueError)' coscientist/core | rg 'return|= "'` — тихое приведение enum.
8. `rg -n '\}\.get\([^,]+,\s*\w+\.' coscientist/core` — карта с fallback на член enum.
9. `rg -n 'len\((checks|criteria)\) == 1' coscientist/core` — вывод из формы данных.
10. `rg -n 're\.compile' coscientist/core` — разбор ключей и имён.
11. Каждая новая версия события требует в PR обоснования, что значение выводится точно. Иначе нужна миграция.
12. Ревьюить весь затронутый модуль, а не только diff.
