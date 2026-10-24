# Changelog

## Unreleased

### C1 coder job + `run_sandbox` [M3]
- `run_coder_job`: spawn child + `run_sandbox(on_guest_started=…)`; deadline `killpg` guest group; `jobs.wait` (poll from `SandboxLimits`); unpublished pgid → fail-closed `JobCancelFailed`; `attempt_is_closed()` at put-time; `finally` forces terminal job; `sandbox_status_to_coder` mapping tested.
- Gorilla @75eb157: kill-after-timeout, no poll literal, no stuck `running`, live attempt-closed check, status mapping tests.
- Gate @3c15ccd: drop non-test `# type: ignore[return-value]` — pass `SandboxResult` isolation Literals through to `put_sandbox`.
- Gorilla @2d80a80: `killpg` guest group (not worker-only); narrow worker `except Exception` → typed `ToolError`; single `spawn` context (no fork/spawn fallback); real-guest kill test.
- Stacks on G6. Parity: [DESIGN §6.4](docs/DESIGN.md#64-coder-и-alembic-как-асинхронные-задачи-r4), [CAP #11](docs/CAPABILITIES.md), §12.2 `coder_timeout`.

### G6 JobStore + Job* events [M3]
- `coscientist.tools.jobs`: `Job` / `JobStore` Protocol + journaled `InMemoryJobStore` (DESIGN §6.4).
- FakeClock `on_change` + unsubscribe; JobStore takes explicit `on_clock_change` (core `Clock` stays `now()`-only); `close()` unsubscribes.
- Job payloads / `JobEventType` (nested into `EventType`) / `require_aware_datetime` / `require_nonblank` in `core.events.job_events`.
- Contract: idempotent submit by `(attempt_id, spec_digest)`; one live job per attempt → `ToolError(local)`; deadline → `JobTimedOut` + `cancel` → `JobCancelled` (or `JobCancelFailed`); `wait` does not revive `timed_out`.
- No `core/ports.py` change; coder/alembic backends and jobs projection fold = follow-ups (C1 / G19).
- Parity: [DESIGN §6.4](docs/DESIGN.md#64-coder-и-alembic-как-асинхронные-задачи-r4), [CAP #11](docs/CAPABILITIES.md), issue [#130](https://github.com/Naramerk/repo/issues/130).

### MCP live servers (literature) [M1]
- `mcp_live`: `literature_live_bundle` → `McpToolRouterBundle` (`StreamableHttpServerConfig` + `McpServerRoute`; `McpClientConfig.model_validate` rebuild); `open_tool_router` (explicit `file_fetcher`/`http_transport`; open-fail cleanup owned by `ToolRouter.open`); tool names cited from CoResearchGraph / CAP #3; `mcp-servers/docker-compose.yml` (`COSCIENTIST_DEV_ROOT`, legacy container port 7331). [DESIGN §13](docs/DESIGN.md#13-вехи), [§12.2](docs/DESIGN.md#122-сценарии-и-пороги), [CAP #3/#8](docs/CAPABILITIES.md).

### ToolRouter(MCP) [M1]
- `coscientist.tools.router.ToolRouter`: explicit `ToolRouterConfig` / `McpServerRoute` (no discovery); routes `(server, tool)` calls through `mcp_adapter` → `McpClient`; fail-closed on unknown server/tool; `ToolRouterCloseError` with required `causes`. [DESIGN §6.3](docs/DESIGN.md#63-маршрутизация-и-alembic-fallback), [CAP #8](docs/CAPABILITIES.md?plain=1#L17).

### M5 API — cancel + FastAPI/WS
- `post_study_cancel`, `create_app` (GET view/events, POST cancel, WS stream); extra `api`.
- [DESIGN §9](docs/DESIGN.md#9-api-для-веба).
- [CAP #19](docs/CAPABILITIES.md?plain=1#L28) / [#21](docs/CAPABILITIES.md?plain=1#L30).
- Required stream `sleep`/`is_disconnected`; WS receive disconnect; finite poll.
- Shared `open_study_runtime`; `bind_cancel_study`; `parse_after_query`; `stream_sleep`.
- WS close reason byte limit (`WS_CLOSE_REASON_MAX_BYTES`); typed stream/cancel params.

### M5 API — DataRootStudyService loop gate + contract tests
- `_has_running_loop()` then `asyncio.run` (no work inside except); `study_events_db` → `(sid, path)`.
- Generators without unreachable self-checks; contract tests one scenario each + typed EventStore fake.

### M5 API — cayman closeout (StudyNotFound path + loop gate)
- `StudyNotFound` only from `core.events.errors` (no api/ports re-export); reason required.
- Loop gate: public `get_running_loop` / `asyncio.run` in except; `STUDY_NOT_FOUND_REASON` constant.
- Contract tests: `match=` + parametrize `ids`; idem key import from core.

### M5 API — study_view under running loop → StudyViewLoopRefuse
- Explicit loop presence gate before `asyncio.run`; typed refuse + test.

### M5 API — study_id format owned by core/events/ids
- `STUDY_ID_RE` / `is_study_id` / `study_id_from_idempotency_key` next to `study_id()`.
- `require_study_id` uses core predicate; path resolve kept in `study_events_db`.

### M5 API — study_id path traversal gate
- `require_study_id` + `study_events_db` resolve-inside-root (format from core).
- `DataRootStudyService` refuses `../x`, `/abs`, `a/b`; no mkdir for missing journals.

### M5 API — GET study view + events contract
- `get_study` / `list_study_events(study_id=)`; store/wire `study_id` match or
  `StudyStoreMismatch`. `EventWire` via `model_validate` (`seq` ge=1).
- Core `StudyNotFound` on `StudyService` port; `DataRootStudyService` real adapter.
- Contract tests (fakes + data-root); payload copy locked.
- [CAP #19](docs/CAPABILITIES.md?plain=1#L28) / [#21](docs/CAPABILITIES.md?plain=1#L30) prep.

### [M2] continue_session §3.6 partition
- `partition_fork` / `fork()` + `last_fork`; durable empty-nonce idempotency via
  `fork_child_id` → `study_id_from_idempotency_key` (`STUDY_ID_RE`).
  Child journal / `TaskInherited` later. [DESIGN §3.6](docs/DESIGN.md#36-продолжение-сессии-fork).

### [M2] Critic + reviewer typed outs + builtin prompts
- `CriticVerdict` / `CriticIssue` (`coscientist.agents.critic`) and `ReviewVerdict` /
  `ReviewEvidenceRef` (`coscientist.agents.reviewer`): frozen strict models; revise/reject
  require issues; `pass` requires non-empty evidence. Types only — no Protocol / adapter.
- Shared `AgentModel` + `NONBLANK` in `coscientist.agents.strict`; ResearchSpec /
  CriticVerdict / ReviewVerdict inherit it (no per-role base copies).
- Built-in prompts via `prompts.builtin` (import directly; no `__getattr__` re-export);
  template `prompt_id`/`version` from `*_PROMPT_REF`; refs via
  `builtin_role_prompt_registry().refs()`.
- `ReviewEvidenceRef.json_pointer`: required `str` (DESIGN §5.1; `""` = whole artifact);
  adapter passes through to domain `EvidenceRef` as-is.
- Parity: [DESIGN §7.1](docs/DESIGN.md#71-роли-тонкие-агенты-typed-io) /
  [§5.1](docs/DESIGN.md#51-типы); [CAP #5](docs/CAPABILITIES.md) / [#27](docs/CAPABILITIES.md#cap-27).

### Benchmark harness — runner/Usage + usage.jsonl/result.json (plan item 4) [M1]
- Runner/suite models: `UsageRecorded` (DESIGN §10 payload; harness accounting
  shape — journal event owned by scheduler S1 / #129), `QualityObservation`,
  `RunnerSuccess`/`RunnerRefuse`, `TimedRunOk`/`TimedRunRefused`,
  `TaskComparison`, `BenchSuiteResult`. Tokens via `coscientist.llm.types.Usage`;
  LLM calls = `len(usage_recorded)` (`llm_call_count` / `token_total`) — **no**
  harness `CallCounts`.
- `bench/write_artifacts.py`: per-run `usage.jsonl` + `result.json` (HER fields:
  status, dur_s, llm_calls, tokens, exp_tasks, evidence, record_result).
  [plan item 4](docs/plan/benchmark-harness.md); [DESIGN §10](docs/DESIGN.md#10-наблюдаемость)
  / [§12](docs/DESIGN.md#12-паритет-с-эталоном).
- Review fix: `TaskComparison` requires matching task ids; writers cover
  parent-not-dir; `assert_never` on unknown run (no unreachable-branch test);
  `ruff format` clean.
- Alligator fix: shared `QualityBase` → `QualityExpectation` /
  `QualityObservation(+record_result)` (correct class in unique-evidence errors);
  drop unused `RunnerRawResult`; tests via `model_validate`/`setattr` (no
  `# type: ignore`); reject cases parametrized with `ids` + exact `match=`
  (no `if "unique" in match` heuristics).
- Crocodile fix: drop tautological `hasattr`/`"estimated" not in` asserts;
  usage.jsonl objects compared to independent expected dicts; split empty
  `token_total` / `llm_call_count` cases into separate tests.

### Benchmark harness — task + quality models (plan item 3) [M1]
- Add `bench/models.py`: `EvidenceOutcomeCount`, `QualityExpectation` (status,
  exp_tasks, evidence tallies — exact-compare fields only), `BenchTask`,
  `TaskSetManifest`. Required fields only; frozen; `extra=forbid`; no scores /
  weights / CallCounts. [plan item 3](docs/plan/benchmark-harness.md);
  parity: [DESIGN §12](docs/DESIGN.md#12-паритет-с-эталоном),
  [CAPABILITIES](docs/CAPABILITIES.md) (B1/B2/R1–R5 / §12).
- Review fix: `TaskSetManifest.tasks` refuse absolute paths and ``..`` segments;
  `ruff format` clean under `ruff.toml`.
- Alligator fix: tests use `model_validate` / `setattr` (no `# type: ignore`);
  reject cases parametrized with `ids` + `match=`.
- Alligator fix: drop unused `_quality()`; `field_name` before `with` in freeze test.

### R16a.1 — reporting formats json/md/html [M5]
- Package `coscientist/reporting/`: `ReportRenderInput(report, report_artifact_id, tasks)`; `render_report_body` → body str; media via `media_type_for(format)` only; shared `artifact_href` for MD+HTML; HTML wrap `(aid, href)|None` + required `html_lang`; `ReportView` strict pydantic; `parse_report_format` for transport. Verdict/attempt provenance via shared `parse_provenance` (inputs + machine; no invent `real`). [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [DESIGN §9/§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a.1` / [#118](https://github.com/Naramerk/repo/issues/118).

### R16a — typed `MinimalReport` [M5]
- Pydantic `MinimalReport` (+ task/question/sections rows, `extra=forbid`); build/truncate/assemble return the model; `render_markdown_report(MinimalReport)`; StudyFinalized via `model_dump(mode="json")`. Rows use SoT `Disposition` / `TaskOutcome` / `Provenance` (no duplicate Literals). Assemble refuses missing question fields, hypothesis key≠id, DONE without outcome, missing/unknown closed-attempt provenance (no invent `real`); DONE without closed attempt uses explicit `provenance=null`; MD Tasks only from `report.tasks`; question `note` preserved; verdict disposition `reason` = verdict; AttemptStarted v10 upcaster refuses absent/null/unknown provenance (no invent); legacy `reclassified_fetch_old` fail-closed on open (`match=AttemptStarted.v10:provenance_required`); reclassify open/finalize seeds an authored journal that already carries provenance (never remap legacy → `real`); finalize reads public `StudyRuntime.observations`; shared `parse_provenance`. [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [DESIGN §9–§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a` / [#117](https://github.com/Naramerk/repo/issues/117).

### Graph schema parse (G7 split 1/2) [M1] — AttrType; `GRAPH_SCHEMA_VERSION`; required attrs; explicit `null_ok`. [DESIGN §2](docs/DESIGN.md#2-доменная-модель), [CAP #14](docs/CAPABILITIES.md?plain=1#L23).

### G4 Need

- `Need` (strict, `kind` discriminator) + `NeedUnrecorded`; PlanAccepted v12: absent → `NeedUnrecorded`, present → strict DU, corrupt → `JournalFormatError`; expand/foreach/loop → `require_need` / `PlanNeedRefuse`; accept omit → `PlanNeedRefuse`. No `Study.require_hypotheses` here. [CAP #2](docs/CAPABILITIES.md); [DESIGN §2](docs/DESIGN.md#2-доменная-модель).
- `StudyRuntime.append_events`: sole durable append+project path (replaces private `_append`); tests use the same entry as production.
- `fold_loop_capped`: status is `capped` only for `iteration_cap`; any other `LoopCapped.reason` (including plan-need refuse codes) folds as `refused` (was: only `error`/`refused` → refused, else capped).

### G4 Study.require_hypotheses

- `Study.require_hypotheses` required (no construct default); StudyCreated v13→v14 refuses missing/null/non-bool (`require_hypotheses_required` / `require_hypotheses_must_be_bool`) — **legacy StudyCreated without the field cannot be replayed** (typed `CatalogError`, no invent `False`); `accept_plan` gates via `assert_profile_hypotheses_satisfied`. [CAP #2](docs/CAPABILITIES.md); [DESIGN §2](docs/DESIGN.md#2-доменная-модель).

### Docs — #16/#17 handoff ([report-s3.md](docs/handoff/report-s3.md))
- Queue issues [#117](https://github.com/Naramerk/repo/issues/117)–[#128](https://github.com/Naramerk/repo/issues/128); `nir.py` body = core `render_markdown_report`; Depends-only ordering.
- [CAPABILITIES.md](docs/CAPABILITIES.md) #16/#17; [DESIGN.md](docs/DESIGN.md) §9–§11 / §10; [DESIGN_M1.md](docs/DESIGN_M1.md) I1.

### Graph area plan (G0) [M1]
- Add `docs/plan/graph.md`: G0–G24 ownership, event→G-PR→owner table, gates for unowned ToolCalled/Mcp*/HypothesisVerdictRecorded folds; CAP #19 UI deferred to M6. Contracts first (G0 / #72 before G7 / #64). DESIGN §16 counts = G14a before G14. G13 depends on #67 G4. Follow-ups list `graph.py` sites on `main` @ `aa8be32` with owner+issue per row. Parity: [DESIGN §2](docs/DESIGN.md#2-доменная-модель) / [§16](docs/DESIGN.md#16-научная-модель-граф); [CAP #19](docs/CAPABILITIES.md?plain=1#L28).

### Graph node/edge writer (G7 split 2/2) [M1]
- `graph_node_upserted` / `graph_edge_added`: registry + pair-check; shared `attr_value_ok`; `GraphWriterError(CoreError)`; aware datetime. [DESIGN §2](docs/DESIGN.md#2-доменная-модель), [CAP #14](docs/CAPABILITIES.md?plain=1#L23).

### Quality checks (#7) — CheckAttachment surface
- Public `CheckAttachmentError` ([DESIGN §5.4](docs/DESIGN.md) / R23;
  [CAPABILITIES #7](docs/CAPABILITIES.md)). Agent view: `is_code` + `check_detail`
  (one discriminator with `CheckResult` / `CheckLike`). Plan:
  [`docs/plan/quality-hitl.md`](docs/plan/quality-hitl.md).

### M2: versioned prompt registry (`coscientist/agents/prompts`)
- In-memory registry ([DESIGN §7](docs/DESIGN.md#7-агентный-слой),
  [§11](docs/DESIGN.md#11-структура-пакетов-и-бюджет-размера),
  [CAP #27](docs/CAPABILITIES.md#cap-27)).

### Docs — research-cycle plan (CAPABILITIES #2 / M4)
- `docs/plan/research-cycle.md`: PR stack for hypotheses / question verdict (RC1–RC20,
  M4 RC12–14). Canon = `hypothesis_verdict`; D-rm **#76** hard dep of RC8; RC3t (**#98**)
  drops threshold defaults incl. `max_expansions` → `max_expansions_required`;
  `refutation_priority` only from Study; legacy missing → typed refuse. Future owners =
  tracking issues **#98–#112**. RC12 scope paragraph lives here (not in main-bound RM).
- DESIGN §2 Study: RC3t/D17 target — sole `max_expansions` (no `max_replans` second
  contract); no field defaults. `graph_schema.yaml` study field renamed to match.
  Upcasters must not invent Need.
- Parity: [CAPABILITIES #2](docs/CAPABILITIES.md#возможности-coscientist-next),
  [DESIGN §2](docs/DESIGN.md#2-доменная-модель),
  [DESIGN_HYP_DELTA](docs/DESIGN_HYP_DELTA.md), [RESEARCH_MODEL §4.1](docs/RESEARCH_MODEL.md).

### Benchmark harness — typed errors (plan item 2)
- Add `bench/errors.py`: `BenchError` / `BenchConfigError` (typed refuse; no
  silent fallbacks). import-linter root includes `bench`; forbid
  `coscientist` → `bench`. (Hatch wheel packaging of `bench` → plan item 15.)
- CI: `ruff` covers `bench` (+ `tests`); `mypy` covers `bench` via
  `[tool.mypy] files` (`coscientist`, `bench`, `web` — not `tests`; suite not
  type-clean).
- Plan: [`docs/plan/benchmark-harness.md`](docs/plan/benchmark-harness.md)
  item 2. Parity: [`docs/DESIGN.md`](docs/DESIGN.md) §12;
  [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) (B1/B2/R1–R5 / §12).

### Docs — RESEARCH_MODEL §4.1 → HYP_DELTA (`hypothesis_verdict`)
- `docs/RESEARCH_MODEL.md` §4.1: canon = `hypothesis_verdict` first-match branches (incl.
  `derivation_review_only`, `untestable_by_agent`, `refute_unresolved` inside support);
  distinct by `independence_key`. Documents main’s current threshold defaults without claiming
  they are already removed.
- `Hypothesis.verdict` Literal + status table include `untestable_by_agent`.
- `tests/refmodel.verdict` removed; `core_rules_adapter` sets `verdict=hypothesis_verdict`
  directly (no `k`→`confirmations_needed` shim).
- Parity: [DESIGN_HYP_DELTA](docs/DESIGN_HYP_DELTA.md),
  [DESIGN §2](docs/DESIGN.md#2-доменная-модель) /
  [§16](docs/DESIGN.md#16-научная-модель-граф),
  [CAPABILITIES #2](docs/CAPABILITIES.md#возможности-coscientist-next)
  (hypotheses / question verdict).

### Docs — agent-layer plan
- `docs/plan/agents.md`: AgentPort / Orchestrator ownership plan (G1 / G1b / G1-impl). Scheduler S1/S2 tracker [#129](https://github.com/Naramerk/repo/issues/129); MCP client journal owner tracked in [#78](https://github.com/Naramerk/repo/issues/78) (nothing in this plan depends on it). Parity: [DESIGN §7.2](docs/DESIGN.md#72-тонкий-llm-клиент-coscientistllm), [CAPABILITIES #5/#6/#18/#23/#24](docs/CAPABILITIES.md).

### [M4] Profiles G2 (P2): `Profile` / `RoleConfig` / `RouteConfig`
- Frozen strict models; role keys / names use `Identifier` from `mcp.config`
  (#77). `RouteConfig.budget` is core `Budget` (P3 depends on
  [N1](docs/plan/profiles-medicine-microfluidics.md#n1-budget-strict-owner-ruslan-and) — no
  wrapper).
- `mcp_servers: McpServers` (shared unique-name type from #77 — no local copy).
- import-linter: targeted ignores only; **no** `allow_indirect_imports` (#66).
- P3 YAML loader (**owner G2**): `model_validate_json` (no list→tuple shim).
  Plan: [P3 — YAML loader](docs/plan/profiles-medicine-microfluidics.md#p3--yaml-loader-owner-g2)
  (depends this PR,
  [N1](docs/plan/profiles-medicine-microfluidics.md#n1-budget-strict-owner-ruslan-and)).
  Parity: [DESIGN §8](docs/DESIGN.md#8-профили-r1) /
  [CAPABILITIES #12/#13](docs/CAPABILITIES.md).

### [M2] MCP server configs: `strict` + shared `Identifier` / `Executable`
- `StdioServerConfig` / `StreamableHttpServerConfig`: `strict=True`; `name` /
  `env_keys` / `api_key_env` via `Identifier`; `command` via `Executable`
  (inner spaces OK for paths like `Program Files`; no leading/trailing).
  Shared types in `mcp/config.py`. Parity:
  [DESIGN §6.0](docs/DESIGN.md#60-тонкий-mcp-клиент-coscientistmcp-m2-prep--a7) /
  [CAPABILITIES #8/#21](docs/CAPABILITIES.md).
- `McpServers` = unique-name `tuple[McpServerConfig, ...]` (single uniqueness
  owner for `McpClientConfig.servers`).
- Tests: list `args` refused; Identifier empty vs whitespace messages split;
  Executable empty / leading / trailing; inner spaces OK; `api_key_env` int /
  empty / `"MY KEY"`; URL whitespace.
- **Follow-up:** YAML profile loader (**P3**, owner **G2**):
  `model_validate_json` (no list→tuple shim). Plan:
  [P3 — YAML loader](docs/plan/profiles-medicine-microfluidics.md#p3--yaml-loader-owner-g2)
  (depends [#62](https://github.com/Naramerk/repo/pull/62),
  [N1](docs/plan/profiles-medicine-microfluidics.md#n1-budget-strict-owner-ruslan-and)).

### Docs — capability #21 A2A/MCP handoff
- `docs/handoff/a2a.md`: small-PR plan for external MAS (A2A facade, remote Tool, MCP module server; DESIGN §6.5 / CAPABILITIES #21). Reuses `StudyService` / `post_studies` / `EventStore.read_after`; `provider=http` depends on tool-gap catalog; MCP modules via `AgentPort` typed outs; real HTTP in `mcp/httpx_transport`; CI green in DoD. Notes **#63** / **#54** merged on `main` (`1a1ea7d`).

### M2 prep (A7 follow-up): MCP → ``invoke_tool`` adapter
- `coscientist.tools.mcp_adapter`: bind MCP session tools as `Tool` for `invoke_tool` (`provider=mcp`).
- Tools keyed by `(server_name, tool)`; `ToolSpec.name` = `server:tool` (no cross-server collisions).
- Per-tool `McpToolProfile` supplies `effect` / `is_fetch` / `idempotent` / `max_retries` (required; no defaults).
- `ToolSpec.timeout_s` = `McpSession.call_tool_timeout_s` only (same as `McpClientConfig.call_tool_timeout_s`).
- import-linter: `tools` still forbidden from `mcp`; only `tools.mcp_adapter -> coscientist.mcp.*` ignored.
- DESIGN §6.0 / CAPABILITIES #8, #21.
- **Follow-up S8:** `CallCtx` has no deadline; add `effective_deadline` on the tool call path (edge → tool-gap / [#46](https://github.com/Naramerk/repo/pull/46) catalog+`ToolResolved` track). Not in this PR.

### M2 prep (A7): thin MCP client (`coscientist/mcp/`)
- Standalone MCP client (no `coscientist.core`). **Own JSON-RPC client** — DESIGN §6.0 vs official `mcp` SDK **2.x** (server-stack deps; SDK has custom httpx client factory; consider `mcp-types` later).
- Optional extra `mcp` (`httpx`); CI installs `.[mcp]`. import-linter: `llm` ↛ `httpx`; **httpx only allowed in `coscientist.mcp`** (forbidden from `core`/`checks`/`experimental`/`llm`/`tools`/`agents`/`api`/`schema`/`web`).
- Profile `stdio` + `streamable_http`; all timeouts/limits/statuses/encodings required. Pagination page/cursor limits; HTTP per-request `wait_for` deadline (SSE keepalives cannot extend); DELETE session on aclose.
- `HttpxTransport` (shared `AsyncClient`, `ssl.SSLContext` verify): TCP pin + Host/SNI/cert on original host. SSRF: `not is_global` after unwrap mapped/6to4/NAT64/`::/96`; multi-member gzip.
- File decode: limited zlib/gzip (`decompress(..., max_length)` loop, no unbounded `flush`); truncated / trailing-garbage streams → `McpProtocolError`. Cancel-notify failures logged; original `McpTimeoutError` preserved. SSE lone trailing `\r` completes the line; JSON-RPC requires `id` or `method`.
- Stdio: stderr keep-drain; public pipe close only (no `proc._transport`); mid-line EOF → transport error.
- Tests: in-process fake + adversarial SSRF/stdio + real-socket pin/redirect/rebinding/SNI mismatch + deflate-bomb RSS + overall deadline across redirect.

### M2 PR A1 — thin LLM client
- New package `coscientist/llm/`: typed `LlmRequest`/`LlmResponse`/`Usage`, `ProviderAdapter` + OpenAI-compatible provider, `LlmClient` with timeout/retry/limits from required `LlmConfig` fields (no guessed defaults), typed errors, API key only from env named in config; no logging/printing of secrets or bodies.
- Transport is injected (`Transport` protocol); unit tests use a fake transport (no network). Required injections: `clock`, `sleep`, `wait_for`, `rng` (no unseeded default).
- Retry: exponential backoff + jitter (`retry_backoff_max_s` caps backoff only); `Retry-After` parsed only when `honor_retry_after` and status is retryable; OWS = SP/HTAB only; delta-seconds = ASCII `1*DIGIT` (int→float; OverflowError/ValueError → `LlmRetryAfterError`); HTTP-date = strict IMF-fixdate (`GMT` exact; leap second/`gmt`/day-name mismatch rejected; RFC 850/asctime rejected; past → delay 0 per RFC 9110); invalid → `LlmRetryAfterError` (never masks `LlmProviderError`); not clamped to backoff max — only `total_deadline_s`; `attempt_timeout` to transport; HTTP success = 200 only; naive/unknown-`utcoffset` clock → `LlmClockError`.
- import-linter: `llm` ↛ `core`/`tools`/`agents`/`api`/`checks`/`experimental`/`google`/`httpx`; `tools` ↛ `agents`/`llm`; layers `agents` above `tools | llm`.
- `docs/DESIGN.md` §7 rewritten: no ADK; thin client; all agent state in the event journal.

### Core layout
- Move flat `coscientist/core/*.py` into subpackages `events/`, `plan/`, `claims/`, `execution/`, `finalize/` (`git mv`); update imports, import-linter contracts, and tests. No logic changes.

### Store (StudyFinalized once)
- After a study is finalized, a second distinct `StudyFinalized` (different idem_key) is rejected with `StudyClosedError` (I3 / exactly one finalize). For **any** event type, identical idem_key+payload retry is checked **before** StudyClosedError so honest retries return the original.
- Same-batch guard: `append_many([SF, …])` probes finalized **after** `_fence()` and treats the batch as closed after the first `StudyFinalized` (in-batch flag + never cache a False finalized probe). `BEGIN IMMEDIATE` on append is **defensive**; peer writers are serialized by the lease `UPDATE` inside `_fence`.
- **Legacy journals** with two `StudyFinalized` rows: fold/`apply_event` / local project are **first-wins** for `studies.finalized_payload` / graph; the second SF taints `meta` (`duplicate_study_finalized` + `tainted`) and local `_duplicate_study_finalized`. That taint does **not** enter the StudyFinalized report/`contributing_reasons` (`finalize()` returns early when already finalized; wiring it through `view()`/report re-render was out of scope). New writes still refuse another SF.

### M1 PR16 follow-up
- Finalize LeaseLost reraise coverage for `_primary_report_path` / `render_markdown_report` / `build_minimal_report`.
- `dispositions()` order on main `7217a95` (#28+#31+#33): skip_cap/scoped_capped → `apply_loop_cap_dispositions` → `_stamp_honest_terminals` → `apply_rollup`. Skip cap only for decomposes parents with **no own covering tasks** (plans + ``runtime.tasks`` addresses — foreach/expand/loop). Combined cover **excludes** ``superseded`` / ``cancelled`` (fold ``TaskDone.reason``) so dead tasks cannot falsely keep a parent under loop-cap.
- Combined cover (plans ∪ runtime) can change ``not_attempted`` reason for questions that previously had ``no_task`` (e.g. runtime-only covering → ``tasks_skipped`` / ``tasks_failed``; cancelled-only covering → empty cover → ``no_task`` again).
- Equal-rank rollup keeps own without false ``rollup_clamped``; lowering preserves ``iteration_cap`` in ``reason_detail``. Clamp predicate includes ``terminal=True`` (#28).

### M1 PR20
- Catalog meta-template `proposed_check`: agent proposes a check when no catalog template fits, with required justification + nearest rejected templates.
- Preregistration: `CheckProposed` (v1) NFKC+JCS digest **before** evaluating `AttemptStarted` / any `ResultRecorded` for that task/target; approval bound to one `task_id`+`target` (target on `RequirementRef.params`; no silent `bound_target` fallback).
- Approval HITL/API/reviewer sources: `CheckApproved` / `CheckRejected`; `by=human` only via journaled `HitlOpened` + `HitlDecided(by=human)` from `answer_hitl` (`kind=proposed_check` + matching `payload_ref`; empty kind/ref fail-closed; propose-written `HitlOpened` / free-form `human_principal` alone never authenticates); delegated reviewer from `ReviewScheduled.reviewer_task_id` + `attempt_role=reviewer` bind; timeout≡reject.
- Core-only counting: unapproved → `unapproved_check`; approved executable like catalog (severe iff trusted); rubric qualitative never-severe; delegated counts but must not tip catalog ties **including 0:0**.
- Quota: `Study.max_proposals` (default 3) caps new pids before eval; excess → `PROPOSAL_QUOTA` with late-write-safe close + StudyFinalized; agent tool `propose_check` registered in `tool_loader`.
- Report: `custom_checks[]`/`observations[]` in 10KB minimal_report truncate; markdown escapes measures; keeps PR16 «Дерево подзадач».
- Upcasters (on main after #28+#29+#31+#32): StudyCreated **v12** (`max_proposals`); bridge **v10→v11** byte-identical to #23 @ 57bc1ad; AttemptStarted **v9** (`attempt_role`, after #23 v8 bridge). StudyFinalized **v4**. Keeps TaskCreated **v13** / PlanAccepted **v11** / PlanExpanded **v5** from #28.
- **Coordinator:** #23 owns StudyCreated v11 / AttemptStarted v8 — delete bridge copies on rebase after #23. Never merge from this agent.

### M1 PR15
- `needs_more_data` + required `artifact(role=report, kind=experiment_design)` with rubric ids `hypothesis` / `variables` / `sample_size` / `success_criterion` (DESIGN_M1 §6.4). Event `QuestionDisposed` (v1; **first-wins**); rejected disposals → journaled `QuestionDisposeRejected` (`_note_reject` only after successful append; fold + `contributing_reasons`).
- Explicit `OutputSpec.kind` + `OutputSpec.evidence_kind` (PlanValidator; rubric ids never imply evidence_kind). Writers: TaskCreated **v13** / PlanAccepted **v11** / PlanExpanded **v5** continuous after #29 PA10/PE4 (`bd413e0`). StudyCreated **v10**. No duplicate `register_upcaster`.
- Fold-only `apply_kind_stamp` (`_stamp_kind` private); missing meta on replay → `_stamp_meta_missing` tainted/internal_error (study still opens → StudyFinalized). `design_artifact_ok` never trusts `meta.kind` alone.
- Honest terminals (`needs_more_data` / `infeasible` / `ill_posed`) stamp ``terminal=True`` for PR16 rollup clamp (no claim-style ``verdict``).
- Projections: dirty-key `_put` for `question_disposals` / `disposal_rejects`; equivalence seed covers decomposes + dispose tables.
- Derivation: review-only never counts; only claim-bound executable CodeCheck MET lifts. `by_theory`. Import-linter: agents ↛ dispose / artifact_stamp.
- **PR8 debt:** `terminal_reason=infeasible` still needs PR8 ill_posed wiring.

### M1 PR16
- `decomposes` acyclicity (self-loop + cycle) on accept/expand writes; `Plan.rollup` (`all|any|majority|rubric`) + `Plan.subquestions` / `QuestionSpec.origin` (`explicit` = PlanOut.subquestions; `implicit` = parent_id without subquestions listing; `emergent` = PlanExpanded origin=emergent).
- PlanValidator: named subquestions; leaf coverage (task or disposition hook); frozen rubric item `decomposition_covers`; depth ≤ `max_depth`; question count ≤ `max_tasks`.
- Parent disposition via `apply_rollup` in `dispositions()` (after PR11 loop-cap; skip only no-own-task decomposes parents). Strict majority `n//2+1` (1:1 / 2:2 → no majority). Rollup never **raises** a parent above its PR 8b claims-based verdict (may lower/keep); `expand_graph` cycle check uses the full existing decomposes tree. Report section «Дерево подзадач».
- Upcasters: PlanAccepted **v10** (after loops v9); PlanExpanded **v4** (after loop fields v3). StudyCreated unchanged (v10; #23 takes v11). `#28` stacks PlanAccepted **v11** / PlanExpanded **v5**. `GraphEdgeAdded` → `graph_edges` in `_affected_tables` for #27 dirty-key projection; load-writes seed covers decomposes edges. No duplicate `register_upcaster`.
- Rollup clamp: terminal own = ``terminal is True`` or ``disposition != not_attempted`` or ``verdict is not None``; incomplete/no-majority/cycle never wipe own terminal; cyclic decomposes localized to subtree (not study-wide wipe).


### M1 PR14 follow-up
- `append_source_fetched`: try/finally releases write permit if append fails after verify; `release_source_fetched_write_permit` raises when no handler is registered.
- Replay pairing trusts journaled `AttemptTainted.{tool,is_fetch}`. Same-name reclassified `is_fetch=False` → domain `FetchToolReclassified` (caught on fold): paired `SourceFetched` uncounted. `FetchIntegrityRaised` journaled only on open/finalize; finalize fold fallback still puts `internal_error` in the report if reconcile fails. Proven by `reclassified_fetch_old` fail-closed refuse on missing provenance, plus authored reclassify journals + natural `tick` finalize.
- `fetch_integrity` projection rows survive without `call_id` (key via tool/row_key).

### Perf / journal write SLO (configurable gate)
- Defaults on and unchanged: **p99 ≤ 50ms**, **max-of-3** rounds, **≥1000 samples/round**. Env overrides (`COSCI_PERF_P99_MS` / `COSCI_PERF_ROUNDS` / `COSCI_PERF_SAMPLES`) allowed in both directions; values weaker than defaults emit `warnings.warn` + a `PERF GATE WEAKENED:` `record_property` on the SLO test (never silent). `COSCI_PERF_GATE=off` skips the SLO test with a clear reason; CI also rejects `GATE=off` and any weaker `COSCI_PERF_*`. Non-finite / out-of-range values raise (`math.isfinite`; p99 in `(0, 10000]`, rounds ≥ 1, samples ≥ 100). CI pins the defaults and, on SLO failure only, reruns the perf pytest **once** under the same 50/3/1000 config (job fails iff both attempts fail; both attempts’ p99 logs are kept). Local loosening/off stays allowed with the explicit warning. Goal: catch a steady slowdown. Demos clear host `COSCI_PERF_*` then use the resolved config.

### Perf / journal write SLO (CI noise)
- p99 ≤ 50ms unchanged. Gate is **max-of-3** independent full loads with **≥1000 samples/round**. `min`/best-of removed — hid intermittent regressions. No separate median SLO.

### Perf / journal write SLO
- Restored honest DESIGN §3.5a measurement: 20 parallel attempts on real-disk `tmp_path`, full `StudyWriter` path (queue → `to_thread` → fence/insert/commit), `writer.latency_samples_ms`, 50ms p99, no retries/skips.
- **Root cause (why only rt_exp tailed):** `projections.apply_event` rewrote *every* row in affected tables on each append (thousands of upserts per load) on the fsync-critical path. Experimental `_project_local` (HITL fold) made the writer queue wait longer between commits, so the O(n²) upsert tax showed up as rt_exp p99 spikes (165–356ms) while rt_core stayed ~6ms. Fix: load only needed tables and upsert keys ``_put`` marks dirty (ContextVar — not `id(row)`). Local measure: dirty-keys alone → p99 ~2–3ms for both runtimes; `asyncio.sleep(0)` after append was **not** required and is omitted so in-memory projection stays seq-ordered with the durable batch.
- **Behavior:** `SourceFetched` `apply_event` now loads `fetch_calls` so journaled pairs land in the SQLite projection (previously the truncated table list left pairs invisible on the write path). Rebase on #26 keeps `AttemptTainted` → `(attempts, fetch_calls, fetch_integrity)`, `FetchIntegrityRaised`, `_row_key` for `fetch_integrity`, and `FetchToolReclassified` fold handling. `#28` adds `QuestionDisposed` / `disposal_rejects` via dirty-key `_put`.
- Equivalence: after the 20-parallel load plus seeded AttemptTainted/SourceFetched/FetchIntegrityRaised/HitlOpened, SQLite projections match `fold_events` with non-empty `source_fetches` / `fetch_calls` / `fetch_integrity` / `hitl_requests`. Canon is strict JSON (no `default=str`).
- Removed store commit-only / shm / wal-autocheckpoint / GC test knobs (those hollowed the SLO).

### Sandbox stdout drain race
- `_watch_process` joins stdout/stderr reader threads after `wait` before sampling sinks. `SandboxLimits.reap_timeout_s` / `output_drain_join_s` are explicit fields (defaults `DEFAULT_REAP_TIMEOUT_S` / `DEFAULT_OUTPUT_DRAIN_JOIN_S`; type-checked finite `> 0`; not derived from `timeout_s`). `CodeCheck` carries the same optional fields; `limits_from_check` / catalog freeze / `_limits` pass them through (omitted → dataclass default only).
- Drain still alive after the join budget sets `output_drain_failed=True`. Alone → `UNAVAILABLE` / `sandbox_unavailable:output_drain`. Guest `TIMEOUT` / `OUTPUT_EXCEEDED` keep their scientific status (attempt charged) and only record the flag — never reclassified as infra.

### Sandbox (host CPU sample order)
- `_watch_process`: sample host-tree CPU via `/proc` **before** `poll()` (poll reaps the child and `/proc` vanishes). Keeps no-cgroup SIGXCPU classification accurate; retains the max sample while the guest is alive. Split out of PR14 so quote work stays focused.
- **M2 notes:** `_host_tree_cpu_s` returns `0.0` (not `None`) when `/proc` is readable but idle; missing `CONFIG_PROC_CHILDREN` silently drops descendants; cgroup idle path untested on CI without delegation.

### M1 PR19
- Input snapshot (K10, A3, K1 / DESIGN §9): `InputBinding.digest` / `digest_version` / `retrieved_at` / `source_uri` at P2; `Attempt.inputs_digest = sha256(JCS(sorted bindings))` (no `retrieved_at`); drift → `input_drift` + `partial`. Legacy `digest=None` or absent/old/garbage `digest_version` → `input_drift_unknown` **without** partial. `get_meta` errors propagate; missing get_meta with `user_input_ids` → limitation (fail-closed). Binding `source_uri` is client URI only (never CAS `file://`).
- User `inputs[]` → CAS via `UserPutCapability` minted only in `api.studies` (`put_user` / `_store(producer=user)` require live `verify_user_put_capability`); private `_ingest_gate` + `_attempt_frame`; lifecycle composed only via `api.runtime` (machine does not import it). Intake exceptions → `IntakeRefuse` + StudyFinalized (never 500 without journal); `LeaseLost`/infra re-raised.
- `InputLimits` **profile-owned**; streaming zip/tar via tarfile-only gate (`_GuardedTarInfo` + `_CountingReader`). Nested/symlink/`../`/NFKC → `INPUT_INVALID`. Meta chain (`x`/`L`/`K`/`g`) depth >8 → `INPUT_INVALID`; stream `Exception` (incl. `RecursionError`) → `INPUT_INVALID`. EOF follows stdlib `tarfile` (unpack only via stdlib).
- Upcasters: StudyCreated **v11** (`as_of`, `user_input_ids`) after #24 StudyCreated **v10** (`max_loop_iter` setdefault). AttemptStarted **v8**. Inherits #28 TaskCreated **v13** / PlanAccepted **v11** / PlanExpanded **v5**; #29 decomposes/rollup chain beneath.
- **M1 gaps (DESIGN §9):** `config_digest` (R14); mid-attempt MCP/DB re-fetch; JCS digest for MCP/DB; soft partial-accept; orphan CAS GC after failed StudyCreated; HITL upload-as-source.

### M1 PR11
- `LoopSpec` on `Plan.loops` (body templates, `scope_*`, mandatory `max_iter` ≤ `Study.max_loop_iter`, `until` predicate).
- Built-in predicates: `survivors_le`, `criterion_met`, `no_change` (scope-restricted); plugins via `register_loop_predicate` (built-ins + existing plugins non-overridable; NFKC names; `survivors_le.n` exact int `0..10000`, never `float()`).
- Invalid LoopSpec → typed **`PlanInvalid`** → **`PlanRefused` + immediate `StudyFinalized`** via **`plan_validate.apply_plan_refused`**. Fail-closed: unknown predicate / unparsable spec / unknown `scope_*`.
- Core materializes each iter as `PlanExpanded(loop=true)` at `PLAN_EXPANDED_SCHEMA_VERSION` / `TASK_CREATED_SCHEMA_VERSION`; `depends_on=["loop:<id>"]` NFKC-normalized; orphan barriers → SKIPPED refuse.
- Until-met → **`LoopCompleted`**. True cap → **`LoopCapped{reason=iteration_cap}`**. Expected eval failures → **`LoopRefused` + `LoopCapped{reason=error}`** (single append batch; no survivor recompute; never disguised as iteration_cap). Unexpected advance/recover errors → journal + **`finalize(trigger=internal_error)`** / `TerminalReason.INTERNAL_ERROR` (never leave TaskDone without StudyFinalized). Disposition `iteration_cap` only for unresolved questions after a true ≥1-iter cap (answered/outside-loop/infeasible preserved).
- Upcasters: PlanAccepted **v9**, TaskCreated **v11**, PlanExpanded **v3**, StudyCreated **v10** (`max_loop_iter`). `#23` takes StudyCreated **v11**. `register_upcaster` duplicate guard + continuous chain.
- Fail-closed internals: `_refuse_loop` failures → `finalize_loop_internal_error`; `log.warning` on journal/close failures; `except LeaseLost: raise` before broad excepts; RUNNING closed (`CORE_BUG`/`CRASHED`) under `_finalizing` so nested `_append(TaskDone)` does not re-enter expand/foreach/loop hooks.

### M1 PR14
- `quote_in_source` (K9): token-boundary + digit-edge; CJK/Thai; super/sub before NFKC; norm cache; presence≠polarity.
- Provenance: opaque `FetchCapability` id + private issued registry in `tools.effects` (not forgeable); verify looks up binds and **consumes**. `AttemptTainted` **before** any `is_fetch` call; `SourceFetched` only via permit after consume; store cannot bypass. Digests attempt-scoped (retries/downstream re-fetch). `countable=True`+`trusted=False`. `ArtifactProducer` includes `user`.
- `NORMALIZATION_VERSION` rules `3`+unidata; M1 gaps documented.

### M1 PR13
- `QuestionSpec.final_type` (`verdict` | `artifact` | `both`) and `OutputSpec.rubric` (`Rubric` / `RubricItem`).
- CodeCheck kind `rubric`: qualitative deliverables; aggregate `all_required` | `k_of_n` (required items only); digest = sha256(JCS(canonical fields, `v=1`)).
- PlanValidator: `artifact` ⇒ OutputSpec.rubric; `verdict` ⇒ ≥1 **explicit** criterion (catalog defaults do **not** count); `both` ⇒ both. **Canonical** `accept_plan` refusal: DAG / `expand_after` / shares critic / criteria / pydantic `TaskSpec`/`QuestionSpec` → `PlanRefused` + immediate `StudyFinalized` (never raw `ValueError` to callers; #24 reuses). Idem = sid + sha256(**full** detail, never truncated).
- Finalize / `rt.view()` share one gate: `study_view` passes `question_final_types` into `study_fulfilled` (never deletes them; `untested` blocks). Verdict may only **downgrade**. `rt.view().verdict` == `StudyFinalized.verdict`.
- Judge: keys = JSON array `(task_id, attempt_id, check_id, item_id, output_digest)` (fail-closed if empty); live status == journal (`PENDING` while retry remains); rebuild from ResultPending/ReviewRecorded only (not RouteReturned); corrupt pending CAS / bad retry → `JudgeCasCorrupt` + `finalize(trigger=internal_error)`.
- **Public payload:** `TerminalReason.INTERNAL_ERROR` / finalize `trigger=internal_error` (new enum value for honest study-level failure, e.g. judge CAS corrupt — not `cancel`).
- Rubric role=validity but excluded from `validity_ok` (failed rubric ≠ claim validity_failed). `both` + verdict + rubric pending → reason `rubric_pending`. Bugs in rubric eval / freeze propagate (no `except Exception → UNASSESSED`).
- Upcasters: PlanAccepted **v8**; TaskCreated **v10**; PlanExpanded **v2** — legacy unknown `final_type` → verdict + report line; current unknown → PlanRefused.
- **M1 gap:** NodeRequirement `STALE` on rubric supersede not implemented; MET of one covering task never covers another's rubric.

### M1 PR18
- Frozen closed `core.tool_registry` (loader-gated `register_tool` only; freeze at end of `tool_loader` → `MappingProxyType`; unregistered → `external_effect`); `invoke_tool` only (import-linter + agents AST); `ToolSpec.effect` advisory.
- **M2 follow-up:** AST gate misses aliases/`getattr`/indirect calls; non-agents packages may still import `tool_registry`; agents contract is meaningful once `coscientist.agents` has code.
- Exactly-once: in-doubt Intended-without-Done spans all attempts by fingerprint; completed keys keep `attempt_id`; read not indexed.
- Taint from journal `AttemptStarted.tainted_context`; per-attempt `ContextVar` for sandbox puts (foreach-safe); taint/side_effect deny → `SideEffectDenied` + report lines.
- Journal effect index + AttemptStarted taint map cached incrementally on the writer (no per-invoke ``read_all``); artifact store required before write call.
- `POST /studies` Idempotency-Key: deterministic id, `O_EXCL`, empty-file recover, fsync+rename; takeover + multi-process race tests.
- Upcasters: AttemptStarted **v7** (`tainted_context`) chained after PR12 v5 shares / v6 `budget_limit_rub` (TaskCreated v9 / PlanAccepted v7 unchanged).

### M1 PR12
- `TaskSpec.budget_share` / `deadline_share` (None = equal remainder of undistributed pool).
- Validators / plan critic: Σ `budget_share` ≤ (1−`replan_reserve`); per-task range + `Study.max_task_budget_share`; critical-path Σ `deadline_share` ≤ (1−reserve). Same critic on `accept_plan`, expansions, foreach, and HITL budget apply.
- P2 renorm (journaled on `AttemptStarted` v6): when shares present, `deadline = min(task.deadline, renormed, ed)`; without shares, `min(task.deadline, ed)` only. Budget: no `max_cost_rub` → `allocated_budget_rub=None` (no task cap); pool/share 0 → refuse at start `SKIPPED(budget)` (no attempt, no exception); else exceed only when `spent > cap`. Cap fixed at first allocation across retries; spend accumulates. Remaining cap passed as `budget_limit_rub`.
- **M1 gap (mid-attempt reserve):** there is no LLM/tool-call runner in M1, so `budget_limit_rub` is **not** enforced mid-flight. Core exposes `would_exceed_budget_limit` for M2 runners (stop before a call that would exceed). Until then the share reserve is **not** inviolable mid-attempt: post-hoc overrun is flagged on `ResultRecorded`/`TaskDone.budget_overrun` (original outcome/detail kept), retries are forbidden, and the report says the task was done with an overrun.
- Concurrent starts: pool subtracts allocated-but-unspent of open tasks so reserve stays untouchable.
- Executor debits `AgentTaskOutput.cost_rub` → `BudgetDebited(task_id=)` on **every** result path (scientific / timeout / same_digest / core_bug / catalog_error / infra) in the **same** append batch as `ResultRecorded`/`TaskDone`. Infra still does not charge an *attempt*; money spent is still debited.
- Foreach children **split** the template’s resolved share (`template_share / N`); when the template share is unset, the resolved parent default (equal remainder, capped) is split the same way. Agents cannot raise own `budget_share` (`agent_share_raise`).
- HITL `budget`: explicit shares via journaled `BudgetSharesAdjusted` (critic-validated; «реши сам»/delegate also applies shares); critic reject → `BudgetSharesRefused` + report line. `delegated_by`/`approved_by` on the HITL decision node only (not plan). Report: task «не выполнено: бюджет задачи исчерпан»; overrun «выполнено с перерасходом…»; study «не выполнено: бюджет исчерпан».
- Upcasters: TaskCreated **v9** / PlanAccepted **v7** (share defaults; PR17 sandbox stays on v8/v6); AttemptStarted v6 (`budget_limit_rub`); StudyCreated v9 (`max_task_budget_share`).

### M1 PR17
- Executable CodeCheck kinds + sandbox limits (B7, B8, K5): `exec_runs`, `tests_pass`, `metric_on_holdout`, `reproducible_rerun`.
- **Separate verifier (B1):** collect agent declared outputs (sha256); catalog scorer / task tests in a second fresh bwrap jail (data/scorer/holdout/tests RO; only results RW); read structured `reward.txt` JSON / junit — never stdout. Holdout labels from catalog (`CodeCheck.holdout`); agent holdout slots ignored. `catalog:metric` scores predictions-as-data (`python -I`). `tests_pass` is **observation-only** (`countable=false`, `trusted=false`; tamper-proof = M2). Per-check `input_allowlist` (fail-closed).
- **bwrap hard isolation (B2):** `--clearenv`, system `--ro-bind`, `--tmpfs /tmp`, `--unshare-net`/`--unshare-pid`/`--unshare-ipc`/`uts`/`cgroup-try`, `--disable-userns`, `--cap-drop ALL`, `--new-session`, `--die-with-parent`; journal/CAS never mounted. Hard only after a real probe. Cgroup: start bwrap inside leaf; `oom_detectable` only after guest host pid verified.
- **Fail-closed:** without verified hard FS isolation → `sandbox_unavailable` (no guest exec). CI installs `bubblewrap` and lifts Ubuntu 24.04 AppArmor userns restrict.
- **`trusted=true` only when both net and fs are hard** (never fake hard). Isolation + `core_evaluated` for replay from journal effect, not artifact meta.
- Infra **only** via private FD (wrapper rlimit / cgroup attach / host errors) + blocked/unavailable/verified cgroup OOM — does **not** charge the retry budget. Guest wall/CPU → host-classified `idle_timeout` / `cpu_timeout` / `wall_timeout` (`result_class=timeout`, never `k_ref`/`k_sup`); **`cpu_timeout` from host cgroup watcher**; with cgroup, `RLIMIT_CPU` is a fuse above budget; without cgroup, SIGXCPU/152 → `cpu_timeout` only when host rusage ≥ budget else NONZERO; refute-role → `refute_unresolved`; idle / no-cgroup refute / ≥2 **task-level** prediction timeouts (any template) → `untestable_by_agent` (single last-attempt timeout not escalated). **Catalog-only per-check limits** frozen on all TaskCreated paths; same-digest over jail-mounted bytes; prediction `exec_runs` via verifier jail. Cgroup `cpu.max` + `cpu.stat`; CPU budget always &lt; wall. `tests_pass` + `reproducible_rerun` observation-only. `agent_facing_*` sole agent template serialization (prompt renderer later).
- Upcasters: TaskCreated **v8**, PlanAccepted **v6** (`sandbox_check_limits` / `sandbox_limits_note`; aggregate slots always stripped); stacked on PR10 foreach/quorum versions.
- Docs: host requirements + timeout redesign. CI moves job into `ci-job` leaf before `subtree_control` (no-internal-processes); if still refused, cpu.stat tests skip (known M1 gap).

### M1 PR10
- `TaskSpec.foreach` (selected/all hypothesis|question of a source task): core materializes N `EXP-<n>` via `PlanExpanded(foreach=true)` — no planner, no expansion quota; restart/recover resumes; report lines in Russian.
- Expand at source `TaskDone` **and** at `accept_plan` / after `PlanExpanded` when `of_task` is already DONE (B1; live == replay). `validate_foreach_specs` in `validate_expansion`.
- Selector requires `source_task == of_task`; cap keeps best **rank** (missing/non-numeric rank **last**, report note; never SKIP template); journals `foreach_total`/`foreach_dropped`/`foreach_rank_notes`; kept children nat-sorted; deterministic `pl_foreach_*`; `max_foreach_children` (StudyCreated **v8**).
- Foreach errors → `ExpansionRefused` + `TaskDone SKIPPED(refused)`; `iter_dispatchable_tasks` excludes templates; `start_attempt` rejects them.
- Quorum pools: **one static pool** per fan-in task + **one pool per foreach template** (never by equal quorum value). Reject mix of ``all``+int; order-independent selection; quorum > static pool size rejected; foreach quorum > children → report «кворум > числа детей».
- Foreach template ``SUPERSEDED`` (empty) and ``REFUSED`` both count as done for ``expand_after`` → PLAN-n.
- Upcasters: TaskCreated v7, PlanAccepted v5, AttemptStarted v4, StudyCreated v8.

### M1 PR9
- `PlanExpanded` + H-g trigger (`expand_after` / `AgentTaskOutput.expansion_request` / HITL).
- Agent `expansion_request` / `expand_after` enqueue **PLAN-n → critic** (never direct `PlanExpanded`); HITL plan edit / «decide yourself» is the third trigger; `delegated_by` from `HitlDecided`.
- Agent `max_expansions` vs human `max_human_expansions`; `replan_reserve` / quotas / `max_depth`; invalid requests → `ExpansionRefused(invalid)` (never raise from record/tick); report lines `not done: expansion limit|invalid|reserve`.
- Expansion counts + `replan_used` folded from events (restart == live); recover re-queues pending expand after crash mid TaskDone→expand.
- Core assigns `EXP-<n>` on agent path; deterministic `ExpansionRefused` idem keys; shared-descendant skip rule; agent cannot supersede away countable refutation (`has_refutation`).
- Question add/supersede (`supersedes` question→question); C10 k-filter excludes claims on superseded targets.
- StudyCreated **v7** + PlanAccepted **v4** (`expand_after`); `max_depth` + `decomposes` edge.
- StudyLifecycle property rules: `expand`, `question_supersede`.
- #15 follow-ups kept: `schema_valid` missing/foreign artifact; **`core_evaluated` from effect only** (meta → producer/call_id).

### M1 PR8b
- Hypothesis verdict model (`docs/DESIGN_HYP_DELTA.md`): claim `validity`/`outcome`/`severe`/`independence_key`; stub|partial|lit|invalid excluded **before** counting.
- `independence_key` base = `experiment_id`; count-time merge by **transitive root-data** digest overlap (code vs data from **`ResultRecorded.output_kinds`** via catalog **`code_output_names`** — planner `OutputSpec.role` and agent `media_type` ignored; all-code upstream → intermediate is a root; diamond DFS uses on_stack vs done) or **structural** share-any criterion template (same target, empty roots; empty plan criteria → `catalog.defaults["experiment"]`). No TaskSpec/goal/title/plan_id hash. Method is not in the key.
- **Countable vs trusted:** evidence is **countable** when the core evaluates a catalog check on an artifact bound to the **current attempt** (`produced_by.call_id`). Countable gates **both** `refuted` (`k_ref`) and severe/`supported` symmetrically. Foreign artifacts are rejected. `effect.trusted` is a separate stronger tag — **PR 17** wires executable kinds to `put_sandbox`; trusted only when sandbox limits fully held (`net_isolation != soft`).
- **No numeric effect → indeterminate** (deliberate safe weakening vs main): missing output / missing artifact / empty json_pointer / `foreign_artifact` never refute. Report carries a **specific** reason (`output_not_submitted`, `artifact_missing`, `pointer_empty`, `foreign_artifact`) — not generic `unclear`.
- On journal read/replay: recompute `severe`/`refute_capable`/`trusted`/`countable` from artifact metadata when the store is attached, else from effect-stamped provenance facts — never journal bits. **Compat:** old `inconsistent` journals without countable provenance may recompute differently (indeterminate / not toward `k_ref`).
- `ResultRecorded` **v3** + v2→v3 upcaster for `output_kinds`. M1: caller passes `attempt_id` for call_id binding; production catalogs have empty `code_output_names` (conservative → all data).
- Study: **`supported_needed`** (default 2, ≥2); `confirmations_needed` = PR8 snapshot `k`. StudyCreated **v6**.
- `aggregate='evalue'` rejected at create (deferred until core computes p-values).
- Report: `hypothesis_report_row` in finalize; failures set dedicated `hypotheses_unavailable` (not a fake hypothesis id). Bind snapshots win on replay; finalize recomputes report rows.
- Keeps PR8: check roles, per-experiment `validity_failed` / `unknown_role`, `VerdictSnapshotsRecorded`.
- Load write test: serial/slow; absolute p99 ≤ 50ms SLO in the dedicated perf job.

### M1 PR8
- Runtime `verdict()` wired into `dispositions()` / finalize (Study `confirmations_needed` / `refutations_needed` / `refutation_priority`); supported → answered; hypothesis verdicts in StudyFinalized.
- `InputSpec.kind=verdict` enabled; `VerdictSnapshotsRecorded` persists full snapshots (not digest-only); invalidated on claim/`about` changes (scoped to affected target).
- Claim carry-overs: worst provenance; no FAILED claims; tight whitelist (`target`/`checked_against`/`reason`/`premise_idx`); core-derived counting fields (`criterion_status` from checked_against ReqStatus; `frozen` from plan-accept criterion nodes; `accepted` = SUCCESS ∧ validated); intent from producing experiment's `tests` edge; namespaced claim ids; unknown targets rejected; `about` required.
- `open_runtime` (core + experimental) builds Executor via `make_executor` (live `get_graph`); `catalog=` required (no test-catalog fallback); one `k`=`confirmations_needed` everywhere.
- `accepted` = recorded scientific result ∧ structural claim validation (not criteria pass). Infra failures only drop claims (`result_class`).
- Check `role` (Duhem–Quine): catalog-only; `RequirementTemplate.role` **required** in catalog format; plan mismatch rejected; `unknown_role` exclusion never forces a verdict; validity failure is per-experiment (`excluded_claims`), inconclusive only when no valid claims remain; scientific FAILED no retry / no H-c cascade.
- `hypothesis_verdicts` kept separate from bind `verdict_snapshots` cache; Q/H grouping keys are kind-prefixed.
- StudyCreated schema_version=4 + v3→v4 upcaster for `confirmations_needed` (no longer piggybacked on v2→v3).
- R17 runtime metadata wiring deferred (DESIGN note + xfail); pure-rule path remains in PR 6.
- Totality: garbage claim attrs dropped (never raise into finalize/view/start_attempt).
- Priority-refuted provenance exception unchanged; report/snapshot `note` when stub/partial supports accompany priority-win (`refuted; N supporting results were stub/partial`).
- Compat: `view()` with graph/study uses PR8 dispositions; PR7 journals without counting claims stay `not_attempted`.

### M1 PR7
- `InputSpec.kind: artifact | claim` (+ `ref`); `uses_claim` edge (17th) at P2.
- `Executor.record` writes claim nodes with `attrs.provenance` from the producing attempt; unknown → bind as `stub` (never real).
- P2 bindings (+ metadata `digest`) on `AttemptStarted` (v3 + upcaster); `GraphEdgeAdded` fails loudly.
- `kind=verdict` landed in **PR 8**.
- TaskCreated v4→v5 upcaster defaults `kind=artifact`.
- #12 leftovers: R17 confirm-only; refmodel `intent` Literal; gen edge attrs; cancel `finalize_mod`; r17 module-level import.

### M1 PR6
- `tests.intent` (confirm|refute, edge attr in graph_schema): refute+met → verdict `survived`; `Study.refutations_needed` / `refutation_priority` at create.
- Pure rules only: `k_ref` / `refutation_priority` / `intent` take effect in production once **PR 8** wires `verdict()` into runtime finalize.
- Provenance ceiling: ≥ k_ref real priority refs stay `refuted` (stub/partial supports do not cap a refutation).
- R17 wired into `counts()`/`verdict()` via `claim_counts_under_r17` (criterion before cited evidence; threshold-source excluded).
- Leftovers: H-c+cancel test; H-c+budget via `record_budget_debit` (`_artifact_store` already uses `executor.artifacts`).

### M1 follow-up (post PR5)
- Confirm `delegated_costly ≤ max_cost_rub` is create_study-only; replay test pins `Study.model_validate` / local_project reopen.
- Skip H-c when `_budget_exceeded` / `_study_cancelled` so children close with budget/cancelled, not `input_unavailable`.
- Graph `experiment.method` aligned to optional (matches `TaskSpec.method`).

### M1 PR5
- `InputSpec.mode` default is `forbid` (stubs only with an explicit mode); TaskCreated v3→v4 upcaster restores `use_partial` for old journals.
- `TaskSpec.tolerate_failed_dep` and H-c: FAILED/SKIPPED parents without `InputSpec` skip children as `input_unavailable`. Stage tasks (SPEC…REPORT) are excluded from H-c so a failed SPEC still allows PLAN/REPORT (DESIGN §7.5).
- `ExperimentMethod` enum matches graph schema; `method=simulation` is scientific metadata and does **not** upgrade stub/partial input provenance.

### M1 PR4 (API note)
- `coscientist.core.plan.intake.intake` / `coscientist.core.claims.reasons.intake` are **async** (`await intake(...)`). Sync call sites must use `asyncio.run` or an event loop. Classifier timeouts use `asyncio.wait_for(asyncio.to_thread(...))`.
