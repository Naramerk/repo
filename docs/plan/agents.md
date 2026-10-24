# Agent layer plan (owner: AgentPort / Orchestrator)

Links: [DESIGN §7.2](../DESIGN.md) (`AgentPort` — **owner: this agent**), [DESIGN §6.0](../DESIGN.md) (MCP), [CAPABILITIES #5/#6/#18/#23/#24](../CAPABILITIES.md).  
Depends on: thin LLM (`coscientist/llm`), tools `invoke_tool` / gate / registry, [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (open), [#62](https://github.com/Naramerk/repo/pull/62) `RoleConfig`/`RouteConfig`, [#50](https://github.com/Naramerk/repo/pull/50) PromptRegistry.  
Does **not** own: `Usage` / `Budget` / `UsageRecorded` (**scheduler** S1/S2); PromptRegistry (**#50**); MCP req/resp journal (**hand off → MCP/#54 follow-up near [#60](https://github.com/Naramerk/repo/pull/60)**).

## Область

| Piece | Owner | Notes |
|---|---|---|
| `AgentPort` / `AgentRun` / `AgentError` | **this agent** | G1 contract + fakes |
| `Orchestrator` (order by `RouteConfig`) | **this agent** | G1b; types from #62 |
| `PromptRegistry` / `PromptRef` | **#50** | Reference only — **no new PR here** |
| `Usage` / `Budget` / `UsageRecorded` | **scheduler** (S1/S2) | Agents return `llm.types.Usage` on `AgentRun`; accept `Budget` (DESIGN §7.2); scheduler records |
| MCP `McpRequestRecorded` / `McpResponseRecorded` | **MCP/#54 follow-up** (near #60) | Hand off — not this track |
| Research cycle, spec/lit, profiles, HITL, bench | **consumers** | |

## Состояние

- On main (`3d5f28d`): `coscientist/llm` (`Usage`), `tools/gate.py`, `core/execution/tool_registry.py`, stub `AgentPort` (`Any`) in `core/events/ports.py`.
- Open: [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (**not merged**).
- `coscientist/agents/` = boundary shell only.

## Сделано

- Prior main: LLM client, import-linter agents boundaries, tools gate/registry.
- (#54 in review only — not landed.)

## Dependency graph

```text
#62 RoleConfig / RouteConfig ──► G1 (role keys) + G1b (Orchestrator)
G1  AgentPort contract       ──► G1-impl JournaledAgentPort
#54 MCP adapter              ──► G1-impl (MCP tools via invoke_tool)
#50 PromptRegistry           ──► G1-impl (PromptRef; no duplicate PR)
G1 / G1-impl (AgentRun.usage) ──► scheduler S1/S2 (S1/S2 consume Usage → write UsageRecorded)
MCP journal Mcp*             ──► hand off (#54 follow-up / #60)
```

Edge direction: **agents produce `Usage` on `AgentRun`; scheduler S1/S2 depend on that and own `UsageRecorded`.** G1-impl does **not** depend on S1/S2.

## Следующий PR

### G1 — `AgentPort` contract (~≤250 LOC)
**Where:** `coscientist.agents` (Protocol + `AgentRun` / `AgentError` + fakes). Delete `Any` stub in `core/events/ports.py` in this same PR.  
**Role:** `role` is a **profile role key from `#62` `RoleConfig`** (mapping key / field typed there). **No** hardcoded `Literal[...]` role set in agents.  
**Tools typing decision (locked):** `agents` must **not** import `coscientist.tools` or `tools.contract` (pyproject `agents must use invoke_tool`, no `allow_indirect_imports` — `agents → tools → tools.contract` fails lint-imports). G1 defines a local structural `Protocol` (e.g. `AgentTool`: `spec` + `async call`) in `agents` with **zero** tools imports. Runtime `JournaledAgentPort` calls **`invoke_tool` only**; it never imports `tools.contract`.  
**Usage/Budget:** return `llm.types.Usage`; accept domain `Budget` (DESIGN §7.2).  
**Acceptance:** mypy-clean; fake port tests; no orchestrator/journal/MCP; audit `--vs-main`; lint-imports; CHANGELOG; DESIGN §7.2 owner.  
**DoD:** below + **GitHub CI green**.

### G1b — Orchestrator by `RouteConfig` (~≤300 LOC)
**Depends:** #62, G1.  
**Acceptance:** fake `AgentPort` order matches `RouteConfig`; no LLM import.  
**DoD:** same (incl. **CI green**).

### G1-impl — Journaled `AgentPort` (~≤400 LOC)
**Depends:** G1, #54 (merged), #50. **Does not depend on S1/S2** (scheduler consumes `AgentRun.usage` afterward).  
**Acceptance:** fake LLM + `invoke_tool` only; live≡replay; no `tools.contract` import.  
**DoD:** same. MCP Mcp* journal out of scope (hand off).

## Контракты (locked for G1)

```python
# role: key into Profile.roles / RoleConfig from #62 — NOT a local Literal enum
# AgentTool: structural Protocol in coscientist.agents (no import of coscientist.tools*)

class AgentError(Exception):
    """Typed agent failure — no AgentRun.error: str|None field."""

class AgentRun(BaseModel):  # frozen, extra=forbid
    output: BaseModel | None
    usage: Usage                 # llm.types.Usage; scheduler S1/S2 record UsageRecorded
    tool_calls: list[ToolCallRef]

class AgentPort(Protocol):
    async def run(
        self,
        role: str,               # profile RoleConfig key (#62); typed when #62 exports alias
        input: BaseModel,
        tools: Sequence[AgentTool],  # local Protocol — not tools.Tool / tools.contract
        budget: Budget,          # DESIGN §7.2
    ) -> AgentRun: ...
```

Align with DESIGN §7.2 sketch (same `role` / `budget: Budget` / no `error: str|None`). When #62 lands a `RoleName`/`RoleKey` alias, G1 switches `role:` to that alias in the same agents package — still no hardcoded role Literal here.

## Вопросы

1. Exact `#62` export name for role keys (`RoleKey` vs `str` NewType) — adopt when #62 merges.  
2. Scheduler S1/S2 PR numbers when that plan opens (consume edge reserved).

## Проверки (DoD — every PR)

| Check | Rule |
|---|---|
| Scope | One task; ≤400 changed lines; stack depth ≤2; ≤1 open PR |
| Tests | Every new function; no shims/stubs/noop/“later” |
| Gates | `audit --vs-main` clean; `lint-imports`; `mypy coscientist` |
| Runtime | `pytest -m "not perf"` + `pytest -m perf` (p99 ≤ 50ms) once |
| **CI** | **GitHub CI green required** (do not skip / “don’t wait”) |
| Docs | CHANGELOG + DESIGN/CAP link; never merge from agent |

## Риски

- Importing `coscientist.tools` for `Tool` typing breaks lint-imports — mitigated by local `AgentTool` Protocol.  
- Hardcoding roles before #62 — forbidden; wait for `RoleConfig` keys.  
- Reversing S1 edge (G1-impl must not wait on scheduler).  
- Taking MCP journal into this track (handed off).
