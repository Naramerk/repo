# Plan: CAPABILITIES #1 (SPEC → ResearchSpec) + #3 (LIT literature)

**Owner:** this agent — ResearchSpec intake + literature research only.  
**Rules (крокодил):** ≤1 open PR; ≤400 changed lines/PR; stack depth ≤2; **no shims**; check `main` tip before each branch; never merge.  
**Base:** `main` (re-fetch before every PR).

## Hard rule: no Protocols in `coscientist/agents/` until G1

Do **not** define ports/Protocols under `coscientist/agents/` before **G1 `AgentPort`** (agent layer) merges.  
Roles call **`AgentPort.run(role, input, tools, budget)`** with that role’s **`output_model`** (`ResearchSpec` / `LiteratureOut`).  
~~`LlmCompletePort`~~ — **deleted** (duplicate of AgentPort).  
~~`PapersSearchPort` in agents/~~ — **deleted**; MCP tools via `#54` `invoke_tool` adapter only.

## External deps (wait — do not stub)

| Dep | Owner / PR | What we consume |
|---|---|---|
| **AgentPort** (G1) | agent layer / research-cycle **RC2** (#51) | `tz_spec` + researcher — **blocked until G1 merges** |
| **RoleConfig** | profiles **G2** (#49 plan) | model/prompt/tools/**schema_retries** — no local copy |
| **Need** / `plan.literature` | research-cycle **RC3** (#51; “G4”) | LIT when `literature.needed`; **no** Need stub |
| **HITL `spec_approval`** | quality-hitl **G5** (#52) | after SPEC — **no** local HITL hook |
| **MCP→Tool** | #54 `tools.mcp_adapter` | `papers_search` / paper-analysis via `invoke_tool` |
| **`quote_in_source`** | `checks/kinds/quote.py` (main) | reuse — do not reimplement |
| **Graph writer** | graph / research-cycle | we emit typed outs; writer creates nodes/edges |

## Acceptance anchors

- **#1** — `tz_spec` @ SPEC → `ResearchSpec{object,goal,scope,constraints,success_metrics}` schema-valid; `spec_approval` via HITL (#52 G5).
- **#3** — `LIT-n` (`method=literature_search`) when `plan.literature.needed`: ≥5 `artifact(role=paper)` with DOI/URL (dedupe); every claim ≥1 `SourceQuote` via `quote_in_source`; 0 unsourced; ≥1 `parsed`; failed download → `download_failed` (kept). Graph writer upserts paper/evidence/claim nodes.

## Planned PRs

| ID | Title | Scope | Depends |
|---|---|---|---|
| **SL1** | feat(agents): ResearchSpec models | Frozen `ResearchSpec` + Goal/Constraint/SuccessMetric; **no Protocol**. **This PR.** | `main` |
| **SL2** | feat(agents): ResearchSpecOut + questions | `questions[]` ≥1; adapter for `questions_from_spec_output`. | SL1 |
| **SL3** | feat(agents): validate_research_spec | Pure validate → typed refuse. | SL2 |
| **SL4** | feat(agents): tz_spec via AgentPort | `AgentPort.run(..., output_model=ResearchSpecOut)`; **schema_retries from RoleConfig** (not hardcoded). | SL3; **G1 AgentPort**; RoleConfig |
| **SL5** | (blocked) spec_approval | HITL **G5** (#52) owns it — handoff only. | #52 G5 |
| **SL6** | feat(agents): LiteratureOut + paper draft | Typed papers/quotes/claims; stance forbidden on input. | `main` |
| **SL7** | feat(agents): paper DOI/URL identity | Pure normalize + dedupe; refuse blank identity. | SL6 |
| **SL8** | feat(agents): validate_literature_out | #3 ACs; SourceQuote via **`quote.py`**. | SL6–SL7 |
| **SL9** | feat(agents): researcher via AgentPort | `AgentPort.run("researcher", …, output_model=LiteratureOut)` + MCP tools via `invoke_tool`. | SL8; **G1**; #54 |
| **SL10** | feat(agents): artifact(role=paper) | Register papers (`parsed` \| `download_failed`); never drop failed. | SL9 |
| **SL11** | feat(graph-consumer): LIT nodes | Consumer of graph writer: paper/evidence/claim nodes. | SL10; graph writer |
| **SL12** | test: #3 AC ≥5 papers + dedupe | Acceptance. | SL10 |
| **SL13** | test: #3 AC claims + quote | Acceptance via `quote.py`. | SL8, SL10 |
| **SL14** | test: #3 AC parsed + download_failed | Acceptance. | SL10 |
| **SL15** | test: SPEC→LIT e2e (fakes) | After **G1**, **Need RC3**, **HITL G5** — **no shims**. | SL4, SL9–SL14; G1; RC3; #52 |

## Cancelled

- ~~LlmCompletePort / any LLM Protocol in agents/~~ → **AgentPort.run + output_model**
- ~~PapersSearchPort in agents/~~ → **#54 invoke_tool**
- ~~local RoleConfig~~ → profiles G2
- ~~hardcoded schema retry~~ → RoleConfig/config
- ~~local spec_approval~~ → #52 G5
- ~~Need / lit-needed shim~~ → RC3

## Per-PR checklist

1. `git fetch origin main` — branch from tip (stack ≤2).
2. Diff ≤400 lines; **no new Protocol in `agents/` until G1**.
3. Local pytest + perf + `audit_heuristics.py --vs-main`.
4. Close previous own PR before opening next (≤1 open).
