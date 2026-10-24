# Agent layer plan (owner: AgentPort / Orchestrator)

Links: [DESIGN §7](../DESIGN.md) (roles, `AgentPort`, journaled state), [DESIGN §6.0](../DESIGN.md) (MCP), [CAPABILITIES #5/#6/#18/#23/#24](../CAPABILITIES.md).  
Depends on: thin LLM (`coscientist/llm`, `Usage`), tools gate + `invoke_tool`, `#54` MCP adapter.  
Does **not** own: `Usage` / study `Budget` types (planner); MCP client internals.

## Область

| Piece | Owner | Notes |
|---|---|---|
| `AgentPort` / `AgentRun` / `AgentError` | **this agent** | Contract + fakes; implementation later |
| `Orchestrator` (role order by `RouteConfig`) | **this agent** | Deterministic; no LLM in orchestrator |
| `PromptRegistry` / `PromptRef` | **this agent** | Profile supplies refs; no prompt text heuristics |
| `Usage` type, study/`Budget` | **planner** | Agents only **return** `llm.types.Usage` and **accept** `budget_ref` |
| MCP request/response journal events | **planned** (not this PR) | Adapter binds tools; journal hooks = later item |
| Research cycle, spec/lit, profiles, HITL, bench | **consumers** of AgentPort / Orchestrator | |

## Состояние

- On main: `coscientist/llm` (`Usage`), `tools/gate.py`, `core/execution/tool_registry.py`, stub `AgentPort` in `core/events/ports.py` (`Any` placeholders).
- Open: [#54](https://github.com/Naramerk/repo/pull/54) MCP → `invoke_tool` adapter (review fixes landed).
- `coscientist/agents/` package exists as boundary shell only (no contract yet).

## Сделано

- MCP tool adapter PR (#54): bind by `(server, tool)`, per-tool profile, timeout from session config only.
- Import-linter / journal / LLM client already on main (prior PRs).

## Следующий PR

1. **G1 — `AgentPort` contract in `ports.py` (own PR)**  
   Typed `AgentPort` Protocol + `AgentRun` / `AgentError` + test fakes in `coscientist/core/events/ports.py` (replace `Any` stub).  
   Reuse `coscientist.llm.types.Usage` on the result; accept `budget_ref` (opaque ref — **not** define `Budget`).  
   Acceptance: mypy-clean Protocol; fake port passes contract tests; no orchestrator / no journal / no MCP; `audit --vs-main` clean; CHANGELOG + DESIGN §7.2 link.  
   **Rule:** `ports.py` changes are this PR only (no mix with orchestrator).

2. **G1b — Orchestrator by `RouteConfig`**  
   Deterministic role sequence from profile `RouteConfig`; calls `AgentPort.run` only.  
   Acceptance: fake `AgentPort` records call order; no LLM import in orchestrator module.

3. **PromptRegistry / PromptRef**  
   Resolve `PromptRef` → text from registry; missing ref fails closed.  
   Acceptance: unit tests; agents never embed role prompt literals.

4. **MCP request/response journal** (planned item)  
   Journal `McpRequestRecorded` / `McpResponseRecorded` via hooks on adapter path; CAS bodies; live≡replay.  
   Acceptance: fake MCP + writer tests; depends on event-type PR first.

5. **Journaled `AgentPort` implementation**  
   LLM loop + `invoke_tool` only; all state in event journal.  
   Acceptance: fake LLM tests; agents ↛ `tools.contract` / registry (import-linter).

## Контракты

```python
# G1 target (DESIGN §7.2) — names indicative; Exact fields locked in G1 PR
class AgentError(Exception): ...

class AgentRun(BaseModel):  # frozen, extra=forbid
    output: BaseModel | None
    error: str | None
    usage: Usage              # coscientist.llm.types.Usage — reuse, do not fork
    tool_calls: list[ToolCallRef]  # existing core ToolCallRef

class AgentPort(Protocol):
    async def run(
        self,
        role: str,
        input: BaseModel,
        tools: list[Tool],
        budget_ref: str,      # planner-owned Budget; agents do not own Budget type
    ) -> AgentRun: ...
```

- Orchestrator: `RouteConfig` → ordered roles; never invents tools/budget.
- Prompt: `PromptRef` only; registry is the single source of prompt bytes.
- MCP journal: separate planned PR (see above); not part of G1/G1b.

## Вопросы

1. Does G1 keep `AgentRun` in `ports.py` (core) while importing `Usage` from `llm`? Today **core ↛ llm** (import-linter). Options: (a) relax one ignore for `ports.py → llm.types`, (b) place `AgentRun` in `coscientist.agents` and leave only a minimal Protocol in `ports.py`, (c) duplicate a structural Usage protocol in core (forbidden by “reuse llm.types.Usage”). **Decide in G1 before coding.**
2. `budget_ref` vs DESIGN sketch `budget: Budget` — confirm opaque ref is required so Budget stays planner-owned.
3. Should stub `AgentPort` in `ports.py` be deleted only in G1 (yes per “ports.py = own PR”)?

## Проверки (DoD per PR)

- One coherent task; ~≤400 changed lines; stack depth ≤2; ≤1 open PR.
- Tests for every new function; no shims/stubs/noop/“adapter later”.
- `scripts/audit_heuristics.py --vs-main` clean; `lint-imports`; `mypy coscientist`.
- `pytest -m "not perf"` + `pytest -m perf` (p99 ≤ 50ms) once locally.
- CHANGELOG entry + link to DESIGN/CAP item.
- Never merge from the agent.

## Риски

- **core ↛ llm vs Usage on AgentRun** — highest design risk for G1; resolve before implementation.
- Colliding with planner Budget PR if AgentPort takes concrete `Budget`.
- Expanding G1 into orchestrator/journal/MCP (split already listed).
- `#54` must land before agent MCP e2e; journal MCP events still unplanned beyond item 4.
