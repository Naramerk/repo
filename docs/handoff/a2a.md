# Handoff: capability #21 — A2A / MCP external MAS

## Область

Capability **#21** (docs/CAPABILITIES.md) / DESIGN **§6.5**:

1. **A2A inbound** — facade creates a study and streams journal events (agent card, task → study).
2. **A2A outbound** — remote agent callable as a `Tool` (HTTP provider once the **tool-catalog contract** from the #46 / tool-gap track lands).
3. **MCP server facade** — publish CoScientist modules (hypotheses / literature / plan) as MCP tools that call the **Orchestrator** / `AgentPort` with typed role outs — not a bare `dict` handler.

Constraints for follow-up work:

- ≤ **400** lines per PR (diff vs base); ≤ **1** open PR; stack depth ≤ **2**; **no shims**.
- Optional feature: config or typed refusal (`IntegrationDisabled`) — no hardcodes/heuristics.
- **Reuse ports already on `main`:** `StudyService` (`coscientist/core/events/ports.py`), `post_studies` / study create (`coscientist/api/studies.py`), `EventStore.read_after` (`ports.py`). Do **not** invent parallel `StudyCreatePort` / `JournalStreamPort`.
- import-linter: integrations must not grow a second HTTP stack; **httpx stays only in `coscientist.mcp.httpx_transport`** (inject that real transport into the A2A Tool at the composition root).
- `scripts/audit_heuristics.py --vs-main` must pass; markers hand-only.
- **DoD includes green GitHub CI** for the open PR.

## Состояние

- Oversized implementation (~1670 lines) was opened as PR #59 and **closed** (over the ≤400 line rule). Branch `cursor/capability-21-external-mas-fecb` kept for reference only — **do not reopen as one PR**. Treat sketches there as rejected where they duplicate core ports or bypass `AgentPort`.
- Base for new work: current `main` (fetch before branching).
- This handoff PR is the planning PR; next implementation PR starts **PR1** below from `main`.

## Сделано

On `main` today (reuse — do not re-define):

| Contract | Location | Use for #21 |
|---|---|---|
| `EventStore.read_after(seq)` | `coscientist/core/events/ports.py` | A2A journal stream (map `StoredEvent` → A2A wire view in the facade/API layer) |
| `StudyService` | `ports.py` | Study projection; extend **this** Protocol if create/stream helpers must be named — do not add a twin |
| `post_studies` / `CreateStudyBody` | `coscientist/api/studies.py` | A2A task → study create |
| `AgentPort.run(role, input, tools, budget)` | `ports.py` | MCP module tools → Orchestrator roles |
| Role outs | DESIGN §7.1 | `HypothesesOut`, `LiteratureOut`, `PlanOut` (typed) |
| `HttpxTransport` / `HttpTransport` | `coscientist/mcp/httpx_transport.py`, `mcp/transport.py` | **Only** allowed httpx site — real A2A HTTP transport lives here (or thin helper beside it), injected into tools |

Closed #59 sketched duplicate ports (`StudyCreatePort`, `JournalStreamPort`, `ModuleHandler.invoke→dict`, standalone `A2AHttpTransport` Protocol + fake-only) — **drop those designs**.

## Следующий PR

**PR1 (≤400, base=`main`):** errors + minimal config + A2A facade that **consumes** `post_studies` / `EventStore` (injected existing types or thin adapters that only forward — no new create/read Protocols). Tests with fake `EventStore` + fake create callable matching `post_studies` signature. No ToolProvider changes. No MCP server yet.

Suggested budget:

1. `coscientist/integrations/errors.py`, `config.py` (`ExternalMasConfig`, `A2AAgentCardConfig`, `A2AFacadeConfig` only).
2. `coscientist/integrations/a2a_facade.py` — gate `a2a`; `agent_card`; `create_task` → call injected study-create that is **`post_studies` (or StudyService once create is on that Protocol)**; `stream_events` → `EventStore.read_after`.
3. Facade may own a small **view DTO** for wire events; it must not redefine store read semantics.
4. `tests/integrations/test_a2a_facade.py`, `CHANGELOG.md`, import-linter entries for new modules only.

**PR2 (≤400; stack on PR1 or from `main` after merge, depth ≤2):** `api/a2a.py` wiring to real `post_studies` + open store / `read_after`; one integration test.

**PR3 — dependency (tool-gap / #46 track, not this area):** catalog contract PR that owns `ToolProvider` / tool-catalog (today: plan #46 → G3 / MCP adapter work, e.g. `provider=mcp` on #54). **`provider="http"` for remote A2A is blocked until that contract PR lands** (or explicitly adds `http`). Do not patch `ToolProvider` in a #21 PR ahead of them.

**PR4 (after PR3 contract available):** real A2A HTTP transport:

- **Where:** `coscientist/mcp/httpx_transport.py` (extend or add a small A2A-oriented helper next to `HttpxTransport`). Respect **httpx-only-in-mcp/httpx_transport**.
- **Protocol for injection:** reuse `coscientist.mcp.transport.HttpTransport` (or a tiny Protocol in `integrations` that matches it) — tools/api receive an **injected** instance; `tools` still ↛ `httpx` and still ↛ implementing httpx themselves.
- Composition root (api / experimental) constructs `HttpxTransport` and passes it into `RemoteA2AAgentTool`.
- Tests: fake transport in unit tests; optional real-transport test colocated with existing mcp httpx tests if needed.
- No shim “fake transport package” as the production path.

**PR5:** `RemoteA2AAgentTool` using the catalog `http` provider from PR3 + transport from PR4.

**PR6:** MCP server facade — `tools/call` for configured modules dispatches through **Orchestrator / `AgentPort`** returning typed **`HypothesesOut` / `LiteratureOut` / `PlanOut`** (serialize to MCP content). **Forbidden:** `ModuleHandler.invoke(arguments) -> dict` bypass. Handler surface = role name + typed I/O schemas owned by the agent layer; if those types are not on `main` yet, this PR waits on the agent-layer PR that adds them (stack ≤2 / dependency note in PR body).

**PR7:** `api/mcp_facade.py` wiring + contract test with fake `AgentPort` returning typed outs.

Each PR: local pytest + audit + lint-imports, then push; **CI green is DoD** (fix or follow-up before merge). ≤1 open implementation PR at a time.

## Контракты

```text
ExternalMasConfig.enabled_integrations: tuple[str, ...]
  requires("a2a" | "a2a_remote" | "mcp_server") → IntegrationDisabled if absent

# Reuse — do not duplicate:
StudyService / api.studies.post_studies  → A2A create_task
EventStore.read_after(seq)               → A2A stream_events (per study store)
AgentPort.run(role, input, tools, budget) → MCP module tools
  roles → HypothesesOut | LiteratureOut | PlanOut (DESIGN §7.1)

A2AFacade.agent_card() / create_task(message) / stream_events(task_id, after_seq)
  # create/stream ports = existing StudyService+post_studies / EventStore only

Remote A2A Tool:
  ToolProvider/"http"     → owned by tool-gap (#46) catalog contract PR — dependency
  HTTP bytes              → coscientist.mcp.httpx_transport (injected HttpTransport)
  ToolError on disabled / bad peer

McpModuleServer.handle(json_rpc) → result | error | None(notification)
  tools/call → Orchestrator/AgentPort → typed role Out (not bare dict)
```

No invented timeouts/status lists/module names — all from config. No second httpx client outside `mcp/httpx_transport`. No shim Protocols that clone `EventStore` / `StudyService` / `AgentPort`.

## Вопросы

1. Exact A2A wire field names for task create / result — keep config-driven until a pinned A2A version is chosen.
2. MCP `tools/list` cursor: typed refuse if present unless a later config enables pagination.
3. Whether `StudyService` on `main` should gain an explicit `create_study` / `events_after` method, or A2A keeps calling `post_studies` + `EventStore` directly (prefer direct reuse until StudyService is extended by its owners).
4. Confirm with #46 / tool-gap which PR is the **contract** base for adding `http` to `ToolProvider` (likely follows G3 / MCP `provider=mcp` adapter).

## Проверки

Per PR:

```bash
python -m pytest tests/integrations/ -q   # or the PR's new test file(s)
python scripts/audit_heuristics.py --vs-main
lint-imports
git diff --numstat origin/main...HEAD | awk '{a+=$1;d+=$2} END{print a+d}'  # must be ≤400
```

Push, then **wait for GitHub CI green** (DoD). Fix CI failures before considering the PR done.

## Риски

- **Line budget:** do not reassemble closed #59; split as above.
- **Duplicate ports:** reintroducing `StudyCreatePort` / `JournalStreamPort` / bare `ModuleHandler→dict` will fail crocodile review again.
- **ToolProvider race:** merging `http` before the #46 catalog contract PR → contract conflict; treat as hard dependency.
- **httpx placement:** putting a real A2A client under `tools/` or `integrations/` breaks import-linter; must live in `mcp/httpx_transport` and be injected.
- **Agent types missing:** MCP facade PR must not invent `dict` outs — wait for `HypothesesOut` / `LiteratureOut` / `PlanOut` on the agent track.
- **import-linter source lists:** every new module must be added to ingest/lifecycle/`_attempt_frame` contracts.
- **Parallel agents:** do not edit `coscientist/agents/` ownership surface except to *call* `AgentPort`; do not fork the MCP client.
