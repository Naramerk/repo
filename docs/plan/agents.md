# Agent layer plan (owner: AgentPort / Orchestrator)

Links: [DESIGN §7.2](../DESIGN.md) (`AgentPort` — **owner: this agent**), [DESIGN §6.0](../DESIGN.md) (MCP), [CAPABILITIES #5/#6/#18/#23/#24](../CAPABILITIES.md).  
Depends on: thin LLM (`coscientist/llm`), tools `invoke_tool` / gate / registry, [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (open), [#62](https://github.com/Naramerk/repo/pull/62) @ `ebfde35` — `RoleName = Identifier` in [`coscientist.profiles`](https://github.com/Naramerk/repo/blob/ebfde3515d47fe7d6a5ffe4bbe719a460cc033e8/coscientist/profiles/models.py) (`from coscientist.profiles import RoleName`), [#50](https://github.com/Naramerk/repo/pull/50) PromptRegistry.  
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
- Open: [#54](https://github.com/Naramerk/repo/pull/54) MCP adapter (**not merged**); [#62](https://github.com/Naramerk/repo/pull/62) @ `ebfde35` exports `RoleName`.
- `coscientist/agents/` = boundary shell only.

## Сделано

- Prior main: LLM client, import-linter agents boundaries, tools gate/registry.
- (#54 in review only — not landed.)

## Dependency graph

```text
#62 ebfde35 RoleName / RoleConfig / RouteConfig ──► G1 (role: RoleName) + G1b
G1  AgentPort contract (coscientist.agents.contract) ──► G1-impl
#54 MCP adapter                         ──► G1-impl (MCP tools via invoke_tool)
#50 PromptRegistry                      ──► G1-impl (PromptRef; no duplicate PR)
G1 / G1-impl (AgentRun.usage)           ──► scheduler S1/S2 (consume Usage → UsageRecorded)
MCP journal Mcp*                        ──► hand off (#54 follow-up / #60)
```

Edge direction: **agents produce `Usage` on `AgentRun`; scheduler S1/S2 depend on that and own `UsageRecorded`.** G1-impl does **not** depend on S1/S2.

## Следующий PR

### G1 — `AgentPort` contract (~≤250 LOC)
**Depends:** **#62** (`RoleName` at `coscientist.profiles`, landed on #62 @ `ebfde35`).  
**Where:** module **`coscientist.agents.contract`** (Protocol + `AgentRun` / `AgentError` + fakes). Delete `Any` stub in `core/events/ports.py` in this same PR.  
**Role:** `role: RoleName` via `from coscientist.profiles import RoleName`.  
**Tools:** `tools: list[Tool]` with `Tool` from `coscientist.tools.contract`.  
**import-linter (G1 lands pyproject; this docs PR does not prove lint-imports):** see decision below + DESIGN §7.2. Coordinated with tools track [#65](https://github.com/Naramerk/repo/pull/65) (DESIGN §6.1 / agents↔`invoke_tool` boundary on that PR).  
**Usage/Budget:** return `llm.types.Usage`; accept domain `Budget`.  
**Acceptance:** mypy-clean; fake port tests; no orchestrator/journal/MCP; audit `--vs-main`; **`lint-imports` green after pyproject change**; CHANGELOG.  
**DoD:** below + **GitHub CI green** + **lint-imports proves the targeted ignores**.

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
from coscientist.profiles import RoleName          # #62 @ ebfde35; RoleName = Identifier
from coscientist.tools.contract import Tool       # typing; see ignore_imports below

class AgentError(Exception):
    """Typed agent failure — no AgentRun.error: str|None field."""

class AgentRun(BaseModel):  # frozen, extra=forbid
    output: BaseModel | None
    usage: Usage                 # llm.types.Usage; scheduler S1/S2 record UsageRecorded
    tool_calls: list[ToolCallRef]

class AgentPort(Protocol):
    async def run(
        self,
        role: RoleName,
        input: BaseModel,
        tools: list[Tool],
        budget: Budget,
    ) -> AgentRun: ...
```

## import-linter decision (G1 implements pyproject — not verified in this docs PR)

Contract `agents must use invoke_tool (no contract / registry / gate)` (`pyproject.toml`):

- **Do not** set `allow_indirect_imports = true` (that would open every transitive path to `tools.gate` / `taint` / `tool_registry`).
- **Targeted `ignore_imports`** only (`unmatched_ignore_imports_alerting = "error"`):

```toml
ignore_imports = [
  # Tool typing on AgentPort (coscientist.agents.contract only)
  "coscientist.agents.contract -> coscientist.tools.contract",
  # invoke_tool lives in tools.effects, which imports contract/gate/registry;
  # ignore those edges in this contract so agents may import tools.effects
  # without allowing direct agents→gate/taint/registry.
  "coscientist.tools.effects -> coscientist.tools.contract",
  "coscientist.tools.effects -> coscientist.tools.gate",
  "coscientist.tools.effects -> coscientist.core.execution.tool_registry",
]
```

Direct `agents → tools.gate` / `tools.taint` / `tool_registry` stay forbidden. Runtime: **`invoke_tool` only** (AST boundary tests).  

Tools-side record: [#65](https://github.com/Naramerk/repo/pull/65) (tools owner; DESIGN §6.1 invoke_tool / agents boundary). G1 DoD must run `lint-imports` after editing pyproject.

## Вопросы

1. Scheduler S1/S2 PR numbers when that plan opens.

## Проверки (DoD — every PR)

| Check | Rule |
|---|---|
| Scope | One task; ≤400 changed lines; stack depth ≤2; ≤1 open PR |
| Tests | Every new function; no shims/stubs/noop/“later” |
| Gates | `audit --vs-main` clean; `mypy coscientist` |
| **lint-imports** | **G1+:** green **after** pyproject ignores land (this docs PR does not change pyproject — no proof here) |
| Runtime | `pytest -m "not perf"` + `pytest -m perf` (p99 ≤ 50ms) once |
| **CI** | **GitHub CI green required** |
| Docs | CHANGELOG + DESIGN/CAP link; never merge from agent |

## Риски

- Starting G1 before #62 merges `RoleName` to main.  
- Broad `allow_indirect_imports` — rejected; keep targeted ignores only.  
- Direct `tools.contract` abuse via ignore — scoped to `coscientist.agents.contract` + AST tests.  
- Taking MCP journal into this track (handed off).
