# Agent layer plan (owner: AgentPort / Orchestrator)

Links: [DESIGN §7.2](../DESIGN.md) (`AgentPort` — **owner: this agent**), [DESIGN §6.0](../DESIGN.md) (MCP), [CAPABILITIES #5/#6/#18/#23/#24](../CAPABILITIES.md).  
Depends on: thin LLM (`coscientist/llm`), tools `invoke_tool` / gate / registry, [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (open), [#62](https://github.com/Naramerk/repo/pull/62) `RouteConfig`, [#50](https://github.com/Naramerk/repo/pull/50) PromptRegistry.  
Does **not** own: `Usage` / `Budget` types (**scheduler**); PromptRegistry (**#50**); MCP req/resp journal (**hand off → MCP/#54 follow-up near [#60](https://github.com/Naramerk/repo/pull/60)**).

## Область

| Piece | Owner | Notes |
|---|---|---|
| `AgentPort` / `AgentRun` / `AgentError` | **this agent** | G1 contract + fakes |
| `Orchestrator` (role order by `RouteConfig`) | **this agent** | G1b; `RouteConfig` from #62 |
| `PromptRegistry` / `PromptRef` | **#50** | Reference only — **no new PR here** |
| `Usage` type, study/`Budget`, `UsageRecorded` | **scheduler** (S1/S2) | Agents **return** `llm.types.Usage` on `AgentRun`; accept `Budget` per DESIGN §7.2; scheduler owns recording |
| MCP `McpRequestRecorded` / `McpResponseRecorded` | **MCP/#54 follow-up** (near #60) | **Not** in this agent's PR list — hand off |
| Research cycle, spec/lit, profiles, HITL, bench | **consumers** | |

## Состояние

- On main (`8d261d6`): `coscientist/llm` (`Usage`), `tools/gate.py`, `core/execution/tool_registry.py`, stub `AgentPort` (`Any`) in `core/events/ports.py`.
- Open: [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (**not merged** — not “done”).
- `coscientist/agents/` = boundary shell only.

## Сделано

- Prior main: LLM client, import-linter agents boundaries, tools gate/registry.
- (#54 in review — adapter only; do not treat as landed.)

## Dependency graph

```text
#62 Profile/RouteConfig ──► G1b Orchestrator
G1  AgentPort contract  ──► G1-impl JournaledAgentPort (item 5)
#54 MCP adapter         ──► G1-impl (MCP tools via invoke_tool)
G1-impl AgentRun.usage  ──► scheduler S1/S2 (UsageRecorded journal edge)
#50 PromptRegistry      ──► G1-impl (PromptRef resolve; no duplicate PR)
MCP journal Mcp*        ──► hand off (#54 follow-up / #60) — not this track
```

## Следующий PR

### G1 — `AgentPort` contract (~≤250 LOC)
**Where:** `coscientist.agents` (Protocol + `AgentRun` / `AgentError` + fakes). Delete `Any` stub in `core/events/ports.py` in **this same PR** (only ports.py touch).  
**Decision (core ↛ llm / core ↛ tools):** contract lives in **`agents`**, which may import `llm.types.Usage` and `Tool` via **`coscientist.tools`** (public re-export) — **not** `tools.contract`. No new core→llm ignore.  
**Acceptance:** mypy-clean; fake port tests; no orchestrator/journal/MCP; audit `--vs-main`; lint-imports; CHANGELOG; DESIGN §7.2 owner line.  
**DoD:** ≤1 open PR; ≤400 LOC; tests per new symbol; perf p99≤50ms once; never merge.

### G1b — Orchestrator by `RouteConfig` (~≤300 LOC)
**Depends:** #62 (`RouteConfig`), G1.  
**Acceptance:** fake `AgentPort` call order = `RouteConfig`; no LLM import in orchestrator.  
**DoD:** same as G1.

### G1-impl — Journaled `AgentPort` (~≤400 LOC)
**Depends:** G1, #54 (merged), #50 (PromptRef), scheduler S1 edge for `UsageRecorded`.  
**Acceptance:** fake LLM + `invoke_tool` only; live≡replay; agents ↛ `tools.contract` / registry.  
**DoD:** same as G1. MCP Mcp* journal **out of scope** (hand off).

## Контракты (locked for G1)

```python
RoleName = Literal[
    "hypothesizer", "researcher", "planner", "critic",
    "executor-route", "reviewer", "reporter",
]

class AgentError(Exception):
    """Typed agent failure — no parallel AgentRun.error: str|None field."""

class AgentRun(BaseModel):  # frozen, extra=forbid
    output: BaseModel | None
    usage: Usage                 # coscientist.llm.types.Usage (scheduler owns UsageRecorded)
    tool_calls: list[ToolCallRef]

class AgentPort(Protocol):
    async def run(
        self,
        role: RoleName,          # typed — not bare str
        input: BaseModel,
        tools: list[Tool],       # from coscientist.tools (↛ tools.contract)
        budget: Budget,          # DESIGN.md:701 — not budget_ref: str
    ) -> AgentRun: ...
```

Failures **raise `AgentError`** (subtypes as needed). `AgentRun` has no `error: str | None`.

## Вопросы

1. Exact `RoleName` set vs #62 profile roles — align when #62 lands.  
2. Confirm scheduler S1/S2 issue ids when that plan opens (edge reserved here).

## Проверки (DoD — every PR)

| Check | Rule |
|---|---|
| Scope | One task; ≤400 changed lines; stack depth ≤2; ≤1 open PR |
| Tests | Every new function; no shims/stubs/noop/“later” |
| Gates | `audit --vs-main` clean; `lint-imports`; `mypy coscientist` |
| Runtime | `pytest -m "not perf"` + `pytest -m perf` (p99 ≤ 50ms) once |
| Docs | CHANGELOG + DESIGN/CAP link; never merge from agent |

## Риски

- Shipping G1 into core `ports.py` with Usage/Tool would break core↛llm/tools — mitigated by agents-package decision above.  
- Starting G1-impl before #54/#50/#62 merge.  
- Taking MCP journal into this track (explicitly handed off).
