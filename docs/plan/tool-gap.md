# TOOL-GAP — revised plan (G3 / G6 ownership)

**Owner (this agent area):** G3 = MCP tool catalog + `ToolResolved`; G6 = `JobStore` / Job events (DESIGN §6.4).  
**Not owned:** Profile / `alembic: enabled|disabled` policy (**G2**, `#49` profiles agent). LLM, MCP client, execution graph = other agents.  
**Base:** `main`. **PR rules:** ≤1 open PR per agent; ≤400 changed lines; stack depth ≤2; no shims; check `main` first; `audit_heuristics.py --vs-main`; local pytest+perf; never merge.

## Open question (needs user answer)

**Exact-name vs semantic catalog search.** DESIGN §6.3 says “по имени и семантическому поиску”. CAPABILITIES #8 only requires catalog membership + successful call.  
Until answered: **G3 implements exact-name lookup only**; semantic/RAG search is **blocked / not started**.

## Non-goals / do not duplicate

| Temptation | Why not |
|---|---|
| Six journal event types (`ToolFound`, …) | DESIGN §6.3: **one** `ToolResolved(task_id, status, tool\|job_id)` |
| Parallel `GapJournal` | Use existing **`EventStore.append`** / study writer path |
| New “catalog call guard” | Overlaps `tools/gate.py` + `invoke_tool` / closed `tool_registry` — do not reimplement |
| Second effect registry | `core.execution.tool_registry` owns **effect class**; G3 catalog is **MCP membership + version** for routing only |
| Sandbox Protocol + “adapter later” | Shim — Coder jobs call **`checks.sandbox.run_sandbox`** directly |
| Alembic enable flag in this area | Lives on **Profile (G2 / #49)** |

## PR list

| ID | Title | Acceptance (1 line) | Depends |
|---|---|---|---|
| **G3** | MCP tool catalog + `ToolResolved` | Exact-name catalog lookup journals one `ToolResolved` (`tool_found` \| `tool_missing`) via `EventStore`; versioned register/bump; tests. | `main` |
| **G6** | `JobStore` Protocol + Job events | `Job` model + `JobStore` Protocol + `JobSubmitted` / `JobStatusChanged` / `JobTimedOut` / `JobCancelled` / `JobCancelFailed` on `EventType`; fake in-memory store; contract tests (§6.4). | `main` (or stack on G3 if shared helpers) |
| **G3b** | Empty catalog fixture | `EmptyMcpCatalog` for R3 / `alembic_nomcp` (no Alembic yet). | G3 |
| **A1** | Alembic client skeleton | Config endpoint/timeouts + injected HTTP transport; typed failures; fake transport tests. | G3; Profile/G2 when wiring enable flag |
| **A2** | Alembic `repo_search` (#10) | Returns candidate repo list; recorded on job/resolution path; empty → typed refuse. | A1, G6 |
| **A3** | Alembic build → validate → register (#9) | Pipeline steps; register into G3 catalog + version bump; `ToolResolved(status=tool_built)`. | A2, G3 |
| **A4** | `alembic_nomcp` e2e (#9) | Empty catalog → pipeline → tool in catalog → call via `invoke_tool`; fake Alembic. | A3, G3b |
| **C1** | Coder job via `JobStore` + `run_sandbox` (#11) | Submit coder job with deadline; run `checks.sandbox.run_sandbox`; artifacts sha256; deadline → `JobTimedOut` + failure(retryable). | G6 |
| **C2** | Coder → catalog + `ToolResolved(coder)` | Successful coder registers tool; one `ToolResolved(status=coder)`. | C1, G3 |
| **R1** | ToolRouter resolve (#8) | Catalog hit → `tool_found`; miss → request Alembic/Coder per **Profile** policy (G2); never guess from names. | G3, G6, G2/`#49`, A3/C2 as available |
| **R2** | `two_tasks` 2/2 `tool_found` (#8) | Both tools in catalog + successful calls; 0 calls outside catalog (`invoke_tool` path). | R1 |

## Stacking

- Default base: `main`. Stack only when required; **depth ≤ 2**.
- G6 may stack on G3 only if it needs shared test helpers; otherwise parallel on `main`.
- R1 waits on Profile G2 for `alembic: enabled|disabled` — do not invent a local policy enum.

## Done / in flight

| ID | Status |
|---|---|
| Plan rewrite | this update to `#46` |
| **G3** | next open PR (then STOP) |
| G6, A*, C*, R* | **not started** — for follow-up agents / later turns |
