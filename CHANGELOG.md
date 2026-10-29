# Changelog

## Unreleased

### cleanup(tools-5): effects fold fields + ToolErrorKind (no behavior change)
- `_event_fold_fields` types EffectIntended/Done and taint folds without
  ``getattr`` (``EventEnvelope`` attrs; plain ``dict`` keeps getattr-first
  semantics — mapping keys are not attributes).
- `_tool_error_kind` removes ``type: ignore[arg-type]`` on EffectDone restore;
  ``on_event`` is ``Callable[[EventEnvelope], None]``.
- Left intentional: writer ``_pr18_*`` getattr/type:ignore cache folds;
  DESIGN §6.1 ``except Exception`` → ``ToolError("opaque")``; JSON
  ``dict[str, Any]`` payloads until EventEnvelope payload typing moves.

### cleanup(tools-4): typed FetchCapability.id on invalidate (no behavior change)
- `#364`: `_invalidate_fetch_capability` reads `capability.id` directly
  (`FetchCapability` is a frozen dataclass with `id: str`); registry discard
  stays `.pop(..., None)` as on main (same style as other capability pops here).

### cleanup(agents-5): extract HYP/LIT schemas into ``schemas_hyp_lit`` (#362)
- Move ``HypothesisDraft`` / ``HypothesisOut`` / ``HypothesisRowsV1`` /
  ``PaperRef`` / ``LiteratureOut`` plus ``hypothesis_rows_v1``,
  ``receipt_identity_keys``, and ``literature_binding_error`` out of
  ``schemas.py`` into ``coscientist.agents.schemas_hyp_lit`` (same source
  text; five ``audit-ok`` markers unchanged).
- Importers (adapter, cli journal/run, hyp/lit tests) take those names from
  ``coscientist.agents.schemas_hyp_lit``; ``schemas.py`` keeps no re-export.
  Both modules use ``AgentModel`` from ``coscientist.agents.strict`` (same
  ``model_config``). No behavior or API change.

### cleanup(cli): restore coder_jobs null inputs/env → {} (#351)
- Restored main semantics for optional `--coder-jobs` `inputs`/`env`:
  JSON `null` becomes `{}` (as on main), not a Mapping refuse.

### cleanup(cli-2): extract dispatch + study_helpers (#338)
- Byte-move ``_NON_EXPERIMENT_ROUTE_PREFIXES`` / ``_dispatch_coder_task`` /
  ``_finish_coder_dispatch`` out of ``run.py`` into ``coscientist.cli.coder_jobs``;
  extract ``_require_attempt``, converters, and journal readbacks into
  ``coscientist.cli.study_helpers``.
- Callers import from the defining modules. The ``audit-ok[broad_except]`` on
  ``except BaseException`` moves 1:1; no behavior or API change.

### feat(finalize): report at ANY terminal with an honest what-went-wrong section
- Owner law #279 (universal-acceptance S2, legacy quality_basis pattern): the
  human-readable CAS `report.md` now explains «что не так» at EVERY terminal
  (refused / failed / cancelled / partial / stubbed), not only fulfilled. The
  `## Honest outcome` section lists FAILED/SKIPPED tasks with their typed
  reason, fulfillment-blocking question dispositions with typed reasons,
  refusal and fallback_reason — facts folded from the journal, no authored
  prose — plus the mandatory disclaimer: a non-fulfilled report does NOT
  assert scientific validity («no scientific acceptance is asserted»).
- `## Usage` section: journal-grounded accounting only (AttemptStarted,
  ArtifactRegistered, BudgetDebited with exact spent_rub strings); LLM tokens
  are not journaled, so the report says so explicitly instead of inventing
  numbers. `render_markdown_report` gained an optional `usage` param (None
  keeps the section out — older callers unchanged).
- Last-resort totality: intake-refuse-without-study and rich-path crashes —
  the terminals where `report.md` was never written — now get a best-effort
  CAS `report.md` built from the last-resort minimal report. The
  StudyFinalized payload stays constant-only (report_artifact_id XOR
  minimal_report contract untouched); packaging failures never sink finalize.
- Verdicts, gates and rc semantics unchanged; the LLM report lane stays
  fulfilled-only (no model prose on non-fulfilled — budget and honesty).

### feat(api): describe trusted capability surface; allow an API-side tool mask
- `describe_cli_capabilities` (CLI) and `describe_run_capabilities` (API) read
  the pinned profile and MCP bundle and expose only models, tool names and
  binding digests — no URLs, commands or credentials cross to the browser;
  `configured_capabilities` builds the same browser-safe catalog from server
  presets and refuses conflicting declarations of one server.
- `POST /studies/{id}/run` accepts an optional `allowed_tool_ids` mask (the
  API side of the CLI `--allowed-tool-ids` flag): it flows through the existing
  namespace into the trusted `LiveCliToolSurface` mask, and preset configs can
  pin it to narrow a launch surface. Historical payloads are byte-identical:
  the wrap serializer drops the key while the mask is absent.
- Intentionally NOT ported from PR246: the profile settings store and revision
  pinning layer (removed on main by #247/#248).

### refactor(core/cli): need-stage closure moves to the accepted-plan boundary
- Architectural fix from live runs (microfluidics mf3): the planner may
  legitimately flip a Need flag between planning rounds (need_off in round 1,
  needed in the critic-ordered round 2). Closing the HYP/LIT DAG tasks at the
  round-1 need_off let an INTERMEDIATE round mutate terminal DAG state — the
  flip then hit a typed one-shot refuse (#233) where the honest behavior is to
  RUN the stage in the later round.
- Core (lifecycle.accept_plan): not-needed HYP/LIT stages close SKIPPED
  ("not needed by the accepted plan") in the SAME transaction as PlanAccepted —
  terminal DAG state is written from the ACCEPTED plan only; a DONE stage is
  never re-closed (TaskDone idem keys are unique).
- Core (lifecycle.create_study): the intake stage chain SPEC→HYP→LIT→PLAN is
  removed — stages are loop-driven by the planning driver in fixed order, and
  the chain's only effect was gating a needed stage on an unclosed not-needed
  one once closure deferred to accept. REPORT keeps deps=["PLAN"] (the real
  post-accept dependency); the generic deps gate stays pinned by
  test_deps_must_be_done (fixture moved to REPORT).
- CLI (_optional_need_stage): need_off no longer cancels the stage — an
  honest usage note records the round's flag and the stage stays startable
  until accept; the DONE-guard (#233) remains as drift defense.
- Tests: core closure test (in-transaction SKIPPED, needed stage untouched);
  the flip scenario now fulfills with LIT running exactly once in round 2.

### fix(cli): replan rounds never re-drive need stages (HYP/LIT one-shot) — live crash fixed
- Live find (microfluidics-next-1-search, litsearch profile): LIT closed SUCCESS
  in plan round 1; the critic-ordered replan re-entered `_optional_need_stage`
  with literature.needed=true again and `start_attempt` raised unhandled
  `RuntimeError("task already DONE")` — the run died with a traceback (rc=1).
- `_optional_need_stage` now resolves the stage task first: DONE+SUCCESS →
  satisfied, an honest usage note is journaled and the round proceeds on the
  already-recorded hypotheses/literature; DONE+SKIPPED (a needed=false→true
  flip across rounds) → typed `CliRefuse` code=3 (TaskDone is terminal, the
  core has no reopen) instead of a RuntimeError crash.
- Tests: replan re-requesting literature runs LIT exactly once and fulfills;
  the false→true flip refuses typed with the LIT stage staying SKIPPED.

### feat(profiles): research-glm-litsearch — wired researcher WITHOUT paper download
- New packaged profile `research-glm-litsearch.v1.yaml` (catalog entry v1,
  tool_catalog_version 2): identical to `research-glm` except the researcher
  role runs `search_papers` + `find_papers_in_db` only —
  `download_papers_from_search` is dropped while the lit server's download
  path returns HTTP 401 (server-side S3 creds; external, verified live
  2026-10-26 via bench/lab/mcp/probe.py).
- Config-only, no code paths changed: `record_literature` already journals
  every tool result and refuses papers that do not trace to a journaled
  result, so search-only literature stays tool-backed.
- Tests: catalog lists the new profile; profile delta pinned vs research-glm.

### feat(bench): driver lanes for the live runner — web (API+WS) and recovery (SIGKILL → continue)
- `bench/drivers/` (new) closes the phase-3 gap: `BenchTask.driver`
  (`web` | `recovery` | `None` — task data, never a config field) now selects
  a live lane in `bench.live_runner.LiveRunner` after the SHARED preflight
  (journal-exists guard + task-input resolution, extracted once for all
  lanes) instead of the typed `driver_task` refusal:
  - `bench/drivers/web.py` (`WebDriver`): the old `webdrive.py` mode=web
    contract on the new API lane (PR #220) — starts
    `python -m coscientist.api.serve` as a SUBPROCESS (every flag explicit
    from config), POSTs `RunStudyBody` built verbatim from the shared
    config, collects the WS journal stream to JSONL (`?after=` resume guard,
    no replay), receives `StudyFinalized`, polls `GET /studies/{id}/run`.
    The bench study id maps onto the core idempotent id
    (`study_id_from_idempotency_key`) — the API lane enforces the core
    study-id format. Honest mapping: run `done` + served summary → the exact
    CLI-lane mapping (`bench.runsummary.success_from_summary`); refused after
    closure (the lane serves no summary) → `RunnerSuccess` with the verdict
    from the journal + the shared observer fold (usage unobservable → `()`,
    never estimated); stream timeout → `RunnerSuccess` status `timeout` with
    the partial observation; POST refusal / server death / missing lazy
    httpx+websockets deps → typed `RunnerRefuse`.
  - `bench/drivers/recovery.py` (`RecoveryDriver`): the new-lane equivalent
    of the legacy recovery mode (PR #221, `recovery_drive.sh` contract) —
    `coscientist run` subprocess (the SHARED `bench.clilane.build_run_argv` —
    no second argv construction), read-only journal poll for the configured
    kill marker (`recovery_kill_event_type` + `entity_key` substring, e.g.
    first EXP `ResultRecorded`), SIGKILL of the process group, then
    `coscientist continue` (`build_continue_argv`: the CLI continue flag set
    verbatim; no study-creation flags — journaled and immutable). Verify:
    the continue summary verdict must equal the journal's `StudyFinalized`
    verdict with exactly one finalize (deep restore invariants stay with
    `bench/lab/driver/recovery_check.py`). A run exiting before the marker
    keeps the CLI exit-code contract (`result_from_process`).
  - `bench/drivers/journal.py`: one read-only observer shared by both lanes
    (`observe_quality`: experiment tasks by kind, results deduped by
    `result_id`, tallies sorted by outcome — same semantics as the run
    summary's `_observed_quality`; `count_marker_events` /
    `read_journal_events` on read-only SQLite, missing db = no events yet).
    Parity with the authoritative summary fold is PINNED by the web e2e on a
    real closed study.
- `LiveRunnerConfig` driver wiring (every field required, no defaults —
  declared up front even when no driver task runs): `driver_out_dir`,
  `web_host/web_port/web_stream_poll_interval_s/web_run_wait_s/
  web_post_timeout_s` (validated strictly above `web_run_wait_s`),
  `web_stream_timeout_s/web_connect_retries`, `recovery_kill_event_type/
  recovery_kill_entity_contains/recovery_kill_wait_s/recovery_lease_wait_s`.
  `bench/runners.example.yaml` documents the full set.
- Shared-code moves (no behavior change, no second path):
  `bench/runsummary.py` (summary parse + usage/quality mapping, was private
  in `live_runner`) and `bench/clilane.py` (`build_run_argv` +
  `result_from_process` exit-code contract) — the CLI lane and the drivers
  read one implementation of each.
- Refusals: `study_journal_exists` (all lanes, incl. the web lane's mapped
  id), `web_post_refused` / `web_server_died` / `web_driver_deps_missing` /
  `web_run_refused`, `exit_<rc>` (continue CLI refusals),
  `recovery_verify_failed`, `summary_unreadable`; an observed `timeout` is a
  `RunnerSuccess` status (HER-comparable), never a verdict invention.
- Tests: `tests/test_bench_drivers.py` (config bounds, observer fold + dedup,
  marker counting on SQLite, POST-body/continue-argv mechanical mappings,
  LiveRunner branch to lanes with the shared preflight); `tests/
  test_bench_driver_recovery_e2e.py` (slow: real SIGKILL through the driver →
  continue → restored study closes fulfilled, checker invariants clean);
  `tests/test_bench_driver_web_e2e.py` (slow: uvicorn subprocess + POST/WS
  lane end to end + observer/summary parity pin; requires launcher-supplied
  uvicorn+websockets, skipped where absent — e.g. plain CI env).

### feat(cli)+feat(bench): observed quality in the run summary; live `new` bench runner (plan item 13)
- `coscientist run` final summary JSON line gains an additive `quality` block:
  `{exp_tasks, evidence: [{outcome, count}, ...], record_result}` folded from
  the closing study's journal state (`_observed_quality`) — EXPERIMENT-task
  count, their recorded-result outcome tallies (sorted by outcome name),
  recorded-result count. Scoped to EXPERIMENT tasks (stage rows like SPEC
  record results too and are not experiment evidence — the HER-comparable
  quantity). The runtime fold double-keys result rows (`result_id` +
  `attempt_id`), so rows dedup by payload `result_id`; the result's task kind
  goes through the folded attempt row, never id parsing. Additive keys: the
  API lane and humans read the same declared output — no second path.
- `bench/live_runner.py` (new): `LiveRunnerConfig` — frozen, extra-forbid,
  EVERY field required (nullable written explicitly; cassette `tools_json` XOR
  live `mcp_tool_bundle` + required `mcp_tool_call_args`; `coder_jobs` needs
  `coder_backend`) — mirrors `_validate_tool_cli_flags` — plus `LiveRunner`,
  a `BenchRunner` that maps each `BenchTask` mechanically onto the
  `coscientist run` flag surface and executes it as a SUBPROCESS
  (`sys.executable -m coscientist.cli`, isolated, required `timeout_s` cap).
  Task mapping: prompt → temp task file, `BenchTask.inputs` → `--task-input`
  per file resolved under the config's `task_inputs_dir`, study id =
  `<study_id_prefix>-<task_id>` (an existing journal dir refuses — reruns need
  a fresh `data_root`/prefix). Driver tasks (`web|recovery`) typed-refuse.
- Plan deviation (documented in the module docstring): item 13 names the
  agent-layer Orchestrator (G1/G1b, #66) as the entrypoint — no such
  composition object exists; the CLI run path is the sanctioned single-source
  live composition (the API lane reuses the exact same function).
- Honest accounting: exit 0 (fulfilled) and 7 (closed non-fulfilled) are
  OBSERVED outcomes → `RunnerSuccess` with the summary's verdict as quality
  status; every other exit code, an unreadable/absent summary line, a
  subprocess timeout, and config problems are typed `RunnerRefuse`
  (`exit_<rc>` / `summary_unreadable` / `timeout` / `driver_task` /
  `study_journal_exists` / `task_input_missing`). Usage rows: only rows
  carrying `prompt_tokens` + `completion_tokens` are LLM calls (`_usage_row`
  shape, report/review lanes included); coder-job, retry-note and
  critic-issue note rows are not LLM calls and are not counted; tokens are
  never estimated. Exact equality vs task YAML stays in `bench.quality`.
- Registry/CLI: `bench/runners.py` registers `new` (requires
  `--runner-config`; typed refusal without it; `fake` refuses a config);
  `bench/cli.py` grows `--runner-config PATH`;
  `bench/runners.example.yaml` documents the full explicit field set.
- DoD: `tests/test_bench_live_runner.py` (config surface, refusal paths incl.
  a real exit-2 CLI refusal and the subprocess timeout, canned summary parse
  incl. mixed usage rows, e2e over the local fake-LLM HTTP server through the
  real CLI subprocess asserting `run_suite` quality compare equal; second-run
  journal refusal), `tests/cli/test_run_study.py` (summary `quality` block
  assertions), `tests/test_bench_cli.py` (registry `("fake", "new")`,
  `--runner-config` wiring).

### feat(agents): wire the plan critic and the post-close reviewer lane (--wired-roles critic/reviewer)
- Gaps №5/№10 closed: `agents/critic.py` was written but never invoked (planner
  output went straight to `accept_plan`) and `agents/reviewer.py` was not in the
  live path. Both roles now run through the existing `LlmAgentAdapter` pattern —
  same client, schema echo + one bounded repair round (`_complete_validated`),
  cumulative `Budget` enforcement — no second LLM path.
- Wired plan critic (`--wired-roles ["critic"]`): between `derive_plan` and
  `accept_plan` the derived `PlanOut` + `StudyContext` go to the critic
  (input `CriticIn`, output the designed `CriticVerdict` from
  `agents/critic.py` — reused, not duplicated). `accept` → `accept_plan` as
  today; `revise`/`reject` → exactly ONE in-place planner retry with the
  formatted issues as `StudyContext.plan_refusal_hint` (the #219 replan
  machinery, shared budget: a derive/accept refusal and the critic share the
  single round). Issues persisting after that round → the plan is ACCEPTED
  with the issues journaled as a `critic` usage row (`stage: plan`, issues +
  note) in the run summary — fail-closed in the sense that the findings are
  never silently dropped; the plan-approval HITL gate remains the hard gate.
  Structural bound: ≤2 critic calls per planning cycle.
- Wired review lane (`--wired-roles ["reviewer"]`): post-finalize (fulfilled
  runs), same lane pattern as the report lane — reads the graph projection,
  builds `build_graph_digest`, runs the reviewer role over
  `ReviewerIn{context, digest_markdown}` → `ReviewerOut{assessment, gaps}`,
  CASes `review.md` (assessment + gaps + the digest embedded verbatim as the
  fact source) and appends a `reviewer` usage row. Writes NO journal events —
  the verdict can never flip. New module `coscientist/cli/review_lane.py`.
- Adapter: `_OUTPUT_MODELS` += critic/reviewer; typed inputs `CriticIn` /
  `ReviewerIn` + `ReviewerOut` in `agents/schemas.py` (strict, extra=forbid);
  new `LlmAgentAdapter.can_run_role` (instance-level role model + prompt
  registered) as the wired-stage dependency seam; `run()` refuses typed
  (AgentError) when a class-supported role is not configured on the instance.
- Prompts: `role.critic` / `role.reviewer` v2 in `prompts/roles.py` (plain
  role body; the schema echo carries the contract). v1 bodies stay in
  `prompts/builtin.py` (designed §7.1 HITL verdict contracts, not wired).
- Profiles: research.v1 / research-glm.v1 already carried the critic/reviewer
  roles and `critic`/`review` routes (budget shape unchanged) — wired stages
  read the role model from the profile, session `Budget` enforcement unchanged
  (per-route budget scoping stays future work, as for plan/execute today).
- Unwired runs are byte-identical: no critic/reviewer cassette bodies, no
  extra usage rows, same exit codes.
- DoD: `tests/cli/test_roles_wiring.py` (cassette e2e: accept proceeds;
  one planner retry on issues; journaled-accept (recorded in the run summary only — study journal untouched) on second failure; unwired
  makes no role calls; review.md + usage row; three code-5 refusals),
  `tests/agents/test_adapter_roles.py` (instance-config refuse; critic input
  contract).
### feat(graph): C1 evidence-link — journaled tool_call artifacts become evidence nodes [schema v0.9]
- Found by the live two-tasks-report run: the report lane honestly wrote
  «не получено» for content numbers because tool_call results never became
  graph nodes — the digest (graph_nodes/graph_edges projection) carried only
  questions/hypotheses/tasks/criteria/claims, and the old system's practice
  of journaling tool outputs as graph evidence had no equivalent (CAS
  artifacts existed, but the graph was content-blind).
- `coscientist/core/execution/evidence_link.py` (new): at
  `Executor.record` the attempt's recorded output artifacts are journaled —
  in the SAME append batch as `ResultRecorded` (via `extra_events`, the
  `claim_nodes_from_attempt` pattern) — as `GraphNodeUpserted(kind=artifact)`
  (identity from the registered `ArtifactRegistered` meta) +
  `GraphNodeUpserted(kind=evidence)` (computational observation; `statement`
  carries mechanically extracted facts from the JOURNALED CAS bytes;
  `ref` = EvidenceRef(artifact_id, "", sha256); new optional schema field
  `evidence.artifact_uri` = CAS uri) + `derived_from` (evidence → artifact)
  + `produced` (experiment → artifact, only when the folded graph already
  has the experiment node — that builder is G13's, never invented here) +
  `relates_to` (evidence → question per `TaskSpec.addresses`, only when the
  PlanAccepted question node exists). All writes go through the G7 writer
  (`graph_node_upserted` / `graph_edge_added`) — one write path, digest idem
  keys make replay a no-op.
- **Edge name: `relates_to`, not `supports`.** RESEARCH_MODEL §9 deliberately
  retired supports/refutes/refines edges in favor of claim(stance); a raw
  tool observation *relates to* the question it was produced for — verdict
  semantics stay computed from criteria, not re-imported as an edge name.
- Facts extraction is mechanical with typed caps: JSON leaves in document
  order (`path = value`, values via `json.dumps` exactly as parsed), depth
  ≤ 4 / ≤ 32 leaves / 200 chars per leaf (truncation says so: «разбор
  ограничен»); parse budget = `minimal_report.MAX_BYTES` (10 240 — existing
  constant, no invented number). Oversized (size pre-check from journal
  meta — bytes never read), non-JSON, and non-finite-constant artifacts
  contribute a reference-only evidence (name, media_type, byte size, CAS
  uri + digest) — fail-closed, never a crash, never invented content.
  Evidence is journaled ONLY for `provenance="tool"` artifacts (the journal
  stamp «bytes are this attempt's recorded tool_call result», set by the
  trusted CLI journal / `put_sandbox`); model-authored values echoes
  (`producer="agent"`) are excluded by that stamp.
- Digest (`reporting/graph_digest.py`): `relates_to` renders as «Наблюдение N
  относится к Вопросу M»; evidence statements (the extracted facts) were
  already rendered, so every extracted number enters the honesty inventory
  automatically — numbers-in/numbers-out now has real numbers to trace. New
  `GraphDigest.evidence_by_artifact` + `evidence_for_artifact()`; the
  science report «Материалы и воспроизводимость» appendix lists each
  artifact with its linked evidence facts (raw CAS uris/digests stay in node
  attrs, never in digest markdown — no new prose-poisoning holes).
- Schema v0.8 → v0.9: +`relates_to` edge (evidence → question), +optional
  `evidence.artifact_uri` (15 nodes / 20 edges; DESIGN §16, RESEARCH_MODEL
  §1/§2/§3 regenerated via `scripts/gen_model_tables.py`).
- Honesty stays intact: facts come from journaled artifact bytes (CAS +
  `ArtifactRegistered` sha256), not model claims; unparseable/binary
  artifacts contribute a reference only.
- DoD: `tests/test_evidence_link.py` (16 tests: facts caps, reference-only
  fallbacks, G7 events, digest inventory, honesty tracing, executor record
  batch), `tests/cli/test_report_lane.py::test_report_lane_tool_numbers_trace_to_evidence`
  (two_tasks e2e: report.science.md carries the real 225/132 traced to
  «Наблюдение 1 … n_rows = 225»); pytest parity 44 failed (pre-existing
  macOS sandbox/cgroup baseline) / 3128 passed; ruff, mypy, gen --check,
  lint-imports (21 kept), audit_heuristics --vs-main 0, pytest -m perf.
### feat(agents): wire the hypothesizer (HYP) and researcher (LIT) need-stage roles [gap №8]
- Gap: planner Need flags (`hypotheses.needed` / `literature.needed`) were
  parsed, but a wired role still refused — `_optional_need_stage` ended in a
  typed "CLI journal record for <need> is not wired" refuse before any
  attempt. The HYP/LIT DAG tasks could only ever close SKIPPED.
- Wired path (mirrors the SPEC stage): `--wired-roles` now runs the role
  honestly — one `start_attempt` → one adapter run → journal record → the
  intake DAG task closes DONE SUCCESS (non-SKIPPED):
  - `hypothesizer` (HYP): `StudyContext` in, new typed `HypothesisOut`
    (hypotheses: id + statement + rationale, unique ids, ≥1) out; journaled
    via `record_hypotheses` (SPEC-record mirror).
  - `researcher` (LIT): runs the profile researcher role's literature tools
    through the planned surface exactly like executor tools (profile bare
    tool names resolved to exact `server:tool` surface ids; call args from
    the cassette / `--tool-call-args`; planned calls resolved BEFORE
    `start_attempt`), typed `LiteratureOut` (papers with explicit
    `identity_key` + `source_tool`) out; journaled via `record_literature`
    (tool results → `provenance="tool"` artifacts, executor-route mirror).
  - No invented citations: `record_literature` refuses (rc=4) unless every
    paper cites an exact journaled tool_call name AND its `identity_key`
    appears verbatim in that tool's result JSON — papers only from tool
    outputs.
- Fail-closed contracts (all preserved/typed): Need=true + role unwired →
  rc=5 (unchanged); wired role without the adapter output schema → rc=5
  (now also checked before any study work); wired role without the profile
  role/model/route (`hypotheses`/`literature` routes must wire exactly that
  role) → rc=5 (report-lane mirror); wired researcher without its literature
  tools on the tool surface → rc=3 (no blanket-allow); adapter instance
  without the wired role's model entry → typed `AgentError` → rc=4; stage
  agent/record failures refuse terminal rc=4 (stage retries are not re-driven
  by the CLI loop — same exposure as SPEC; `continue` refuses unfinished
  stage tasks, unchanged).
- Exact-5 gate fix (MCP review round drift): `PAPER_ANALYSIS_TOOLS` in
  `coscientist/tools/mcp_live.py` required exactly 4 tools, but the live
  `paper-analysis` server lists 5 — `get_papers_database_statistics` added
  (verified live 2026-10-25, `bench/lab/mcp/README.md`); exact-set tests
  cover both drift directions (missing + extra tool).
- Profiles: `research.v1.yaml` / `research-glm.v1.yaml` gain the
  `hypothesizer` / `researcher` roles and `hypotheses` / `literature` routes
  (budgets: 8 LLM calls / 32k tokens / 0-8 tool calls); the researcher role
  carries the literature allowlist (`search_papers`,
  `download_papers_from_search`, `find_papers_in_db`) — packaged
  `tool_catalog.json` bumped to version 2 with those tools (CLI
  `--tool-catalog-version` pin follows).
- Lifecycle: intake HYP/LIT stage tasks now carry the stage expected output
  (the catalog `artifact_present.check.output` name, same naming choice as
  SPEC) so the PI1b default existence gate binds to the task's own required
  outputs — a wired stage record closes through the same existence gate as
  SPEC instead of failing `output_not_submitted`.
- Schema additions are additive (new models + `_OUTPUT_MODELS` entries +
  wired-role names at the end of `coscientist/agents/schemas.py`; new prompt
  refs `role.hypothesizer` / `role.researcher` v1) to stay merge-clean with
  concurrent schema/prompt workstreams.
- DoD: `tests/cli/test_hyp_lit_wiring.py` (wired HYP happy path closes HYP
  non-SKIPPED; wired LIT happy path on a fake literature surface; invented
  citation + identity-key drift refuses rc=4; surface/role/route refuses);
  `tests/tools/test_mcp_live.py` exact-5 gate; `tests/profiles/` v2 catalog
  + role/route validation; unwired Need refusals unchanged
  (`test_planner_hypotheses_needed_refuses` / `test_planner_literature_needed_refuses`).
- Rebase review (2026-10-26, onto main @ 7fec16d — critic/reviewer + C1
  evidence-link merged in): `PaperRef.doi` / `PaperRef.url` field defaults
  carry `audit-ok[field_default]` markers (identity gate is `identity_key`,
  never doi/url). Known hardening candidate (advisory, kept as spec'd for
  now): `record_literature`'s `identity_key` presence check is a substring
  test on the raw `result_json` — harden later by parsing `result_json` and
  walking values for equality instead of substring. Semantic follow-ups
  (noted, not implemented here): (a) post-merge two wired-role validation
  styles coexist (`LlmAgentAdapter.can_run_role` vs the CLI
  `_wired_role_adapters` pre-check) — unify later; (b) the `continue` path
  does not carry hypothesizer/researcher adapters, so an open HYP/LIT stage
  refuses typed (AgentError, fail-closed) — decide explicitly later whether
  continue should re-drive wired need stages.

### fix(events): bare ensure_lease keeps the operator's lease TTL
- `StudyRuntime.ensure_lease()` no longer defaults to a 30s TTL that silently
  downgraded the operator's `--lease-ttl-s`: `tick()`'s bare renewal kept the
  row at +30s, so any LLM call longer than 30s inside a loop iteration let the
  lease expire and the next journal write was fenced off (`LeaseLost: epoch
  fencing rejected write`, observed live 2026-10-26, two-tasks-report-1).
  `ttl_s=None` (bare call) now preserves the store's current TTL.
- DoD: `tests/test_lease_lifecycle.py::test_bare_ensure_lease_does_not_downgrade_store_ttl`.


### CLI bounded recovery loop — executor retries via the core + one replan round [A4 / gaps №6-7]
- Gap: the core has retry machinery (`_fire_due_retries` in the tick loop,
  `Study.max_attempts`, `AttemptClosed.retryable/retry_at`) and the adapter
  has a one-round schema repair, but `coscientist run` was single-pass: the
  first executor error/failed attempt → `CliRefuse` rc=4, and a
  `PlanRefused` → exit with no second planner attempt. Live GLM runs died on
  transient model/tool failures a retry would survive.
- Executor retries are driven through the CORE's existing path — no parallel
  CLI retry machinery: a transient executor failure (`AgentError` /
  `ToolError` / `LlmError` / `run.error`, or a `record_executor_route`
  model-output contract refusal) closes the attempt with the core's
  `fail_attempt(retryable=True)` (journals `AttemptClosed.retryable=true` +
  `retry_at`); the driver then keeps ticking, and `_fire_due_retries` starts
  the next attempt under the `Study.max_attempts` / task-deadline / budget
  bounds the core already enforces. The CLI only (a) starts FIRST attempts
  for dispatchable tasks (never on tasks with prior attempts, so the core's
  `max_attempts` raise is unreachable from the driver), (b) executes each
  open attempt exactly once (`executed_aids`), (c) ticks until close.
  Budget-exceeded `AgentError` stays terminal (rc=4 — the budget gate never
  retries). Runtime-drift refusals (no executor / attempt row / task row)
  are pre-checked BEFORE dispatch and refuse terminal rc=4 without an
  adapter call — infra drift never burns the retry budget or surfaces as an
  rc=7 science failure. Failure details stay visible as `retryable_error`
  usage rows.
- One bounded replan round (adapter repair pattern at plan level): a
  `PlanDeriveError` or `accept_plan` refusal feeds its detail back via the
  new `StudyContext.plan_refusal_hint` for exactly ONE second planner call;
  a second refusal is terminal rc=3. Derive refusals do not finalize the
  study → the replan round runs IN PLACE on the same runtime (no reopen, no
  extra journal, `tz_spec` not re-called); `accept_plan` refusals finalize
  the study REFUSED (core fail-closed), so that replan round opens ONE
  fresh study `<study_id>-r1` which records the SAME validated SPEC output
  (one `tz_spec` call per run — no fabricated plan, `max_expansions`
  untouched). The summary reports the study that actually closed.
- Loop bounds (fail-closed, no new subsystems): recovery loop capped by
  `--max-ticks` (rc=6, unchanged), retries by `Study.max_attempts` +
  task/study deadlines + budget (core), replan by exactly-one-round; a
  scheduled-but-never-due retry cannot spin (tick cap test). Coder delivery
  stays single-dispatch with typed refuses (its job lifecycle owns retries).
- Exit semantics unchanged and now documented in `coscientist run --help`
  epilog: 0 only on `verdict=fulfilled`; 2 usage/config; 3 create/derive/
  accept refusal (after ≤1 replan); 4 agent failure outside the bounded
  retry (e.g. budget exhausted); 5 missing wiring; 6 `max_ticks` cap;
  7 non-fulfilled verdict (incl. retry exhaustion).
- DoD: `tests/cli/test_run_study.py` — transient-failure-then-success closes
  fulfilled with ≥2 `AttemptStarted` for the task + a `retryable`
  `AttemptClosed` journaled; persistent executor schema violations exhaust
  exactly `max_attempts=3` attempts and refuse typed rc=7; first-refusal-
  then-valid replan round passes (2 planner calls, round-2 payload carries
  `plan_refusal_hint`, `<sid>-r1` study closes fulfilled); derive-refusal
  (duplicate task keys) replans IN PLACE on the original study (no `-r1`
  journal, one `tz_spec` call, `PlanAccepted` on the original journal);
  runtime-drift refusals are terminal rc=4 with no adapter call and no
  retryable failure journaled; a never-due retry is bounded by `max_ticks`
  rc=6. Gates: pytest parity (identical pre-existing macOS sandbox failure
  set; 83 CLI tests green), ruff, mypy, lint-imports (21 kept / 0 broken),
  audit_heuristics `--vs-main` 0.
### Web lane — start studies from the API + webdrive driver [gap 3 / 11]
- Gap: the old system had a full web lane (chat over HTTP+WS drove studies:
  prompt POST → status events → final response; benchmark scenario `web` ran
  through `webdrive.py`). The new API was read-only + cancel — no way to
  START a study, no driver.
- `POST /studies/{id}/run` (`coscientist/api/run.py`): accepts `task_text`
  plus the exact CLI `run` config refs (llm-config path, catalog path,
  profile name + tool-catalog pin, model pins, tools-json / mcp-tool-bundle
  + call-args paths, coder flags, thresholds, budgets, hitl-mode, rng-seed).
  Every field required — explicit `null`/`[]` where the CLI flag is optional
  and absent; no API-side defaults. The service builds an
  `argparse.Namespace` 1:1 and calls `coscientist.cli.run._async_main` —
  the whole CLI path (loaders, fail-closed validation, `run_study`) stays
  single-source in the CLI; the API adds no second validation path (a thin
  seam, so the in-flight retry-loop change to `run_study` lands under it).
- Status/final surface is the journal itself (no new event types): the
  existing `WS /studies/{id}/stream` already streams every event and
  `StudyFinalized` is the final verdict/response event. New
  `GET /studies/{id}/run` returns run status and, once terminal, the
  captured `run_study` summary (phase, verdict, usage).
- Fail-closed: unknown config refs → typed `config_ref_not_found`; CLI
  refusals (unknown profile, bad pins, intake refuse) → typed
  `study_run_refused:<detail>` (400, never 500); crash pre-journal →
  `study_run_failed` (500); concurrent second run on the same study →
  `study_run_in_progress`; re-run of an existing journal →
  `study_journal_already_exists` (reservation is loop-atomic: sync section).
- Cancel is run-aware: `bind_app_deps` wraps cancel so an API-started live
  run is interrupted (task cancelled, lease released) before the journal
  cancel runs — `POST /studies/{id}/cancel` mid-run now yields
  `StudyCancelled` + `StudyFinalized(trigger=cancel)` on the stream and
  `run_status=cancelled`.
- `coscientist.api.serve` (`python -m coscientist.api.serve`): uvicorn entry
  with explicit required flags (data-root, catalog, host, port, stream poll
  interval, run wait bound); uvicorn is a runtime-only dep via `uv --with`
  (repo dependency surface unchanged).
- Driver `bench/lab/driver/webdrive.py` + launcher
  `bench/lab/driver/run_web.sh`: the old webdrive contract on the new
  transport — POST run (old WS chat_message send), WS status events,
  `StudyFinalized` + GET run as final_response, overall timeout, JSON-lines
  event log, summary JSON `{elapsed_s, n_events, final}`, exit 0 only on
  fulfilled. httpx/websockets via `uv run --with` only.
- import-linter: three forbidden contracts (mcp / httpx / invoke_tool entry)
  gain one documented ignore each for the single sanctioned edge
  `coscientist.api.run -> coscientist.cli.run` — every api → tools/mcp/httpx
  chain runs through the CLI reuse edge, never api's own code; the new api
  modules are listed in the ingest-gate/lifecycle source contracts (their
  auto-coverage tests updated accordingly).
- Tests: in-process E2E (`tests/api/test_run_endpoint.py`) with the cassette
  LLM surface — study started via API, events streamed over the app's WS,
  verdict event, run summary, typed refusals (unknown profile, missing ref,
  concurrent run) and mid-run cancellation; driver pure-helper contract in
  `tests/test_webdrive.py`. Live-verified once end to end via `run_web.sh`
  (real uvicorn + websockets + local LLM endpoint: 24 events,
  `verdict=fulfilled`, exit 0).
- Review fixes (k=4, same branch): start-timeout can no longer orphan a
  journal — the timeout path re-checks the journal and, when `StudyCreated`
  is visible, journals the cancel via `post_study_cancel` (terminal on the
  stream, `run_status=cancelled`) instead of refusing; re-run refusal is
  keyed on the `StudyCreated` EVENT, so a run interrupted between
  store-open and `create_study` leaves a re-runnable stub (a bare empty
  `events.db` is no longer a permanent zombie). webdrive: POST timeout is a
  required flag (`--post-timeout-s`) set strictly above the server
  run-wait bound in `run_web.sh`; WS reconnects resume
  `?after=<last seen seq>` with a replay guard (no duplicate JSONL lines,
  no inflated `n_events`); the retry loop covers `OSError`/`TimeoutError`
  (dial refusals, open timeouts, mid-stream resets), not just
  `WebSocketException`. Re-review (k=2, both LOW): the single-flight slot is
  now claimed synchronously BEFORE the journal-await (F5 — with the old
  check-then-await shape a second same-sid POST could slip through while
  the first was suspended in `_study_created`; the claim is released on any
  pre-task failure, and two racing same-sid POSTs deterministically yield
  one start + one `study_run_in_progress`); the start-timeout cancel-wait
  is bounded by `run_wait_s` with a typed `study_run_interrupt_timeout`
  refuse instead of racing the journal cancel on a lease the stuck run may
  still hold (F6).
### Recovery lane — `coscientist continue` (gap №4: checkpoint/restore on the event-sourced core) [M1]
- Design (checkpoint-equals): the old system's checkpoint+session-restore maps
  onto the new architecture's own terms — **the journal IS the checkpoint**
  (`events.db`, SQLite WAL, `synchronous=FULL`, one DB per study, plus the
  lease row owner/epoch/nonce/expires_at). There is deliberately no parallel
  snapshot subsystem. **Restore** = a new process on the same
  `data_root`/`study_id`: `open_runtime` replays every event (tasks, attempts,
  graph, verdicts rebuilt by `_project_local`) and takes the lease — a killed
  predecessor's lease is waited out (bounded), takeover bumps the epoch and
  rotates the nonce, so a zombie old writer is fenced on its next append
  (`SqliteEventStore._fence` → `LeaseLost`). **Continue** = `recover()`
  (crashed open attempts closed retryable, record-path attempts finished from
  CAS, pending expansions/foreach/loops resumed) plus re-drive of unfinished
  work through the SAME dispatch helpers as `run` (`_require_attempt` →
  executor-route → `record_executor_route`; coder jobs via
  `_dispatch_coder_task`). What must never happen — double execution of a
  closed attempt — is impossible by construction: `Executor.record` writes
  require the attempt open (`AttemptClosedError`), the store rejects appends
  after `StudyFinalized` (`StudyClosedError`), and stale epochs cannot write.
- `coscientist continue <data-root> --study-id …` (flag surface mirrors `run`
  where applicable; study-creation flags and `--hitl-mode` intentionally
  absent — the journaled `Study.deadline`/`HitlPolicy` are authoritative and
  continue never extends the deadline). New `--lease-wait-s` bounds the
  takeover wait. Fail-closed: missing/corrupt journal or foreign study id →
  code 2; finalized study / no accepted plan / non-re-drivable spec / pending
  stage task → code 3; live lease past the wait window or lost takeover race →
  code 5; max_ticks → 6; closed but verdict ≠ fulfilled → 7 (same codes as
  `run_study`).
- Re-drive fidelity: journaled `TaskSpec` → `TaskSpecDraft` inverse
  (`draft_from_task_spec`) verified by `spec_redrivable` — draft-carried
  fields must roundtrip through `_to_task_spec` unchanged, and core-owned
  behavioral fields (foreach/loop material, budget/deadline shares, method,
  tests intent, tolerated failed deps, `InputSpec` bindings) typed-refuse
  (the executor-route prompt cannot carry them; no silent semantics drop).
  Deterministic TaskCreated enrichment (e.g. `sandbox_check_limits` frozen
  from the catalog) does NOT block re-drive.
- `continue` stays thin for composition with the retry-loop lane: one
  re-drive pass per round (`iter_dispatchable_tasks` first, then open
  driver-less attempts — under the fenced lease those can only be tick-fired
  retries), tick only when idle; tasks come from the journal only (never
  re-planned), so a resumed study cannot grow duplicate EXP-n keys.
- Lab driver (legacy benchmarks `recovery`/`recovery2` on the new stack):
  `bench/lab/driver/recovery_drive.sh` — run in its own process group, poll
  the journal for the first EXP `ResultRecorded`, SIGKILL the group
  mid-study (the bundled fake LLM holds the EXP-1 call until the kill marker
  → deterministic kill point), `coscientist continue`, then
  `recovery_check.py` verifying from the journal alone: unique task keys,
  one `AttemptStarted` per attempt, no `ResultRecorded` after
  `AttemptClosed`, no duplicate results per attempt, exactly one
  `StudyFinalized` with verdict, ≥2 EXP tasks each with one success
  `TaskDone`.
- Tests: `tests/cli/test_continue_cmd.py` (unit, fakes — kill-simulation
  resume with no duplicate EXP tasks / no re-executed closed attempts,
  finalized/foreign/corrupt/missing-journal refuses, live-lease refuse,
  fencing proof that the old epoch can neither renew nor append after
  takeover, redrive fidelity roundtrips) and
  `tests/cli/test_continue_e2e_slow.py` (slow, real subprocesses + SIGKILL).

### Report lane — scientific report + ГОСТ ТЗ/НИР from the graph projection
- Gap: `coscientist run --wired-roles ["report"]` refused (the DAG REPORT
  placeholder was cancelled when unwired and hard-refused when wired), so a
  successful run emitted only the technical `report.md` from finalize — no
  scientific report and no official documents. Reporting is now WORKING:
  after a fulfilled run the CLI emits `report.science.md`,
  `tz.gost-19.201-78.md` (ТЗ по ГОСТ 19.201-78) and
  `nir.gost-7.32-2017.md` (отчёт о НИР по ГОСТ 7.32-2017) into the run's CAS
  (`<data_root>/<study_id>/arts/cas/…`), beside the finalize `report.md`
  (which stays, unreplaced). Markdown only — no DOCX dependency exists in
  pyproject (checked; python-docx absent → skipped per policy).
- Single source: `coscientist/reporting/graph_digest.py` builds a COMPACT
  Russian digest in code from the journal projections
  (`store.get_projection("graph_nodes"/"graph_edges")` — never the raw
  transcript, never model-invented numbers): study goal/request, questions,
  hypotheses (+verdict: «подтверждена»/«опровергнута»/…), experiments
  (+attempts/outcomes), evidence, artifacts, claims, conclusions, criteria,
  constraints and the edges between them, rendered as ordinal labels
  («Гипотеза 1 проверяет…»); internal node ids never appear. Missing data
  renders «не получено»; a «Сводка» block adds code-computed counts. All
  three documents render from this one digest.
- Honesty gate (`coscientist/reporting/honesty.py`): a number may appear in
  generated prose ONLY if it exists in the digest (numbers-in-numbers-out;
  identifiers glued to letters — `LD50`, `ГОСТ 7.32-2017` — are names, not
  numeric claims); letter+digit tokens are legal only when digest-sourced
  (so `LD50` passes, invented `H1`/`TASK-3`/`cn_ab12…` refuse); agent names
  and tracker statuses refuse; markdown ordered-list markers are structure,
  not data. Enforcement: post-generation validator over the digest's number
  inventory per prose slot, one bounded repair round (the adapter's
  `_complete_validated` pattern — violation fed back once), then typed
  `CliRefuse` code 8. Refuted/postponed hypotheses are stated AS-IS;
  «не получено» for missing data — both tested.
- GOST documents (form/register from legacy CoScientist as the reference
  standard, implemented fresh): ТЗ 19.201-78 — code-built skeleton
  (sections 1–7 in numeric order; задачи/критерии/состав работ printed
  verbatim from the graph; model fills impersonal prose for 1/2/4/6 per the
  old tz_spec register). НИР 7.32-2017 — code-built structure (титул with
  `<...>` placeholders for org requisites, реферат+ключевые слова,
  содержание, Введение, Методика / per-hypothesis / Обобщённые результаты
  sections, Заключение, источники from paper artifacts, приложения from
  artifact facts; model fills `NirProse` prose per the old nir_report
  register). Scientific report — fixed headings (цель, подход, результаты,
  обсуждение, ограничения, следующий шаг, материалы/воспроизводимость) with
  code-appended artifact appendix.
- Wiring (`coscientist/cli/run.py` + new `coscientist/cli/report_lane.py`):
  the lane runs AFTER the tick loop closes (the graph is complete only
  post-finalize; I3 closes journal writes, so documents are CAS artifacts and
  the lane writes NO journal events — verdict semantics unchanged, report
  content can never flip a verdict). Unwired `--wired-roles` behavior is
  byte-identical (the DAG REPORT placeholder still cancels with
  «report agent not wired»); wired, the placeholder closes with «report
  deferred to post-finalize lane» and the lane runs on verdict=fulfilled
  only. Fail-closed deps checked before any study work: LlmClient passed +
  profile `report` role (model — `glm-4.7` in research-glm, `gpt-4.1` in
  research; both packaged profiles gained the role); GLM-4.7 cheap policy —
  compact prompts, ≤ 2 LLM calls per document (initial + one repair),
  `Budget.deadline` checked before every call.
- DoD: `tests/reporting/test_graph_digest.py` (labels/edges, number
  inventory incl. computed counts, «не получено», machinery kinds skipped,
  foreign-study/malformed rows refuse), `tests/reporting/test_honesty.py`
  (untraceable → refuse, traceable/LD50/ГОСТ/list-markers pass, refuted
  wording passes, «не получено» passes, ids/agent names/statuses refuse),
  `tests/reporting/test_gost_documents.py` (ТЗ section order + verbatim
  graph facts; НИР outline/markdown incl. references from artifacts;
  science structure + appendix), `tests/cli/test_report_lane.py` (e2e
  cassette: three documents land in CAS beside `report.md`, usage row
  recorded; invented number → repair → typed refuse code 8 with no artifacts;
  repair round recovers). `test_run_study.py` report tests updated to the
  new seam (no client → code 5; no profile report role → code 5).

### CLI task inputs — `--task-input` through the ingest gate [M1]
- Gap: core/API already had typed inputs (`InputSpec`,
  `POST /studies` `inputs[]` → `register_user_input_artifacts` → CAS
  `ArtifactRegistered`, `producer=user`), but `coscientist run` hardcoded
  `inputs=[]`, so bench scenarios (hypothesis_check / file_handoff) smuggled
  CSV paths inside prompt text. Data is now a first-class CLI input.
- `coscientist run` gains a repeatable `--task-input PATH` flag.
  `_load_task_inputs` reads each file into a typed `UserInput` (name = file
  basename, raw bytes, `source_uri` = given path, media type from a fixed
  suffix map — `.csv/.tsv/.json/.txt/.md/.pdf`, unknown suffix keeps the
  `application/octet-stream` default; content is never sniffed) and passes
  them to `create_study(inputs=..., user_put_capability=...)` — the SAME
  ingest-gate registration path as `POST /studies` (no duplicated gate). The
  capability is minted via new `api.studies.mint_study_user_put_capability`
  / `release_study_user_put_capability` (import-linter keeps the mint inside
  the intake module; `coscientist.cli` still cannot import `_ingest_gate`);
  `post_studies` semantics unchanged.
- Study-level binding reuses the API shape exactly (design decision studied):
  user inputs are NOT `TaskIntent` fields — `InputSpec` has no user-input
  kind and `POST /studies` binds them study-wide (`Study.user_input_ids`), so
  the CLI wires the registered rows to every derived task, mirroring core's
  `study_user_input_bindings` on every attempt. The LLM never echoes input
  names (no guess/failure mode); `derive_plan` needs no new parameter.
- `agents/schemas.py`: new `RegisteredInput` (name + `artifact_id` + `uri` +
  `media_type`; journal-sourced readback of `ArtifactRegistered`
  `producer=user`); `StudyContext.inputs` (planner sees names+refs instead of
  prompt-path guessing) and `ExecutorRouteIn.inputs` (executor consumes file
  data by CAS ref); the adapter includes them in the executor user payload.
- Fail-closed: missing `--task-input` file / duplicate artifact names
  (basenames) / unreadable file → typed `CliRefuse` code 2 before any study
  or LLM work; after `create_study` a journaled `StudyRefused` (input limits,
  decode, duration) refuses with kind+reason (code 3) instead of a confusing
  `start_attempt` failure; registered-count mismatch vs `--task-input` files
  refuses (code 4, no silent drop).
- DoD: `tests/cli/test_run_inputs.py` — one-CSV run journals
  `ArtifactRegistered(producer=user)` + `StudyCreated.user_input_ids`,
  attempts bind `user:<name>` (source=real), planner + executor request
  payloads carry the refs; refusal paths (missing file, duplicate names,
  duration refusal, full-argv flag). [DESIGN_M1 §9](docs/DESIGN_M1.md),
  [CAPABILITIES](docs/CAPABILITIES.md).
### Prov — executor-route output provenance policy (fail-closed) [M1]
- Gap: ``record_executor_route`` journaled every output as a trusted
  ``producer="agent"`` artifact — nothing distinguished "bytes are the
  attempt's recorded ``tool_call`` result" from "model invented the value";
  ``artifact_exists`` verified existence only, so a run could "fulfill" a
  calculation question with invented numbers under a tool-id name.
- Stamping (``domain.Artifact.provenance`` via ``artifacts.put`` +
  ``cli/journal.record_executor_route``): journal content-origin stamp derived
  from the attempt's recorded ``AgentRun.tool_calls`` (never model claims) —
  tool_call result artifacts get ``provenance="tool"`` with the tool name +
  attempt-scoped call ref on ``produced_by``; ``ExecutorRouteOut.values``
  echoes get ``provenance="agent"`` / visible ``producer="agent"``. The stamp
  never changes ``producer``/trust semantics (``put`` stays the agent path).
  ``ArtifactRegisteredPayload`` accepts the optional field (writer-omitted for
  pre-existing v1 journals; no schema version bump — additive optional).
- Policy (``checks/evaluate`` + ``checks/kinds/rubric``): a required output
  named after a ``tools_hint`` tool id (intent-derived plans name outputs
  exactly so) must be backed by a ``provenance="tool"`` artifact from the same
  attempt — a values echo (or unstamped legacy artifact) under a tool-id name
  FAILS typed with detail ``provenance_violation:<name>``. Enforced in the
  default ``artifact_exists`` evaluation, the plan-authored criteria path, and
  the derive-attached ``OutputSpec.rubric`` path (production catalogs without
  experiment defaults). Model-authored values remain allowed only under
  non-tool output names with visible ``producer="agent"``.
- DoD: ``tests/cli/test_journal.py`` (stamps from recorded tool_calls) +
  ``tests/checks/test_default_artifact_binding.py`` (tool-backed passes;
  values-echo under tool name fails typed; agent-named values pass with
  ``producer="agent"``; per-output rubric enforces the same policy).
### Coder wired end-to-end: router resolution + CLI dispatch [G3 / gap №1]
- Router (G3/#65 seam): `ToolRouter.attach_coder_route(backend)` attaches the
  explicit coder tool binding (backend validated by `resolve_coder_backend` —
  `bwrap` Linux/CI hard sandbox or `local-python` macOS/dev; never a platform
  guess; typed refuse otherwise, single-attach). `resolve(by_name="coder")`
  returns `Resolution(status="coder")` with the route attached and
  `tool_missing` without — the typed status instead of the old blanket
  `tool_missing until G3/G6`. `StaticToolRouter` (cassette) and
  `LiveCliToolSurface` (reserved `--tool-call-args` key `"coder"`, empty args)
  expose the same binding so the planner `tools_hint` enum carries it.
  Journalled `ToolResolved(status="coder")` remains a #65 payload-Literal hard
  dep (this path journals `Job*` + `ToolCalled` + `EffectIntended`/`EffectDone`).
- Intent marker: code-execution delivery is `route="experiment"` +
  `tools_hint=["coder"]` (no new TaskSpec route value — the hint IS the
  surface-visible binding; outputs derive named `coder` exactly like tool
  ids). `derive_plan` refuses mixed hints (`coder` is exclusive — split MCP
  tool calls into another task).
- CLI dispatch: `--coder-backend {bwrap,local-python}` (explicit operator
  allow for the write-effect coder tool; absent → typed refuse code 5, no
  stub science) + `--coder-jobs` JSON (per task_key: `argv`, `inputs` (UTF-8
  str), `env`, `outputs[{name,source,media_type}]`, `limits` — exactly the 9
  `limits_from_check` budget keys; argv/limits are never invented). Dispatch
  goes through `invoke_coder_for_study` → `invoke_tool(coder)`; Job*
  lifecycle events flow through `InMemoryJobStore` into the study journal
  (sync `JobJournal` → writer queue, drained before dispatch returns); job
  outputs are registered by the coder backend itself (sandbox producer, job
  artifact machinery — model-invented values never enter); the attempt is
  recorded with `AgentTaskOutput` from the job outcome. Non-succeeded
  outcomes are typed refusals (retryable surfaced).
- What stays fail-closed: agents still cannot call coder through the executor
  tool loop (adapter refuses `status != "tool_found"`); bwrap backend keeps
  the hard Linux preflight (bwrap + hard isolation + cgroup delegation,
  `allow_cgroup_degraded` forbidden); local-python keeps no-net-isolation
  honesty (`allow_network=False` refused); by_query/alembic resolution stays
  `tool_missing` (G6).
- DoD: router/derive/CLI-surface tests +
  `tests/cli/test_run_study.py::test_coder_task_local_python_success`
  (echo-style local-python job → SUCCESS verdict, `JobSubmitted`/
  `JobStatusChanged`/`Effect*`/`ToolCalled`/`ResultRecorded` journaled,
  `producer="sandbox"` artifact with the real stdout). Network clone
  (alembic-style) intentionally not unit-tested (no network in CI).
- import-linter: one narrow ignore `tools.router -> tools.coder.local`
  (router exposes only the route Literal/validation; coder machinery stays
  CLI-dispatch-only). lint-imports 21 kept / 0 broken.

### Coder local-python backend (macOS/dev) [M3]
- Explicit coder execution backend selection — `resolve_coder_backend` refuses
  (typed, fail-closed) anything that is not exactly `bwrap` (Linux/CI hard
  sandbox) or `local-python`; never a platform guess. `invoke_coder_for_study`
  now requires `backend=...`.
- `run_local_coder_job`: subprocess python in a cwd-isolated workspace dir
  with wall-time deadline (folded into JobStore deadline → same `timed_out`
  retryable semantics), output size caps (`OUTPUT_EXCEEDED` → failed), exit
  code + capped stdout/stderr as typed artifacts. Best-effort
  RLIMIT_AS/RLIMIT_CPU. **No network/fs isolation claims**: artifacts record
  `net_isolation=none`; `allow_network=False` requests are refused on this
  backend (network must stay reachable for alembic_nomcp). bwrap stays the CI
  gate unchanged.
- DoD: `tests/tools/test_coder_local_backend.py` (selection refuse, success,
  cwd isolation, non-zero exit, timeout kill, output cap).
  [DESIGN §6.4](docs/DESIGN.md), [CAP #11](docs/CAPABILITIES.md).
### Reduction D — bench review advisories (PR #205) [reduction]
- ``bench`` CLI loaded/validated the manifest twice; ``load_tasks`` now accepts the
  already-loaded manifest and the CLI passes it through (typed refuses unchanged).
- ``bench/tasks/{web,recovery,recovery2}.yaml`` headers: only the future live adapters
  (items 12/13) will typed-refuse driver tasks — reworded accordingly.

### Reduction C — drop empty ``web`` package from lint/type scope [reduction]
- ``web/`` has no content (only ``__init__.py``): removed from mypy files,
  import-linter root_packages and the forbidden-module contracts mentioning it,
  until it gains content. ``bench`` kept in mypy scope (has content).

### Reduction B — tool_resolution enum single source of truth [reduction]
- ``ToolResolutionStatus`` Literal is now the only place the enum body is authored;
  the three public cross-check helpers (canonical type string, schema-text extractor,
  packaged loader) collapsed into ``assert_tool_resolution_matches_graph_schema``
  deriving the expected ``enum(...)`` from the Literal. Fail-closed mismatch refuse kept.

### Reduction A — drop dead exact-catalog-resolve probe [reduction]
- `coscientist.cli.run` always shipped `coscientist.tools.resolve` (wheel package), so the
  `find_spec` importability probe and its duplicate code=5 refuse path were dead; removed.
  `_assert_profile_tools_on_surface` now refuses unconditionally (code=3, no blanket-allow).
- DoD: `tests/cli/test_run_study.py::test_assert_profile_tools_on_surface_refuses_unconditionally`.
### PI1b — default artifact_present binds to the task's outputs [M1]
- Live GLM two_tasks on 105c269 closed partial only because both exp tasks
  were PARTIAL: empty criteria → ``catalog.defaults["experiment"]`` attaches
  ``artifact_present`` whose ``CodeCheck.output`` is a catalog literal, while
  intent-derived outputs are named after tool ids → existence unmet.
- Fix (``coscientist/checks/evaluate.py``): a default-attached
  ``artifact_exists`` binds to the TASK's own required ``expected_outputs``
  (every one must have a registered artifact owned by the attempt); the
  template supplies kind semantics only. Plan-authored criteria keep the
  template-pinned output (single-source prereg). Tasks with no required
  expected outputs keep the legacy template-literal lookup — recorded FAILED
  ``output_not_submitted`` on absence (never a silent pass, never a raise:
  base flows legitimately submit undeclared outputs and expect a recorded
  FAILED, e.g. foreach quorum bodies; raising would suppress
  ``ResultRecorded``). SPEC unchanged (its single output already binds).
- ``tests/cli`` no longer needs the ``_cli_catalog`` re-pin — deleted; the
  stock ``minimal_test_catalog`` now closes intent-derived studies
  fulfilled. DoD: ``tests/checks/test_default_artifact_binding.py`` +
  ``tests/cli/test_run_study.py`` happy path (exp tasks SUCCESS, verdict
  fulfilled) on the stock fixture catalog.

### PI1a — intent questions are text-only; derivation assigns final_type='artifact' [M1]
- Follow-up to PI1 after a live GLM two_tasks run: the model naturally picks
  ``final_type='both'`` for delivery questions, and ``PlanIntent`` cannot
  express measured criteria anyway (PI1's deliberate fail-closed refusal).
  Rather than prompt guidance, the intent schema now expresses only what the
  system can deliver today: ``QuestionIntent`` is ``text`` only;
  ``derive_question_drafts`` assigns ``final_type='artifact'`` to every
  question and the verdict/both refusal branch is deleted.
- Verdict/both support returns as a separate typed slice (measured-criterion
  linkage in intent → derived ``RequirementRef`` rows); see PI1 risk note.
- DoD: ``tests/agents/test_derive.py::test_final_type_is_always_artifact_regardless_of_catalog``.

### PI1 — planner typed intent; core derives the frozen plan [M1]
- Planner LLM output is now minimal typed intent ``PlanIntent`` (per task:
  goal / route / depends_on / tools_hint / optional key+title; per question:
  text + final_type; hypotheses/literature Need unchanged) —
  ``coscientist/agents/schemas.py``. No expected_outputs / rubrics / criteria /
  naming in LLM hands.
- New deterministic derivation ``coscientist/agents/derive.py``
  (``derive_task_specs`` / ``derive_plan``, typed ``PlanDeriveError``):
  expected_outputs named exactly after the delivering tool id (one per
  tools_hint, JSON, auto ``artifact_exists`` rubric), criteria defaulted empty,
  system task keys ``t{N}`` (planner slugs optional, unique, slug-shaped),
  question ids ``q{N}`` with every task addressing every question; verdict/both
  questions refuse fail-closed (never invent measured criteria). Lives in the
  agents layer because ``TaskSpecDraft``/``PlanOut`` are agent role schemas —
  core must not import agents.
- Adapter planner request schema is context-typed
  (``plan_intent_model_for``): ``tools_hint`` becomes a JSON-schema enum of the
  study's ``ctx.tool_ids`` — schema-checked, so the hand-written "PLAN
  CONTRACT" prompt block is deleted entirely (planner body = one-line role
  body, role.planner v2). ``run_study``: planner → PlanIntent → derive_plan →
  existing accept_plan backstop (all gates unchanged);
  ``tests/cli`` fixtures move to a ``_cli_catalog`` whose ``artifact_present``
  pin follows the derived tool-id naming (single-source rule kept).
- DoD: ``tests/agents/test_derive.py`` (naming / auto-rubric / empty criteria
  / verdict refusal / deps wiring / derived plan passes accept_plan unchanged),
  adapter enum + prompt-shrink tests; audit_heuristics 0.
- Production catalogs must re-pin ``artifact_present.check.output`` to a
  tool id (derived plans name outputs after tool ids, never a hand-named
  ``main``); the test fixture shows the pattern (``_cli_catalog`` in
  ``tests/cli/test_run_study.py``).

### G14a — DESIGN §16 / M0 counts vs schema v0.8 [M1]
- Pin DESIGN §16 + M0 note + RESEARCH_MODEL principle 6 to packaged schema
  **v0.8 / 15 nodes / 19 edges** after G17+G18 (`tool_call` + `mcp_exchange`).
  Prose counts already matched tip from #189; this closes the G14a docs gate
  with an explicit version pin + fail-closed count test.
  [plan G14a](docs/plan/graph.md).

### G17 — fold ToolCalled → tool_call via G7 [M1]
- Schema `tool_call` kind (graph_schema **v0.8**; restack after G18 took v0.7 + `mcp_exchange`)
  + `graph_fold.tool` builders through `graph_node_upserted`; projection `apply_to_map`
  wires `ToolCalled` after TG-Call. DESIGN §2/§16 + RESEARCH_MODEL → **15 nodes / 19 edges**.
- Attrs flatten DESIGN §10 `ToolCalledPayload` (`ref` + outcome); `error_kind` only
  when `outcome=error` (no invent null on ok).
- [DESIGN §10/§16](docs/DESIGN.md), [CAP #19](docs/CAPABILITIES.md?plain=1#L28),
  [plan G17](docs/plan/graph.md) / [#88](https://github.com/Naramerk/repo/issues/88).

### RC7r — HITL reject → no PlanAccepted [M4]
- Core rule only: plan-gate HITL reject (`plan` / `plan_approval` / `hypotheses`) stamps `status=rejected` on the HITL request row and `accept_plan` refuses with `hitl_reject_blocks_plan` (no `PlanAccepted`). HITL caller remains #52.
- DoD: `tests/test_rc7r_hitl_reject.py::test_reject_writes_no_plan_accepted`. [CAP #2](docs/CAPABILITIES.md); [plan RC7r](docs/plan/research-cycle.md) / [#103](https://github.com/Naramerk/repo/issues/103). Stacks on RC6 (#102 / PR #188).

### RC6 — selected hyp referenced by ≥1 task [M4]
- `assert_plan_refs_selected_hyp` / `assert_foreach_items_include_selected`: after RC5 artifact validate, the selected hyp id must appear on a plan task (`foreach_item`) / in HYP-n foreach expand `items` (typed refuse `selected_hyp_ref_required`).
- Foreach materialization over canonical `HYP-n` sources runs the gate after item selection; non-`HYP-n` of_task unchanged (PR10 fixtures).
- DoD: `tests/test_rc6_selected_ref.py::test_plan_requires_selected_hyp_ref`. [CAP #2](docs/CAPABILITIES.md); [plan RC6](docs/plan/research-cycle.md) / [#102](https://github.com/Naramerk/repo/issues/102). Stacks on RC5 (#101 / PR #183).

### R16a.4 — reporting format test split [M5]
- Split multi-scenario format tests (json/md/html/nir) to one-assert-scenario-per-test; no bundled multi-path cases. Test-only; no product behavior change. [CAP #16](docs/CAPABILITIES.md); [DESIGN §9/§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a.4` / [#122](https://github.com/Naramerk/repo/issues/122).

### MCP-J1 — McpRequestRecorded / McpResponseRecorded (#78) [M1]
- Event types + fail-closed payloads (`call_id` / `server_name` / `tool` / `attempt_id` + request `args_digest` / response `content_digest`+`is_error`).
- `tools.mcp_adapter.McpBoundTool` optional `McpCallJournal` hooks around `call_tool` (request before RPC; response only when a `CallToolReply` returns; transport errors leave response unjournaled — no invent).
- Unlocks graph **G18** fold. Parity: [DESIGN §6.0](docs/DESIGN.md#60-тонкий-mcp-клиент-coscientistmcp-m2-prep-a7), [docs/plan/graph.md G18](docs/plan/graph.md), [#78](https://github.com/Naramerk/repo/issues/78)/[#89](https://github.com/Naramerk/repo/issues/89).

### R16a.3 — NIR format=nir + config title [M5]
- `format=nir` via `reporting/nir.py`: body = core `render_markdown_report(MinimalReport)`; framing title from required `ReportFormatConfig.nir_title` (no hardcoded title / no second section renderer). Media `text/markdown`. [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [DESIGN §9/§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a.3` / [#121](https://github.com/Naramerk/repo/issues/121).

### R16a.2 — ReportInputRefuse single path [M5]
- `ReportRenderInput` construct and `parse_report_input` raise typed `ReportInputRefuse` (wrap ValidationError); no bare ValidationError-only refuse path. Tests use `match=` on raises. [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [DESIGN §9/§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a.2` / [#120](https://github.com/Naramerk/repo/issues/120).

### RC4 — HYP-n when `Study.require_hypotheses` [M4]
- When `plan.hypotheses.needed` (forced true by `require_hypotheses` via G4), `accept_plan` refuses unless the plan includes ≥1 canonical `HYP-n` task (`hyp_n_required`).
- `is_hyp_n_task_key` lives in leaf `need` (no `need→expand_validate` edge); shared `task_kind_for_route` in expand_validate; `accept_plan` assigns `TaskKind.HYPOTHESES` for hyp routes.
- DoD: `tests/test_rc4_hyp_n.py::test_require_hypotheses_forces_hyp_n`. [plan RC4](docs/plan/research-cycle.md) / [#100](https://github.com/Naramerk/repo/issues/100).

### TG-Call ToolCalled on invoke_tool (#88) [M1]
- Event type ``ToolCalled`` + fail-closed ``ToolCalledPayload`` (DESIGN §10: ``ref`` / ``outcome`` / ``error_kind`` / ``duration_ms``).
- ``invoke_tool`` journals ``ToolCalled`` on successful and failed ``tool.call`` (not on pre-call deny / EffectDone restore).
- Sequenced after ``ToolResolved`` on the catalog resolve→invoke path; unlocks graph **G17** fold. Parity: [DESIGN §10](docs/DESIGN.md#10-наблюдаемость), [docs/plan/graph.md G17](docs/plan/graph.md), [#88](https://github.com/Naramerk/repo/issues/88).

### G8 — lifecycle graph writes via G7 writer [M1]
- `plan/lifecycle.py` `create_study` / `accept_plan`: study, question, criterion
  `GraphNodeUpserted` and decomposes `GraphEdgeAdded` go through
  `graph_node_upserted` / `graph_edge_added` (registry attrs + digest idem).
  No second hand-rolled envelope path. [DESIGN §16](docs/DESIGN.md#16-научная-модель-граф),
  [CAP #19](docs/CAPABILITIES.md?plain=1#L28), [plan G8](docs/plan/graph.md) / [#84](https://github.com/Naramerk/repo/issues/84).

### M5 API — data_root composition into create_app (#153)
- `bind_event_store_for` + `bind_app_deps`: wire `DataRootStudyService` + SQLite
  `EventStore` + `bind_cancel_study` into `AppDeps` (clock/poll/sleep required).
- Routes map `StudyNotFound`→404 / `ApiRefuse`→400; WS stream passes `study_id`
  into `list_study_events`; stores closed after HTTP/WS use.
- Contract tests: TestClient + real `data_root` (not only fakes).
- `coscientist.api` package init does **not** re-export FastAPI `create_app` /
  `AppDeps` / `bind_app_deps` (import from `coscientist.api.app`) so `.[mcp]` /
  ingest-tar does not require `fastapi`.
- [DESIGN §9](docs/DESIGN.md#9-api-для-веба), [CAP #19/#21](docs/CAPABILITIES.md).

### RC8 — question-path verdict matrix [M4]
- DoD: `tests/test_rc8_question_verdict.py` — one test per RM §4.1 / research-cycle RC8 matrix row; asserts `(verdict, rule)` via sole `hypothesis_verdict` (no refmodel). Tail rows counted / validity_failed / excluded-only stay distinct. [plan RC8](docs/plan/research-cycle.md) / [#104](https://github.com/Naramerk/repo/issues/104).

### RC5 — hypotheses artifact validate (≥3; one selected) [M4]
- `validate_hypotheses_artifact`: ≥3 rows; each id/statement/prediction/rank + strict bool `selected`; exactly one `selected=True` (typed refuse `hypotheses_lt_three` / `hypotheses_no_selected` / `hypotheses_selected_not_unique`).
- Foreach materialization over canonical `HYP-n` sources runs the same gate before expand (`ExpansionRefused`); non-`HYP-n` of_task unchanged (PR10 fixtures).
- DoD: `tests/test_rc5_hypotheses_artifact.py::test_rejects_lt_three_or_no_selected`. [CAP #2](docs/CAPABILITIES.md); [CAP #16](docs/CAPABILITIES.md?plain=1#L25) report consumers of hyp projection; [plan RC5](docs/plan/research-cycle.md) / [#101](https://github.com/Naramerk/repo/issues/101).


### G18 — fold Mcp*Recorded → mcp_exchange [M1]
- Schema kind `mcp_exchange` (graph_schema v0.7; attrs 1:1 from MCP-J1 payloads); DESIGN §2/§16 + RESEARCH_MODEL counts → 14 nodes / 19 edges.
- `graph_fold.mcp`: G7 `graph_node_upserted` builders; `projections.apply_to_map` folds `McpRequestRecorded` / `McpResponseRecorded` into `graph_nodes` (response without prior request → skip, no invent `args_digest`).
- Closes graph unlock [#89](https://github.com/Naramerk/repo/issues/89) after MCP-J1 [#171](https://github.com/Naramerk/repo/pull/171) / [#78](https://github.com/Naramerk/repo/issues/78). Remaining G14 kinds (`llm_call`, `tool_call`, `job`, `plan_expansion` + provenance edges) still G14. [plan G18](docs/plan/graph.md).

### RC17a — Study.max_attempts from profile [M4]
- `Study.max_attempts` / `Profile.max_attempts` / `CreateStudyBody.max_attempts` required (no invent); `StudyCreated` writer schema v16 + upcaster refuse `max_attempts_required`.
- Packaged `research.v1` sets `max_attempts: 3`; CLI `run` wires `loaded_profile.max_attempts` → `create_study`; StudyCreated fold syncs runtime + bound Executor (pre-study cap 0, no invent-3).
- DoD: `tests/test_rc17a_max_attempts.py::test_missing_max_attempts_refuses`. [DESIGN §2 D17](docs/DESIGN.md); [plan RC17a](docs/plan/research-cycle.md) / [#99](https://github.com/Naramerk/repo/issues/99).

### R16a.1a — MD artifact code-span no backslash escape [M5]
- Lock `test_aid_in_code_span_no_backslash_escape`: code-span label is raw `aid` (no `\[`/`\]`/`\(`/`\)` inside the span); href remains percent-encoded via `artifact_href`. Fix already on main from R16a.1; this PR is the DoD test. [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [handoff](docs/handoff/report-s3.md) `R16a.1a` / [#119](https://github.com/Naramerk/repo/issues/119).

### Graph event-owner unlocks (G12 / G17 / G18) [M1]
- Assign plan IDs: research-cycle **RC12v** (`HypothesisVerdictRecorded`, [#96](https://github.com/Naramerk/repo/issues/96)); tools **TG-Call** (`ToolCalled`, [#88](https://github.com/Naramerk/repo/issues/88)); **MCP-J1** (`Mcp*Recorded`, [#78](https://github.com/Naramerk/repo/issues/78)/[#89](https://github.com/Naramerk/repo/issues/89)). Docs only — folds still wait for those PRs on `main`. Closes ownership gap for [#87](https://github.com/Naramerk/repo/issues/87)/[#88](https://github.com/Naramerk/repo/issues/88)/[#89](https://github.com/Naramerk/repo/issues/89).

### G14a — DESIGN §16 / M0 graph kind counts [M1]
- Align M0 note + RESEARCH_MODEL principle with packaged schema: **13 nodes / 19 edges** (`graph_schema.yaml` v0.6). DESIGN §16 already listed 13/19. [plan G14a](docs/plan/graph.md).


### Production profile catalog + research skeleton [G2 / R1]
- Packaged exact `profiles/packaged/tool_catalog.json` + `production_catalog.json`; YAML → `Profile.model_validate_json` only (no list→tuple shim).
- Production `research` v1 roles: `tz_spec`, `planner`, `critic`, `executor-route`, `reviewer` (DESIGN §7.1/§7.5); unknown profile/tool refuse.
- Hard dep for runtime exact tool resolve/invoke: merge of [#65](https://github.com/Naramerk/repo/pull/65)+[#157](https://github.com/Naramerk/repo/pull/157). Medicine/microfluidics YAML deferred (fixtures missing) → `UnknownProfileError`.

### Coder workflow attach (`invoke_coder_for_study`) [M3]
- `coscientist.tools.coder.attach`: hard Linux sandbox preflight (bwrap + hard isolation + `CGROUP_JOB`); refuse `allow_cgroup_degraded` bypass; `CoderTool` + `invoke_coder_for_study` → `invoke_tool` → `run_coder_job`.
- Registry built-in `coder` (`provider=coder`, write). Docs: `docs/coder-workflow.md` (hard deps: #65 `ToolResolved` coder status, alembic, AgentPort route — no invent).
- Preserves #150 gorilla closes inside `run_coder_job` (cgroup.events liveness, `GuestProcessGroupGone` journal+raise).

### C1 coder job + `run_sandbox` [M3]
- `run_coder_job`: spawn child + `run_sandbox(on_guest_started=…)`; deadline `killpg` guest group; `jobs.wait` (poll from `SandboxLimits`); unpublished pgid → fail-closed `JobCancelFailed`; `attempt_is_closed()` at put-time; `finally` forces terminal job; `sandbox_status_to_coder` mapping tested.
- Gorilla @75eb157: kill-after-timeout, no poll literal, no stuck `running`, live attempt-closed check, status mapping tests.
- Gate @3c15ccd: drop non-test `# type: ignore[return-value]` — pass `SandboxResult` isolation Literals through to `put_sandbox`.
- Gorilla @2d80a80: `killpg` guest group (not worker-only); narrow worker `except Exception` → typed `ToolError`; single `spawn` context (no fork/spawn fallback); real-guest kill test.
- Gorilla @5640529313 / @5640572892: one ESRCH layer — `_signal_or_record_gone` journals `JobGuestProcessGroupGone` (like `record_cancel_failed`); no helper except→bool/record; `GuestPgidSlot` Protocol; coder spec `TypedDict` (no `dict[str, Any]`).
- Register `JobGuestProcessGroupGone` in `PAYLOAD_MODELS` (EventType member was journaled without a wire schema — blocked store reads after #150).
- Align `ForkCreatedPayload` with durable-fork wire (`parent_id`/`child_id`/partition digests) so store reads accept #160 journals.
- Gorilla @5640612760 / @5640642564: restore multiline overdue comprehension from main; ESRCH → `GuestProcessGroupGone(ToolError)` via `raise ... from`.
- Gorilla @5640731055 / @5640759607: guest liveness from sandbox `cgroup.events` (`CgroupEventsLiveness` / `on_guest_cgroup`); missing source → typed refuse; TOCTOU `GuestProcessGroupGone` journaled then re-raised (no swallow); finally kill bool → `JobCancelFailed`; no audit markers.
- Stacks on G6. Parity: [DESIGN §6.4](docs/DESIGN.md#64-coder-и-alembic-как-асинхронные-задачи-r4), [CAP #11](docs/CAPABILITIES.md), §12.2 `coder_timeout`.

### [M1] CLI ``--profile`` loads packaged production research.v1
- Fail-closed ``load_profile`` + required ``--tool-catalog-version`` pin; unknown profile / pin mismatch → ``CliRefuse``.
- Wires profile roles → adapter ``role_models`` (CLI ``--*-model`` must match); validates ``spec``/``plan``/``execute`` routes.
- Role tools must already be on cassette/live ToolRouter surface — no blanket-allow; missing surface + no #65+#157 resolve → hard-dep refuse.
- Preserves #156 ToolRouter mutex, ``--wired-roles``, ``--hitl-mode``, cumulative CLI budgets.

### [M1] AgentPort adapter on MCP ToolRouter (DESIGN §13 D / §6.3)

- Uses the concrete `coscientist.tools.router.ToolRouter` (#139; no parallel Protocol / `call_via_router`). CAP #8: `ToolNeed` / `Resolution` / `resolve(by_name)` — exact `ToolSpec.name` match (no colon-parse); not-open raises like `call`. Agent path `resolve` → `call(server, tool, args, ctx)` with `CallCtx` from `core.execution.call_ctx` (`tools.contract` fully forbidden for agents; no ignore_imports; Tool/star banned by AST) + study `HitlPolicy` (required). `ToolResolutionStatus` Literal; graph_schema sync via explicit assert (not at import).
- `LlmAgentAdapter`: require `max_llm_calls`/`max_tokens` ≥ 1; adapter-accumulated `llm_calls`/`tokens` across `run` calls + post-call token refuse; required `Budget` (`max_cost_rub` must be `None`); schema failures raise `AgentError`. Roles: `tz_spec` / `planner` / `executor-route`; prompts via `PromptRef`/`PromptRegistry` (CAP #27). One closed `ToolArg` type. [DESIGN §7](docs/DESIGN.md#7-агентный-слой), [§6.3](docs/DESIGN.md#63-маршрутизация-и-alembic-fallback), [§13](docs/DESIGN.md#13-порядок-работ).

### G6 JobStore + Job* events [M3]
- `coscientist.tools.jobs`: `Job` / `JobStore` Protocol + journaled `InMemoryJobStore` (DESIGN §6.4).
- FakeClock `on_change` + unsubscribe; JobStore takes explicit `on_clock_change` (core `Clock` stays `now()`-only); `close()` unsubscribes.
- Job payloads / `JobEventType` (nested into `EventType`) / `require_aware_datetime` / `require_nonblank` in `core.events.job_events`.
- Contract: idempotent submit by `(attempt_id, spec_digest)`; one live job per attempt → `ToolError(local)`; deadline → `JobTimedOut` + `cancel` → `JobCancelled` (or `JobCancelFailed`); `wait` does not revive `timed_out`.
- No `core/ports.py` change; coder/alembic backends and jobs projection fold = follow-ups (C1 / G19).
- Parity: [DESIGN §6.4](docs/DESIGN.md#64-coder-и-alembic-как-асинхронные-задачи-r4), [CAP #11](docs/CAPABILITIES.md), issue [#130](https://github.com/Naramerk/repo/issues/130).

### MCP live servers (literature) [M1]
- `mcp_live`: `literature_live_bundle` → `McpToolRouterBundle` (`StreamableHttpServerConfig` + `McpServerRoute`; `McpClientConfig.model_validate` rebuild); `open_tool_router` (explicit `file_fetcher`/`http_transport`; open-fail cleanup owned by `ToolRouter.open`); tool names cited from CoResearchGraph / CAP #3; `mcp-servers/docker-compose.yml` (`COSCIENTIST_DEV_ROOT`, legacy container port 7331). [DESIGN §13](docs/DESIGN.md#13-вехи), [§12.2](docs/DESIGN.md#122-сценарии-и-пороги), [CAP #3/#8](docs/CAPABILITIES.md).

### [M1] CLI ``coscientist run`` (DESIGN §13 / §12.2)

- Entry: SPEC via `tz_spec`→`Executor.record`; planner → Need-off HYP/LIT via
  `cancel_task` (SKIPPED, not fabricated SUCCESS); `accept_plan`; executor-route
  → record (no invented claims); REPORT cancel; tick to closed/`fulfilled`.
  Allow-list from tool surface (`StaticToolRouter` or live MCP); static router
  refuses args mismatch; rubrics only from planner `OutputSpecDraft.rubric`.
  `HttpxTransport` retryable only for timeout/network. Intake HYP/LIT
  `required=False` (DESIGN optional Need). Explicit args including required
  `--hitl-mode` (no default / hardcoded `HitlPolicy`) and RC3t thresholds
  (`--confirmations-needed`, `--refutations-needed`, `--refutation-priority`,
  `--supported-needed`, `--max-expansions`; no invent); `CliRefuse` + non-zero
  exit. **Cassette vs live:** mutually exclusive `--tools-json` (cassette
  `StaticToolRouter`) or `--mcp-tool-bundle` + `--tool-call-args` (real
  `ToolRouter` via `open_tool_router_for_cli`; exact `server:tool`, no
  blanket-allow). Live allow-list = `--tool-call-args` keys (not every MCP-bound
  tool); `planned_calls` resolved before `start_attempt` → typed `CliRefuse` (no
  mid-run `KeyError` / dangling `AttemptStarted`). Optional `--wired-roles` seam
  for hypothesizer/researcher/report (fail-closed before `start_attempt` until
  adapter schemas + CLI journal record + #65/#150). Docs:
  [docs/cli-run-tools.md](docs/cli-run-tools.md). [DESIGN §12.2](docs/DESIGN.md#122-сценарии-и-пороги)
  / [§13](docs/DESIGN.md#13-порядок-работ).

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
  drop unused `RunnerRawResult`; typed refuse via `model_validate` / explicit
  constructors (no `# type: ignore`); reject cases parametrized with `ids` +
  exact `match=` (no `if "unique" in match` heuristics).
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
- Alligator fix: typed refuse via `model_validate` / explicit constructors
  (no `# type: ignore`); reject cases parametrized with `ids` + `match=`.
- Alligator fix: drop unused `_quality()`; `field_name` before `with` in freeze test.

### R16a.1 — reporting formats json/md/html [M5]
- Package `coscientist/reporting/`: `ReportRenderInput(report, report_artifact_id, tasks)`; `render_report_body` → body str; media via `media_type_for(format)` only; shared `artifact_href` for MD+HTML; HTML wrap `(aid, href)|None` + required `html_lang`; `ReportView` strict pydantic; `parse_report_format` for transport. Verdict/attempt provenance via shared `parse_provenance` (inputs + machine; no invent `real`). [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [DESIGN §9/§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a.1` / [#118](https://github.com/Naramerk/repo/issues/118).

### R16a — typed `MinimalReport` [M5]
- Pydantic `MinimalReport` (+ task/question/sections rows, `extra=forbid`); build/truncate/assemble return the model; `render_markdown_report(MinimalReport)`; StudyFinalized via `model_dump(mode="json")`. Rows use SoT `Disposition` / `TaskOutcome` / `Provenance` (no duplicate Literals). Assemble refuses missing question fields, hypothesis key≠id, DONE without outcome, missing/unknown closed-attempt provenance (no invent `real`); DONE without closed attempt uses explicit `provenance=null`; MD Tasks only from `report.tasks`; question `note` preserved; verdict disposition `reason` = verdict; AttemptStarted v10 upcaster refuses absent/null/unknown provenance (no invent); legacy `reclassified_fetch_old` fail-closed on open (`match=AttemptStarted.v10:provenance_required`); reclassify open/finalize seeds an authored journal that already carries provenance (never remap legacy → `real`); finalize reads public `StudyRuntime.observations`; shared `parse_provenance`. [CAP #16](docs/CAPABILITIES.md?plain=1#L25); [DESIGN §9–§11](docs/DESIGN.md); [handoff](docs/handoff/report-s3.md) `R16a` / [#117](https://github.com/Naramerk/repo/issues/117).

### RC3t study thresholds [M4]
- Drop construct / kw defaults on `Study` thresholds (`confirmations_needed`,
  `refutations_needed`, `refutation_priority`, `supported_needed`, `max_expansions`),
  `lifecycle.create_study`, `CreateStudyBody`, `hypothesis_verdict`.
- Single source: `coscientist.core.execution.thresholds` (`*_GE` + `require_*`)
  feeds `Study` / `CreateStudyBody` `Field(ge=…)`, upcasters, `hypothesis_verdict`.
- Upcasters refuse missing keys (`*_required`); no invent / clamp. Writer
  `StudyCreated` v15. Bind with `study is None` → `study_required`.
- Remove `verdict.py` 2-tuple compat shim and `rules.verdict` alias; callers use
  `hypothesis_verdict` directly.
- `verdict_binding_digest`: refuse missing thresholds / `provenance` (no invent).
- `dispositions`: all `CatalogError` (incl. `StudyRequiredError`) propagate
  fail-closed; other exceptions still map to `not_attempted`.
- DoD: `tests/test_rc3t_study_thresholds.py` (typed refuse + `match=` / `ids=`).
  [CAP #2](docs/CAPABILITIES.md); [DESIGN §2](docs/DESIGN.md#2-доменная-модель);
  [research-cycle RC3t](docs/plan/research-cycle.md).
- Review: `StrictInt`/`StrictBool`; restore main formatting on non-RC3t hunks
  (no glued >100); cover upcast/verdict raises with `match=`; dispositions
  tests via public API (no private `_dispositions_inner` / setattr).


### Graph schema parse (G7 split 1/2) [M1] — AttrType; `GRAPH_SCHEMA_VERSION`; required attrs; explicit `null_ok`. [DESIGN §2](docs/DESIGN.md#2-доменная-модель), [CAP #14](docs/CAPABILITIES.md?plain=1#L23).

### G4 Need

- `Need` (strict, `kind` discriminator) + `NeedUnrecorded`; PlanAccepted v12: absent → `NeedUnrecorded`, present → strict DU, corrupt → `JournalFormatError`; expand/foreach/loop → `require_need` / `PlanNeedRefuse`; accept omit → `PlanNeedRefuse`. No `Study.require_hypotheses` here. [CAP #2](docs/CAPABILITIES.md); [DESIGN §2](docs/DESIGN.md#2-доменная-модель).
- `StudyRuntime.append_events`: sole durable append+project path (replaces private `_append`); tests use the same entry as production.
- `fold_loop_capped`: status is `capped` only for `iteration_cap`; any other `LoopCapped.reason` (including plan-need refuse codes) folds as `refused` (was: only `error`/`refused` → refused, else capped).

### G4 Study.require_hypotheses

- `Study.require_hypotheses` required (no construct default); StudyCreated v13→v14 refuses missing/null/non-bool (`require_hypotheses_required` / `require_hypotheses_must_be_bool`) — **legacy StudyCreated without the field cannot be replayed** (typed `CatalogError`, no invent `False`); `accept_plan` gates via `assert_profile_hypotheses_satisfied`. [CAP #2](docs/CAPABILITIES.md); [DESIGN §2](docs/DESIGN.md#2-доменная-модель).

### EventStore payload schemas [M1]

Fail-closed typed wire schemas for every ``EventType`` on replay
(``payload_schemas`` + ``schema_gate``; ``extra=forbid``). Prerequisite for
TOOL-GAP G3 (#65). [DESIGN §3.5](docs/DESIGN.md#35-журнал-событий-event-store) /
schema gate.

- KindSpec dumps: criterion variants by `check` (`graph_schema` discriminator;
  code|review require metric/op/target; rubric+`affects_stance=false` requires
  `rubric_digest`; rubric+`affects_stance=true` requires metric — no invent /
  no code waive). Incomplete → typed `CatalogError` / PlanRefused.
- `ReviewCheckPlain`|`ReviewCheckMeasured` via `measured` discriminator on
  `RequirementTemplate.check` only (no `ReviewCheck` alias; no `None`/`Any`
  metric; `max_calls` required). `CATALOG_VERSION`=4; v3 HISTORY refuse
  `review_check_measured_discriminator_and_max_calls_required`.
- Experiment `attempts` required from real task rows. Claim `checked_against`
  from agent only. HITL plan-delegate without Need/revision/plan_id →
  `HitlPlanDelegateRefuse`. Plan Need `| NeedUnrecorded` only.
  [DESIGN §2](docs/DESIGN.md#2-доменная-модель) /
  [DESIGN_M1 §6.2](docs/DESIGN_M1.md).
- AttemptStarted: R16a **v10** keeps provenance required
  (`AttemptStarted.v10:provenance_required`, no invent); **v11** marks omitted
  forever-fields (`n`/`dispatch_id`/`route_returned`/`closed`/`study_id`) as
  `AttemptUnrecorded` (provenance excluded). `CATALOG_VERSION: Final[int]` from
  HISTORY; `attempt_get` accepts `Mapping|BaseModel` only.

### TOOL-GAP G3 (#65) [M1]: catalog + exact ``ToolResolved``; registers ``ToolResolved`` into fail-closed payload schema gate. [DESIGN §6.3](docs/DESIGN.md#63-маршрутизация-и-alembic-fallback) / [CAPABILITIES #8](docs/CAPABILITIES.md#cap-8). Depends: payload-schemas PR.

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
