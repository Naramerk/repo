# Plan: profiles #12/#13

**P2** owner: `Profile`/`RoleConfig`/`RouteConfig` (G2). ≤1 open PR, ≤400 lines, stack ≤2, no shims.

## Dependencies

| Dep | Owner | Note |
|---|---|---|
| Budget strict/no defaults | **N1 (#70)** | No RouteConfig wrapper. P3 depends on N1. |
| PromptRef | #50 | waits |
| AgentPort/catalog | G1/#46 | role keys: `Identifier` from `mcp.config` (not `str`) |
| MCP strict+`Identifier` | **#77** | P2 stacked on #77; `Identifier` from `mcp.config` only |

## Acceptance (old CoScientist)

| ID | Mirrors |
|---|---|
| **P6a/b** | `MedicalAgent`+`med_tools.py` |
| **P7a** | `test_microfluidics_assembly.py` |
| **P7b** | `test_microfluidics_e2e.py` |
| **P7c** later | HITL |
| **P8** | R1 |

Live/cassettes required; fakes alone do not close.

## Stack (≤2)

| PR | Scope | Depends |
|---|---|---|
| **#77** | MCP strict + shared `Identifier` | `main` |
| **P2** | frozen profile models | ← #77 |
| **P3** | YAML loader — see below | ← P2 + N1 (#70) |
| **P4** | medicine+microfluidics YAML | ← P3 |
| **P5** | unknown→API refuse (M5) | ← P3 |
| **P6/#12** | acceptance | P4+AgentPort+med MCP |
| **P7/#13** | acceptance | P4+AgentPort |
| **P8** | R1 root | P6a+P7a |

## P3 — YAML loader (owner: G2)

**Owner:** G2 (medicine/microfluidics profiles track — this agent).  
**Scope:** packaged `profile.yaml` → `Profile` via `model_validate_json` only (no list→tuple shim).  
**Depends:** P2 models (#62) + N1 Budget strict (#70).  
**Out:** medicine/microfluidics YAML contents (P4); API unknown-profile refuse (P5).

Out of scope for the area: #46, AgentPort, #50. Gate: pytest · perf · audit --vs-main
