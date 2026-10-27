# Study Console

Local profiles organize studies by nickname, as in the legacy local registry.
They are not accounts or authentication: anyone who can use this interface can
open all journals. The selector remembers a browser's choice. “Все исследования”
clears that choice and shows the original full list; a new launch then has no
local profile. Execution presets (“Параметры исследования”) are configured by the operator on the server.

Create profiles explicitly; nicknames are normalized to NFC, whitespace is
collapsed, and names are unique ignoring case. Profiles and study associations
persist in `DATA_ROOT/console-profiles.sqlite3`. Reading a missing catalog creates
nothing. Existing journals have no inferred profile. Open a journal, select a
profile and use “Добавить открытое исследование” to attach it explicitly. Repeated
attachment to the same profile is idempotent; reassignment to another is refused.
Replan children inherit only through the actual recorded root/active binding.

`GET/POST /console/profiles` list/create profiles; `GET/POST
/console/profiles/{id}/studies` list/attach existing journals. `POST /console/runs`
optionally accepts `profile_id`; it never replaces the run owner or human principal.
The selected profile is pinned in the launch digest and durable reservation before
dispatch. SQLite association and reservation JSON are separate stores: there is no
cross-store atomic transaction. Matching replay repairs only missing association
metadata from the recorded reservation; it never automatically restarts research.
An uncertain browser request retains its original profile and idempotency key even
after selecting another profile. Starting a different profile request is explicit.
Catalog failure affects profile operations, not existing Stop/HITL/run controls.

The API serves a local research console at `/ui/`. Start it with
`python -m coscientist.api.serve --data-root PATH --catalog CATALOG --host 127.0.0.1 --port 8455 --stream-poll-interval-s 1.0 --run-wait-s 60`.
The launcher needs FastAPI, uvicorn and a WebSocket backend. It uses the same journals
as CLI and API runs; it never starts a second runner.

Studies include CLI IDs and API IDs. A resumable WebSocket streams journal deltas;
the browser folds tasks, artifacts and graph nodes. Graph columns group node kinds,
not causal depth: research links may be cyclic or arrive after their nodes. Selecting
a node shows its attributes and directed links. Fit follows new data and container
resize until the user pans/zooms; the Fit button resumes automatic fitting.

Artifact identity comes from journaled `ArtifactRegistered` or artifact-kind
`GraphNodeUpserted` descriptors. Display names are metadata, never filesystem paths.
CAS requests validate containment and SHA-256 before returning bytes. Scientific
reports without graph nodes and CLI evidence without registration events both work.
Text/Markdown is rendered without executing HTML; images preview via blob URLs,
and all formats retain an original download. The document metadata states when its
source stamp is unavailable rather than inventing provenance.

Interactive API runs expose a human plan approval gate before `PlanAccepted`.
The human sees the exact recorded candidate snapshot, SHA-256, PLAN attempt,
question and choices in the Decisions tab. Accept/reject commands carry the opening
sequence and snapshot digest. The single lease-owning run consumes the inbox and
calls the existing HITL decision implementation; the HTTP handler opens no writer.
Acknowledgements identify the durable `HitlDecided` event. Reject and timeout stop
the run before plan acceptance; timeout is recorded explicitly and never delegates
human approval. Reloads/reconnects reconstruct pending/history from the journal.

Answers are available only while the interactive API owner is waiting. Historical
and CLI studies remain viewable; an absent owner is explicit and buttons are disabled.
The inbox is intentionally process local: server restart loses the command transport,
not recorded decisions, and does not silently create a replacement runner. Starting
research still uses the existing explicit `POST /studies/{id}/run` contract.
The runs page can launch research when the server starts with `--run-presets PATH`.
The JSON file contains `presets`, each with `id`, `label`, `description`, and `config`.
`docs/console-presets.example.json` shows the complete shape with explicit GLM
settings and server file paths to replace before use; it is never loaded automatically.
`config` is the existing explicit run settings contract with `task_text` excluded;
all other fields remain required. Use `hitl_mode: "interactive"`. Config and input
paths resolve relative to the preset file; their bytes are pinned at server startup.
Changing a referenced file requires restarting the server before a new launch.
The server validates/pins presets at startup and launches research only on human
submission. Without the flag, existing journal viewing remains available.

The browser sends only the chosen preset ID, exact human task, and idempotency key.
It displays server-provided models, deadline and maximum LLM/token/tool budgets.
Preset metadata contains no server paths, provider configuration or credentials.
Same-key retries retain the task and full pinned configuration identity; changing
either produces a conflict. The shared durable intake reservation code prevents a
second launch. Closing/reloading a browser does not cancel an acknowledged run;
an unacknowledged draft retains its key in that tab for explicit retry.

Run controls show the actual owner state and follow replan journals. Status-only
polling uses the configured server interval while an owner is running; events and
graphs continue to use the resumable journal stream. Stop interrupts the original
owner before the existing journal cancellation path closes its actual active
journal, including a child created by replanning before the approval gate opens.
An atomic server-side binding retains root run identity and the active journal;
this is transport metadata, not scientific evidence. Direct client cancellation
of a child journal remains disallowed. A server restart displays
recorded terminal results or `owner_unavailable`; it never starts a replacement run.
Public failures are typed categories, with full configuration diagnostics in server
logs. User management, CLI interactive transport and scientific-answer acceptance
remain separate capabilities. A green execution verdict shown here is the recorded
historical verdict, not a new audit.

Checks: `uv run --extra dev --extra mcp pytest tests/api/test_console.py -q`,
`node --test tests/web/model.test.mjs`; full repository gates still apply.

Текст ещё не отправленной задачи и выбранные параметры сохраняются в памяти вкладки при смене или создании локального профиля. После отправки неподтверждённый запрос сохраняет исходные текст, параметры, профиль и ключ; поля заблокированы для точного повтора. Кнопка «Новый запрос для выбранного локального профиля» явно создаёт новый запрос, не останавливая предыдущий. Перезагрузка страницы сохраняет только уже отправленный неподтверждённый запрос.

## Навигация и источники представлений

`#/new` открывает только создание исследования, `#/runs` — каталог исследований. Это разные страницы; смена локального профиля сохраняет ещё не отправленный текст и параметры в памяти вкладки. В группе «Работа» доступны оркестратор, принятый план, научный граф, результаты, документы и решения человека. «Наблюдение» содержит вызовы инструментов, трассировку сессии и материалы литературы; в материалах исследования видно фактическое использование MCP. Представления конкретного исследования доступны после открытия его журнала. Ссылки `#/study/<id>/<tab>` сохраняют выбранный раздел.

Вызовы инструментов объединяют реальные `ToolCalled`, `McpRequestRecorded` и `McpResponseRecorded` по `call_id`. Незавершённая запись MCP не объявляется ошибкой или признаком активного исполнителя. Панель литературы считает только результаты и артефакты зарегистрированного этапа LIT, а не статьи или научно подтверждённые источники. Код открывается по CAS-идентичности существующих артефактов; интерфейс не получает произвольный доступ к файловой системе. Специальная статистика FEDOT не показывается без соответствующих данных.

Неприоритетные UX-заметки для следующего прохода: заголовок нового исследования повторяется в H1 страницы и H2 формы; раскрытие полной LIT-записи пока не сохраняется при изменении самого набора LIT-результатов; для узких экранов длинные технические JSON лучше просматривать в отдельной панели документа. Эти правки не блокируют запуск, профили и просмотр исследования.
