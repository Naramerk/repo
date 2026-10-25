# Возможности CoScientist-next

Копия списка возможностей с карты CoScientist (Page «Карта CoScientist: что нужно, что наслоение», 2026-10-23).
«обяз.» — обязательная возможность (требование пользователя). Ссылки на старый код — репо
`CoScientist_dev-dev_1-2026-10-22/CoScientist`, только как эталон поведения. Веха — из docs/DESIGN.md §13.
Измеримые критерии: #2, #3, #8, #19. Для остальных численные пороги уточняются при работе над вехой.

| # | Возможность | Критерий приёмки | Старый код | ТЗ/ТП | Веха |
|---|---|---|---|---|---|
| 1 | Приём запроса и формирование ТЗ исследования | роль tz_spec на стадии SPEC выдаёт ResearchSpec (объект, цель, границы, ограничения, метрики успеха), валидный по схеме; spec_approval по политике HITL (DESIGN §7.5) | context_init/, agents/system.yaml:190, TZSpecAgent | ТП 2.1 | M4 |
| 2 | Гипотезы (обяз.) | задача HYP-n, если plan.hypotheses.needed (решение planner с reason, профиль может обязать); без гипотез вердикт получает ответ на вопрос (RESEARCH_MODEL §4.1). Артефакт гипотез: ≥3 гипотезы, у каждой id, формулировка, ранг, проверяемое предсказание; ровно одна selected; в плане ≥1 задача ссылается на её id; при отказе HITL — статус rejected, план не строится | hypotheses/, system.yaml:500 | ТЗ п.9; ТП 1.1 | M4 |
| 3 | Исследование литературы (обяз.) | эксперимент LIT-n (method=literature_search), если plan.literature.needed: ≥5 artifact(role=paper) с DOI или URL (дедупликация по ним); у каждого утверждения ≥1 evidence с SourceQuote (quote_in_source) (0 утверждений без источника); ≥1 полный текст со статусом parsed; нескачанный источник имеет статус download_failed, а не пропадает | system.yaml:550, MCP papers_search, mcp-servers/paper-analysis-mcp-server/paper_analysis_server.py:18-32 | ТП 1.1 | M4 |
| 4 | План с критериями успеха этапов | план валиден по схеме; у каждой задачи route, инструменты, ≥1 критерий; ключи EXP-n уникальны | tools/task_tracker.py:269, research_quality/plan_criteria.py | ТЗ п.9 | M1 |
| 5 | Критика плана с циклом доработки | критик возвращает typed verdict; при revise план переделывается не более max_plan_revisions раз; работает в CLI, API и headless | web/agent_tree.py:306 | ТП 2.2, 3.2, 4.1 | M2 |
| 6 | Исполнение задачи с попытками | попытка создаётся кодом; outcome success/partial/failure вычислен из проверок; повтор, отмена, восстановление, конфликт записей — по DESIGN §3 | experiments/runtime/state_machine.py:2013, :2163; tools.py:806 | ТП 2.2 | M0–M1 |
| 7 | Проверки качества результата | на каждой записи результата ставятся CodeCheck и ReviewCheck; UNASSESSED отделён от FAILED | research_quality/plugin.py:351, reviewed-rules.json | ТЗ п.8-9 | M2 |
| <a id="cap-8"></a>8 | Подбор инструментов под этап | у каждой задачи ровно один статус: tool_found (инструмент в каталоге MCP, вызов прошёл), tool_built (alembic) или coder/tool_missing (записан запрос); на two_tasks — 2/2 tool_found; 0 вызовов инструментов вне каталога | tools/retrieval_tools.py:362, tools/servers_web_search.py:12 | ТП 2.2 | M1 |
| 9 | Alembic: инструмент, когда нет MCP-сервера (обяз.) | при отсутствии MCP-инструмента (фикстура с пустым MCP-каталогом): repo_search → build → validate → register; инструмент вызван в задаче (сценарий alembic_nomcp) | experiments.yaml:110 → bindings.py:932 → alembic_tools.py:50 | ТП 2.3, 3.2, 4.1 | M3 |
| 10 | Поиск репозиториев | для недостающего инструмента возвращается список репозиториев-кандидатов | experiments/capabilities/repo_searcher.py | ТП 1.1, 3.2 | M3 |
| 11 | Coder: исполнение кода (обяз.) | код запускается в sandbox (local или HTTP) как job с deadline; артефакты регистрируются с sha256; по deadline — failure(retryable) | tools/coder_tools/coder_tools.py:289, sandbox_tools.py:403 | ТЗ п.11 | M3 |
| 12 | Медицинские исследования (обяз.) | профиль medicine: маршрут medical выполняет задачу с med_tools | system.yaml:753, tools/med_tools.py | — | M4 |
| 13 | Микрофлюидика (обяз.) | профиль microfluidics стартует (корень study создан ядром) и выполняет задачу эталона | microfluidics/schema.py:63-72, microfluidics.yaml:47 | — | M4 |
| 14 | Граф исследования и провенанс | результаты, доказательства и ссылки читаются по id из проекции журнала | graph/research/agent_tools.py:459, store.py | ТП 2.4 | M1 |
| 15 | Граф исполнения / аудит записей | по продуктовому решению (DESIGN §14) | graph/research/record_audit.py, graph/service.py | — | — |
| 16 | Отчёт | JSON, MD, HTML со ссылками на artifact_id; отчёт о НИР | tools/result_formatter_tool.py:119, reporting/nir/ | ТЗ п.9, п.17; ТП 2.4 | M5 |
| 17 | Артефакты в S3 и presigned URL | артефакты в объектном хранилище; API отдаёт presigned URL | reporting/s3_upload.py:56 | ТП 2.4, 3.1 | M5 |
| 18 | HITL | plan_approval, result_acceptance, skip_task как состояние с deadline и политикой; headless никогда не зависает | hitl/tool.py:103 | ТП 2.2 | M2 |
| 19 | Веб-интерфейс (обяз.) | на эталонной сессии: граф показывает все узлы плана (число = числу задач); статус каждого узла совпадает с проекцией ядра (0 расхождений); прогресс X из N = числу завершённых задач; каждый зарегистрированный артефакт открывается (HTTP 200); HITL-запрос виден и решение доходит до прогона; отчёт открывается | web/app.py:4725, static/js | демо | M6 |
| 20 | Метамодель на узлах | у узла видны NodeRequirement: статус, источник, доказательства | web/research_metamodel.py:80-128 | демо | M2/M6 |
| 21 | Интеграция с внешними МАС (A2A, MCP) | опционально (DESIGN §6.5): A2A-фасад создаёт study и отдаёт поток событий; удалённый агент вызывается как Tool; модули публикуются как MCP-инструменты | a2a/server.py, hitl/a2a_tools.py:58 | ТЗ п.10, п.14 | M5 |
| 22 | FEDOT.MAS | опционально (DESIGN §6.5): Tool fedotmas.run_pipeline включён в профиле, задача выполнена, результат — артефакт | tools/fedotmas_tools.py:50 | ТП 3.2, 4.2 | M5 |
| 23 | Модели через OpenAI-совместимый API (AITunnel) | модель на роль, резервная модель; usage на каждый вызов | config/settings.py | ТЗ п.9-10; ТП 4.1 | M1 |
| 24 | Управление прогоном | отмена study и задачи, восстановление, Budget (вызовы, токены, ₽, время) на study/попытку/роль; превышение → BudgetExceeded (DESIGN §7.4) | web/run_control.py, execution_control/ | — | M0–M3 |
| 25 | Сценарий «Запрос заказчика» | по решению владельцев (DESIGN §14) | config/tool_catalog.yaml:623 | — | — |
| 26 | Учёт токенов и стоимости (новое) | токены и ₽ по study, задаче, роли, модели; трассировка вызовов инструментов; при неизвестной цене — cost_unverified, не pass | нет в старом коде | — | M1 |
| 27 | Versioned agent prompts | exact `(prompt_id, version)`; no inline prompts at call sites | — | — | M2 |

<a id="cap-27"></a>
**#27 owner:** agent layer (`coscientist/agents/prompts`).

## Пробелы старой системы, которые закрывает новая
- Метрики ТЗ (время до плана, доля успешных прогонов, доля валидных артефактов) — RunReport.
- Критик плана в CLI — #5.
- Известные ошибки B1, B2, R1–R5 — тесты и сценарии в DESIGN §12.

## Вне рамок
- Камера лаборатории, реактор.
