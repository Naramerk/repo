# Agent layer plan (owner: AgentPort / Orchestrator)

Links: [DESIGN §7.2](../DESIGN.md) (`AgentPort` — **owner: this agent**), [DESIGN §6.0](../DESIGN.md) (MCP), [CAPABILITIES #5/#6/#18/#23/#24](../CAPABILITIES.md).  
Depends on: thin LLM (`coscientist/llm`), tools `invoke_tool` / gate / registry, [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (open), [#62](https://github.com/Naramerk/repo/pull/62) **`RoleName` + `RoleConfig`/`RouteConfig`**, [#50](https://github.com/Naramerk/repo/pull/50) PromptRegistry.  
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

- On main (`e4e8ec0`): `coscientist/llm` (`Usage`), `tools/gate.py`, `core/execution/tool_registry.py`, stub `AgentPort` (`Any`) in `core/events/ports.py`.
- Open: [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (**not merged**); [#62](https://github.com/Naramerk/repo/pull/62) profiles (`RoleName` export requested).
- `coscientist/agents/` = boundary shell only.

## Сделано

- Prior main: LLM client, import-linter agents boundaries, tools gate/registry.
- (#54 in review only — not landed.)

## Dependency graph

```text
#62 RoleName / RoleConfig / RouteConfig ──► G1 (role: RoleName) + G1b
G1  AgentPort contract                  ──► G1-impl JournaledAgentPort
#54 MCP adapter                         ──► G1-impl (MCP tools via invoke_tool)
#50 PromptRegistry                      ──► G1-impl (PromptRef; no duplicate PR)
G1 / G1-impl (AgentRun.usage)           ──► scheduler S1/S2 (consume Usage → UsageRecorded)
MCP journal Mcp*                        ──► hand off (#54 follow-up / #60)
```

Edge direction: **agents produce `Usage` on `AgentRun`; scheduler S1/S2 depend on that and own `UsageRecorded`.** G1-impl does **not** depend on S1/S2.

## Следующий PR

### G1 — `AgentPort` contract (~≤250 LOC)
**Depends:** **#62** (must export `RoleName`).  
**Where:** `coscientist.agents` (Protocol + `AgentRun` / `AgentError` + fakes). Delete `Any` stub in `core/events/ports.py` in this same PR.  
**Role:** `role: RoleName` **from #62 directly** — no `str` stub, no agents-local `Literal`.  
**Tools:** `tools: list[Tool]` with **`Tool` from `coscientist.tools.contract`** (DESIGN §7.2). No duplicate local Protocol.  
**import-linter decision (locked, agreed with tools owner [#65](https://github.com/Naramerk/repo/pull/65) / `bc-ff3e7866`):** see DESIGN §7.2 — G1 applies it in pyproject.  
**Usage/Budget:** return `llm.types.Usage`; accept domain `Budget`.  
**Acceptance:** mypy-clean; fake port tests; no orchestrator/journal/MCP; audit `--vs-main`; lint-imports; CHANGELOG.  
**DoD:** below + **GitHub CI green**.

### G1b — Orchestrator by `RouteConfig` (~≤300 LOC)
**Depends:** #62, G1.  
**Acceptance:** fake `AgentPort` order matches `RouteConfig`; no LLM import.  
**DoD:** same (incl. **CI green**).

### G1-impl — Journaled `AgentPort` (~≤400 LOC)
**Depends:** G1, #54 (merged), #50. **Does not depend on S1/S2.**  
**Acceptance:** fake LLM + **`invoke_tool` only** (no `Tool.call`); live≡replay.  
**DoD:** same. MCP Mcp* journal out of scope (hand off).

## Контракты (locked for G1)

```python
from coscientist.tools.contract import Tool  # type only; see import-linter decision
from <profiles/#62> import RoleName          # G1 depends on #62 export

class AgentError(Exception):
    """Typed agent failure — no AgentRun.error: str|None field."""

class AgentRun(BaseModel):  # frozen, extra=forbid
    output: BaseModel | None
    usage: Usage                 # llm.types.Usage; scheduler S1/S2 record UsageRecorded
    tool_calls: list[ToolCallRef]

class AgentPort(Protocol):
    async def run(
        self,
        role: RoleName,          # from #62 — not str, not local Literal
        input: BaseModel,
        tools: list[Tool],       # coscientist.tools.contract.Tool
        budget: Budget,          # DESIGN §7.2
    ) -> AgentRun: ...
```

## import-linter decision (G1 implements; tools/#65 agreed)

Contract `agents must use invoke_tool (no contract / registry / gate)` (`pyproject.toml`):

1. **`allow_indirect_imports = true`** — so `agents → tools.effects → tools.contract` (the `invoke_tool` path) is legal. Direct imports of `tools.gate` / `tools.taint` / `tool_registry` stay forbidden.  
2. **Narrow ignore** (unmatched alerting = error):  
   `coscientist.agents.<contract_module> -> coscientist.tools.contract`  
   Justification: type `list[Tool]` per DESIGN §7.2 without a duplicate Protocol; **runtime must still call `invoke_tool` only** (AST / boundary tests keep forbidding `.call(` / `register_tool`).  

Documented in DESIGN §7.2; G1 lands the pyproject change.

## Вопросы

1. Confirm `#62` `RoleName` export path/module when it lands.  
2. Scheduler S1/S2 PR numbers when that plan opens.

## Проверки (DoD — every PR)

| Check | Rule |
|---|---|
| Scope | One task; ≤400 changed lines; stack depth ≤2; ≤1 open PR |
| Tests | Every new function; no shims/stubs/noop/“later” |
| Gates | `audit --vs-main` clean; `lint-imports`; `mypy coscientist` |
| Runtime | `pytest -m "not perf"` + `pytest -m perf` (p99 ≤ 50ms) once |
| **CI** | **GitHub CI green required** |
| Docs | CHANGELOG + DESIGN/CAP link; never merge from agent |

## Риски

- Starting G1 before #62 exports `RoleName`.  
- Direct `tools.contract` import abused for `Tool.call` — mitigated by ignore scoped to contract module + AST boundary tests.  
- Reversing S1 edge.  
- Taking MCP journal into this track (handed off).
