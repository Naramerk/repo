# TOOL-GAP / Alembic / Coder — small PR plan

Area: CAPABILITIES #8 (tool retrieval statuses), #9 (Alembic pipeline), #10 (repo search), #11 (Coder sandbox job).
Base: `main` @ `2ac0d5b`. Parallel agents own LLM client, MCP client, execution graph — this area depends only on narrow Protocols + fakes.
Hard rules: no hardcodes/heuristics (`scripts/audit_heuristics.py --vs-main`); config or typed refuse; tests for every new function; local pytest + perf (p99 ≤ 50 ms); never merge (owner merges).
Size: each PR ≈ ≤300 changed lines, one coherent slice.

## PR list

| # | Title | Scope (1 line) | Depends on |
|---|---|---|---|
| 1 | **tool-gap plan doc** (this PR) | Add `docs/plan/tool-gap.md` with the stacked PR list only. | — (base `main`) |
| 2 | TOOL-GAP journal event types | Add `ToolFound` / `ToolMissing` / `ToolProvisionRequested` / `AlembicStep` / `CoderStep` / `ToolCatalogRegistered` to `EventType` + constructibility tests; no projection logic. | main (independent of #1) |
| 3 | `tools/gap` types + errors | Frozen `CatalogTool`, `ToolNeed`, `ResolutionStatus`, typed error classes; unit tests only. | #2 optional |
| 4 | `tools/gap` config models | Required-field `ToolGapConfig` / `ToolSpecValidationConfig` / `AlembicClientConfig` / `CoderAgentConfig` / `CoderSandboxLimits` (no guessed defaults); validation tests. | #3 |
| 5 | Versioned MCP tool catalog | `VersionedToolCatalog`: exact-name get/contains/register + `catalog_version` bump; refuse duplicates; tests. | #3 |
| 6 | Gap journal helpers + ports | `GapJournalPort`, `MemoryGapJournal`, event builders for #2 types; clock/catalog/HTTP/sandbox/codegen Protocols; tests. | #2, #3 |
| 7 | Tool retrieval → found/missing | Exact-name lookup journals `ToolFound` or `ToolMissing`; no semantic/name guessing; tests. | #5, #6 |
| 8 | Catalog call guard | `call_catalog_tool`: refuse any call absent from catalog (0 outside-catalog calls); tests. | #5 |
| 9 | Missing-tool policy routing | Explicit plan/config `alembic` \| `coder` only; typed refuse if backend missing; no name-based guessing; tests. | #4 |
| 10 | ToolSpec validation | Config-driven validate-before-register; typed `ToolSpecValidationError`; tests. | #4, #3 |
| 11 | Alembic HTTP client skeleton | Injected `HttpTransport`; config endpoint/timeouts; typed transport/HTTP failures; one POST helper + tests with fake transport. | #4, #6 |
| 12 | Alembic `repo_search` (#10) | Step returns candidate repository list; journal `AlembicStep(repo_search)`; empty/malformed → typed refuse; tests. | #11 |
| 13 | Alembic `build` step (#9) | Build from candidate list (server chooses; no local name pick); journal `AlembicStep(build)`; tests. | #12 |
| 14 | Alembic validate + register (#9) | Validate spec (#10) + remote/local register; journal steps; bump catalog version on success; tests. | #10, #13, #5 |
| 15 | `ToolGapRouter` wire-up | retrieve → missing → policy → provisioner → register; statuses `tool_found` / `tool_built` / `coder` / `tool_missing`; tests. | #7, #8, #9, #14 |
| 16 | `two_tasks` 2/2 `tool_found` (#8) | Scenario: both tools in catalog, both calls succeed, 2/2 `ToolFound`, 0 outside-catalog calls; e2e with fakes. | #15, #8 |
| 17 | `alembic_nomcp` scenario (#9) | Empty catalog → repo_search→build→validate→register → tool called in task; fake Alembic + fake MCP; e2e. | #15, #14, #8 |
| 18 | CoderAgent codegen path | `CoderAgent` + `CodeGenerator` Protocol; journal `CoderStep(code_generated)`; typed codegen failures; tests with fake LLM. | #4, #6 |
| 19 | Coder job + deadline (#11) | Job envelope with explicit deadline; sandbox run via `SandboxRunner` Protocol (local/HTTP adapter later); tests. | #18 |
| 20 | Coder artifacts sha256 (#11) | Register code/stdout/stderr (and result files) with sha256 digests on journal/artifact records; tests. | #19 |
| 21 | Coder deadline → failure(retryable) (#11) | Past deadline → verdict failure with `retryable=True`; journal; tests (fake clock). | #19 |
| 22 | Coder provision → catalog | Successful coder run yields `CatalogTool(source=coder)` + register via router; status `coder`; tests. | #15, #20, #21 |
| 23 | CHANGELOG + public exports | Document TOOL-GAP surface in CHANGELOG; `__init__` exports only; no behavior change. | last implementation PR merged or stacked |

## Stacking notes

- Prefer base=`main` when the slice only needs Protocols/fakes already on main.
- Stack on the prior TOOL-GAP PR when the slice imports unfinished modules (say so in the PR body: `stack_on: #N`).
- Do not duplicate MCP/LLM clients or the execution graph; inject Protocols and test with fakes.
- GitHub CI may be blocked — run full pytest + perf + `audit_heuristics.py --vs-main` locally before opening each PR.

## Out of scope for this area

- Real MCP session wiring / live Alembic HTTP / production sandbox bwrap adapter (follow-up after #17/#22).
- Agent orchestrator / plan expansion / HITL (other agents).
- Semantic tool search (RAG); retrieval here is exact-name against the versioned catalog only.
