# Plan: profiles (CAP #12 and CAP #13)

**G2** = medicine/microfluidics profiles track (cloud agent for this area).  
**P2** owner: `Profile`/`RoleConfig`/`RouteConfig` (G2). ≤1 open PR, ≤400 lines, stack ≤2, no shims.

## Dependencies

| Dep | Owner | Note |
|---|---|---|
| Budget strict, no field defaults | **N1** — [§ N1](#n1-budget-strict-owner-ruslan-and) | No RouteConfig wrapper. P3 depends on N1. |
| PromptRef | [#50](https://github.com/Naramerk/repo/pull/50) | waits |
| AgentPort | G1 ([agents plan](agents.md)) | role keys: `Identifier` from `mcp.config` (not `str`) |
| Tool catalog | G3 ([#65](https://github.com/Naramerk/repo/pull/65)) | replaces closed #46 |
| MCP strict+`Identifier` | **[#77](https://github.com/Naramerk/repo/pull/77)** | P2 stacked on #77; `Identifier` from `mcp.config` only |

## Acceptance (old CoScientist)

| ID | Mirrors |
|---|---|
| **P6a/b** | `MedicalAgent`+`med_tools.py` |
| **P7a** | `test_microfluidics_assembly.py` |
| **P7b** | `test_microfluidics_e2e.py` |
| **P7c** | microfluidics HITL — handoff to [§ P7c](#p7c-microfluidics-hitl-owner-ruslan-and); not G2 |
| **P8** | R1 |

Cassettes required; fakes alone do not close.

## Stack (≤2)

| PR | Scope | Depends |
|---|---|---|
| **#77** | MCP strict + shared `Identifier` | `main` |
| **P2 (#62)** | frozen profile models | ← #77 |
| **P3** | YAML loader — see below | ← P2 + N1 |
| **P4** | medicine+microfluidics YAML | ← P3 |
| **P5** | unknown→API refuse (M5) | ← P3 |
| **P6 (CAP #12)** | acceptance | P4+AgentPort+med MCP |
| **P7 (CAP #13)** | acceptance | P4+AgentPort |
| **P8** | R1 root | P6a+P7a |

## N1 Budget strict (owner: Ruslan And)

**Owner:** Ruslan And.  
**Scope:** core `Budget` with no guessed field defaults (strict); scheduler-adjacent, not G2.  
**Why here:** no open issue whose title matches N1 Budget strict (closed #70; [#129](https://github.com/Naramerk/repo/issues/129) is S1+S2 `UsageRecorded` only).  
**P3 depends on this section** (not on #129).

## P7c microfluidics HITL (owner: Ruslan And)

**Owner:** Ruslan And.  
**Deliverable:** `tests/profiles/test_p7c_microfluidics_hitl.py` plus cassettes `tests/profiles/cassettes/p7c_*`.  
**Gate:** microfluidics profile HITL human and timeout paths through `experimental/hitl.py` (quality-hitl **HE5a** surface: [plan](quality-hitl.md#hitl-18--engine-this-agent), CAP #18).  
**DoD:** cassette replay green; fakes alone do not close.  
**Why here:** no open issue whose title matches HE5a or P7c ([#93](https://github.com/Naramerk/repo/issues/93) is G8b graph writes only).  
**Not G2.**

## P3 — YAML loader (owner: G2)

**Owner:** G2 (medicine/microfluidics profiles track).  
**Scope:** packaged `profile.yaml` → `Profile` via `model_validate_json` only (no list→tuple shim).  
**Depends:** P2 (#62) + [N1](#n1-budget-strict-owner-ruslan-and).  
**Out:** medicine/microfluidics YAML contents (P4); API unknown-profile refuse (P5).

Out of scope for G2: G3 catalog ([#65](https://github.com/Naramerk/repo/pull/65)), AgentPort (G1), #50, [P7c](#p7c-microfluidics-hitl-owner-ruslan-and). Gate: pytest · perf · audit --vs-main
