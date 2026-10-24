# Plan: profiles (CAP #12 / CAP #13)

**G2** = medicine/microfluidics profiles track (cloud agent for this area).  
**P2** owner: `Profile`/`RoleConfig`/`RouteConfig` (G2). ≤1 open PR, ≤400 lines, stack ≤2, no shims.

## Dependencies

| Dep | Owner | Note |
|---|---|---|
| Budget strict/no defaults | **N1 ([#129](https://github.com/Naramerk/repo/issues/129))** | Scheduler S1/S2 / Budget wiring (replaces closed #70). No RouteConfig wrapper. P3 depends on N1. |
| PromptRef | [#50](https://github.com/Naramerk/repo/pull/50) | waits |
| AgentPort/catalog | G1 / [#65](https://github.com/Naramerk/repo/pull/65) | role keys: `Identifier` from `mcp.config` (not `str`); G3 catalog replaces closed #46 |
| MCP strict+`Identifier` | **[#77](https://github.com/Naramerk/repo/pull/77)** | P2 stacked on #77; `Identifier` from `mcp.config` only |

## Acceptance (old CoScientist)

| ID | Mirrors |
|---|---|
| **P6a/b** | `MedicalAgent`+`med_tools.py` |
| **P7a** | `test_microfluidics_assembly.py` |
| **P7b** | `test_microfluidics_e2e.py` |
| **P7c** | microfluidics HITL — handoff to [#93](https://github.com/Naramerk/repo/issues/93) (HITL / CAP #18 engine) via quality-hitl **HE5a** ([plan](quality-hitl.md#hitl-18--engine-this-agent)); not G2 |
| **P8** | R1 |

Cassettes required; fakes alone do not close.

## Stack (≤2)

| PR | Scope | Depends |
|---|---|---|
| **#77** | MCP strict + shared `Identifier` | `main` |
| **P2 (#62)** | frozen profile models | ← #77 |
| **P3** | YAML loader — see below | ← P2 + N1 ([#129](https://github.com/Naramerk/repo/issues/129)) |
| **P4** | medicine+microfluidics YAML | ← P3 |
| **P5** | unknown→API refuse (M5) | ← P3 |
| **P6 (CAP #12)** | acceptance | P4+AgentPort+med MCP |
| **P7 (CAP #13)** | acceptance | P4+AgentPort |
| **P8** | R1 root | P6a+P7a |

## P3 — YAML loader (owner: G2)

**Owner:** G2 (medicine/microfluidics profiles track).  
**Scope:** packaged `profile.yaml` → `Profile` via `model_validate_json` only (no list→tuple shim).  
**Depends:** P2 (#62) + N1 Budget strict ([#129](https://github.com/Naramerk/repo/issues/129)).  
**Out:** medicine/microfluidics YAML contents (P4); API unknown-profile refuse (P5).

Out of scope for the area: [#65](https://github.com/Naramerk/repo/pull/65) catalog, AgentPort, #50, P7c HITL ([#93](https://github.com/Naramerk/repo/issues/93)). Gate: pytest · perf · audit --vs-main
