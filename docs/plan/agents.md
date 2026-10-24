# Agent layer plan (owner: AgentPort / Orchestrator)

Links: [DESIGN §7.2](../DESIGN.md#72-тонкий-llm-клиент-coscientistllm) (`AgentPort` — **owner: this agent**), [DESIGN §6.0](../DESIGN.md#60-тонкий-mcp-клиент-coscientistmcp-m2-prep-a7) (MCP), [CAPABILITIES #5/#6/#18/#23/#24](../CAPABILITIES.md).  
Depends on: thin LLM (`coscientist/llm`), tools `invoke_tool` (gate, registry), [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (**merged**), [#62](https://github.com/Naramerk/repo/pull/62) (`Identifier` from `mcp.config` for role keys; `from coscientist.mcp.config import Identifier`), [#50](https://github.com/Naramerk/repo/pull/50) PromptRegistry, [#70](https://github.com/Naramerk/repo/pull/70) scheduler plan (S1/S2).  
Does **not** own: `Usage`, `Budget`, `UsageRecorded` (**scheduler** S1/S2 per [#70](https://github.com/Naramerk/repo/pull/70)); PromptRegistry (**#50**); MCP req/resp journal (`McpRequestRecorded`, `McpResponseRecorded` — tracked in [#78](https://github.com/Naramerk/repo/issues/78); nothing in this plan depends on it).

## Область

| Piece | Owner | Notes |
|---|---|---|
| `AgentPort`, `AgentRun`, `AgentError` | **this agent** | G1 contract + fakes |
| `Orchestrator` (order by `RouteConfig`) | **this agent** | G1b; types from #62 |
| `PromptRegistry`, `PromptRef` | **#50** | Reference only — **no new PR here** |
| `Usage`, `Budget`, `UsageRecorded` | **scheduler** ([#70](https://github.com/Naramerk/repo/pull/70) S1/S2) | Agents return `llm.types.Usage` on `AgentRun`; accept `Budget` (DESIGN §7.2); scheduler records |
| MCP `McpRequestRecorded`, `McpResponseRecorded` | [#78](https://github.com/Naramerk/repo/issues/78) | Tracked open question; nothing in this plan depends on it |
| Research cycle, spec/lit, profiles, HITL, bench | **consumers** | |

## Состояние

- Main tip: `0006805`.
- On main: `coscientist.llm` (`Usage`); `tools.gate`; `core.execution.tool_registry`; `tools.mcp_adapter` (#54); stub `AgentPort` (`Any`) in `core.events.ports`.
- Open: [#62](https://github.com/Naramerk/repo/pull/62) profile models (`Identifier` role keys); [#70](https://github.com/Naramerk/repo/pull/70) scheduler S1/S2 plan; [#78](https://github.com/Naramerk/repo/issues/78) MCP journal owner (no plan dependency).
- `coscientist.agents` is a boundary shell only.

## Сделано

- Prior main: LLM client, import-linter agents boundaries, tools gate and registry.
- **#54 merged**: MCP → `invoke_tool` adapter.
- **#60 merged**: capability #21 A2A/MCP handoff plan (`docs/handoff/a2a.md` on main; no MCP client journal item there yet).

## Dependency graph

```text
#62 Profile/RoleConfig/RouteConfig (role: Identifier) ──► G1 (start after #62 merged) + G1b
G1  AgentPort contract (coscientist.agents.contract) ──► G1-impl
#54 MCP adapter (merged)                ──► G1-impl (MCP tools via invoke_tool)
#50 PromptRegistry                      ──► G1-impl (PromptRef; no duplicate PR)
G1, G1-impl (AgentRun.usage)            ──► #70 scheduler S1/S2 (Usage → UsageRecorded)
MCP journal Mcp*                        ──► #78 (no edge into G1 / G1b / G1-impl)
```

Edge direction: **agents produce `Usage` on `AgentRun`; [#70](https://github.com/Naramerk/repo/pull/70) S1/S2 depend on that and own `UsageRecorded`.** G1-impl does **not** depend on S1/S2. G1 / G1b / G1-impl do **not** depend on [#78](https://github.com/Naramerk/repo/issues/78).

## Следующий PR

### G1 — `AgentPort` contract (≤250 LOC)
**Start:** G1 after [#62](https://github.com/Naramerk/repo/pull/62) merged (`Profile` / `RouteConfig` on main).  
**Where:** module **`coscientist.agents.contract`** (Protocol + `AgentRun`, `AgentError` + fakes). Delete `Any` stub in `core/events/ports.py` in this same PR.  
**Role:** `role: Identifier` via `from coscientist.mcp.config import Identifier`.  
**Tools:** `tools: list[Tool]` with `Tool` from `coscientist.tools.contract`.  
**import-linter (G1 lands pyproject; this docs PR does not prove lint-imports):** see decision below. Agents↔`invoke_tool` rule is [DESIGN §6.1](../DESIGN.md#61-контракт) on main. Coordinated with tools track [#65](https://github.com/Naramerk/repo/pull/65) for `tools.effects` →\* `ignore_imports` only (#65 does not edit DESIGN).  
**Usage/Budget:** return `llm.types.Usage`; accept domain `Budget`.  
**Acceptance:** mypy-clean; fake port tests; no orchestrator/journal/MCP; audit `--vs-main`; **`lint-imports` green with the pyproject change G1 lands**; **AST boundary test mandatory in G1** (no `Tool.call`, no `register_tool`, no direct gate/taint/registry misuse — мартышка + крокодил); CHANGELOG.  
**DoD:** below + **CI green on push and on merge-ref vs current main** + lint-imports + AST test.

### G1b — Orchestrator by `RouteConfig` (≤300 LOC)
**Depends:** #62 merged, G1.  
**Acceptance:** fake `AgentPort` order matches `RouteConfig`; no LLM import.  
**DoD:** same.

### G1-impl — Journaled `AgentPort` (≤400 LOC)
**Depends:** G1, #54 (merged), #50. **Does not depend on #70 S1/S2.** **Does not depend on [#78](https://github.com/Naramerk/repo/issues/78).**  
**Acceptance:** fake LLM + **`invoke_tool` only** (no `Tool.call`); live≡replay; **AST test mandatory**.  
**DoD:** same. MCP Mcp\* journal out of scope ([#78](https://github.com/Naramerk/repo/issues/78)).

## Контракты (locked for G1)

```python
from coscientist.mcp.config import Identifier     # after #62 merged; role keys
from coscientist.tools.contract import Tool       # typing; see ignore_imports below

class AgentError(Exception):
    """Typed agent failure — no AgentRun.error: str|None field."""

class AgentRun(BaseModel):  # frozen, extra=forbid
    output: BaseModel | None
    usage: Usage                 # llm.types.Usage; #70 S1/S2 record UsageRecorded
    tool_calls: list[ToolCallRef]

class AgentPort(Protocol):
    async def run(
        self,
        role: Identifier,
        input: BaseModel,
        tools: list[Tool],
        budget: Budget,
    ) -> AgentRun: ...
```

## import-linter decision (G1 implements pyproject — not verified in this docs PR)

Contract `agents must use invoke_tool (no contract / registry / gate)` (`pyproject.toml`):

- **Do not** set `allow_indirect_imports = true` (that would open every transitive path to `tools.gate`, `tools.taint`, and `tool_registry`).
- **Targeted `ignore_imports`** only (`unmatched_ignore_imports_alerting = "error"`).

Checked against [#65](https://github.com/Naramerk/repo/pull/65) `tools.effects` imports: `contract`, `gate`, `core.execution.tool_registry` (no `tools.taint` import there) — the three effects→\* ignores match:

```toml
ignore_imports = [
  # Tool typing on AgentPort (coscientist.agents.contract only)
  "coscientist.agents.contract -> coscientist.tools.contract",
  # invoke_tool in tools.effects (#65): imports contract, gate, tool_registry
  "coscientist.tools.effects -> coscientist.tools.contract",
  "coscientist.tools.effects -> coscientist.tools.gate",
  "coscientist.tools.effects -> coscientist.core.execution.tool_registry",
]
```

Direct `agents → tools.gate`, `tools.taint`, and `tool_registry` stay forbidden. Runtime: **`invoke_tool` only**. **G1 must ship an AST boundary test** (мартышка + крокодил) — lint-imports alone is not enough.

Agents↔`invoke_tool`: [DESIGN §6.1](../DESIGN.md#61-контракт). Tools-side `effects` ignores: [#65](https://github.com/Naramerk/repo/pull/65). G1 DoD: `lint-imports` + AST test after pyproject edit.

## Вопросы

1. **MCP journal owner** — [#78](https://github.com/Naramerk/repo/issues/78). Assign an agent area + plan item / PR for `McpRequestRecorded` / `McpResponseRecorded`. **Nothing in this plan depends on that issue** (no edge into G1 / G1b / G1-impl).

## Проверки (DoD — every PR)

| Check | Rule |
|---|---|
| Scope | One coherent task; ≤400 changed lines |
| Open PRs | **≤1 open PR per agent** (project rule) |
| Stack | **Stack depth ≤2** (project rule; separate from open-PR cap) |
| Tests | Every new function; no shims/stubs/noop/“later”; **G1: AST boundary test mandatory** |
| Gates | `audit --vs-main` clean; `mypy coscientist` |
| **lint-imports** | **G1:** green with the pyproject `ignore_imports` G1 lands (this docs PR does not change pyproject) |
| Runtime | `pytest -m "not perf"` + `pytest -m perf` (p99 ≤ 50ms) once |
| **CI** | **Green on push** and **green on merge-ref against current `main`** (both required) |
| Docs | CHANGELOG + DESIGN/CAP link; never merge from agent |

## Риски

- Starting G1 before #62 merges profile models (`Identifier` role keys) to main.  
- Broad `allow_indirect_imports` — rejected; keep targeted ignores only (matched to #65 `effects` imports).  
- Direct `tools.contract` abuse via ignore — scoped to `coscientist.agents.contract` + **mandatory AST test in G1**.  
- Taking MCP journal into this track (owned elsewhere — [#78](https://github.com/Naramerk/repo/issues/78); no plan dependency).
