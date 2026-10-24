# Plan: scheduler / usage / budgets (CAPABILITIES #4, #5, #23, #24, #26)

Links: [CAPABILITIES #4/#5/#23/#24/#26](../CAPABILITIES.md), [DESIGN §7](../DESIGN.md)
(agent layer / Orchestrator), [DESIGN §7.4 Budget](../DESIGN.md), [agents plan #66](https://github.com/Naramerk/repo/pull/66)
(G1 `AgentPort` / G1b Orchestrator).

Owner rules: **≤1 open PR per agent**, **≤400 lines**, **stack ≤2**, **no shims**,
check `main` first, contracts first. **Green CI is DoD.** Never merge from agents.

**WIP:** [#70](https://github.com/Naramerk/repo/pull/70) (`cursor/prompt-registry-docs-ec93`)
is the **docs split of #50** (code on #50; this PR docs-only).

**PromptRegistry** is **agent-layer owned** (code PR #50). This plan owns
Usage / Budget / `effective_deadline` follow-ups (this docs PR #70).

**N1** is a **scheduler-plan task** owned by the **planner agent** (not G2 / not #62).
N1 Depends: **merged [#62](https://github.com/Naramerk/repo/pull/62)** only.
G2 **P3** Depends: **merged N1** only (stack depth ≤2).

---

## Ownership (authoritative)

| Surface | Owner |
|---------|--------|
| **`Usage` / `UsageRecorded` / `Budget*`** | **This area** |
| **Single `effective_deadline`** | **This area** (typed **refuse** if exceeded — **no clamp**) |
| **`Orchestrator`** | **Agent layer G1b** (#66) — call it |
| **`AgentError` → attempt outcomes** | **This area maps**; depends on **G1** AgentError (#66); **no own AgentPort** |
| **`PromptRegistry`** | **Agent layer** (#50) |
| **`Profile` / `RoleConfig`** | **G2 profiles ([#62](https://github.com/Naramerk/repo/pull/62))** |
| **N1 Budget strict** | **This area** (planner agent) |
| Core plan validate / accept | Reuse **`plan_validate`** + `LifecycleMixin.accept_plan` |
| Provider tokens | Reuse **`llm.types.Usage`** |
| **`max_attempts` hard `=3`** | **RC17a** in research-cycle [#51](https://github.com/Naramerk/repo/pull/51) — **not this area** |

### Edges / consumers

| Edge | Note |
|------|------|
| **S6 → S8** | Cost/wall-time budget (S6) uses **`effective_deadline` refuse helpers (S8)** — no parallel clock |
| **S8 → #46** | Job timeouts share the same refuse helpers |
| **#52** | Consumer of `effective_deadline` (checks/HITL timing) |
| **N1 → #62** | N1 lands only after **#62 is merged** |
| **P3 → N1** | G2 [#62](https://github.com/Naramerk/repo/pull/62) **P3** (YAML loader) lands only after **N1 is merged** |

---

## Already in main — **extend**, do not rewrite

CAPABILITIES **#4 / #23 / #26** are **M1-era**. **S5–S7** and **S11** must
**extend** existing Budget* / `plan_validate` / plan events — never rewrite them.
**S10** is **new** PlanOut/CriticVerdict schemas (not an extend of those surfaces).

| Artifact | Location |
|----------|----------|
| `BudgetDebited`, `BudgetExceeded`, `BudgetSharesAdjusted`, `BudgetSharesRefused` | `coscientist/core/events/events.py` |
| Plan events (`PlanAccepted`, `PlanExpanded`, `ExpansionRefused`, `PlanRefused`, loops) | same |
| `plan_validate` | `coscientist/core/plan/plan_validate.py` |
| `LifecycleMixin.accept_plan` | `coscientist/core/plan/lifecycle.py` |
| `Study.effective_deadline()` | `coscientist/core/execution/domain.py` |
| `llm.types.Usage` | `coscientist/llm/types.py` |
| `Budget` field defaults `100` / `1_000_000` / `500` | `domain.py` — **remove in N1** (strict, no defaults) |

**S10 vs #51:** research-cycle [#51](https://github.com/Naramerk/repo/pull/51) owns HYP/verdict
RC stack — **not** PlanOut/CriticVerdict. S10 = **new** frozen planner I/O schemas only;
no overlap (RC dropped stage-criterion duplicate of `plan_validate`).
**`max_attempts=3`** → **RC17a** in #51 (not scheduler).

---

## CAPABILITIES acceptance

| # | Acceptance |
|---|------------|
| **#4** | route, tools≥1, criteria≥1, unique EXP-n — extend, don’t replace `plan_validate` |
| **#5** | Typed critic verdict; revise ≤ **required** `max_plan_revisions` (config; missing → typed refuse); CLI/API/headless via **G1b Orchestrator** |
| **#23** | Model/fallback from G2 (#62); usage per call via `UsageRecorded` ← `llm.types.Usage` |
| **#24** | Budget calls/tokens/₽/time; exceed → `BudgetExceeded`; one `effective_deadline`; **N1** drops guessed Budget defaults |
| **#26** | Tokens+₽ by study/task/role/model; unknown price → `cost_unverified` |

---

## Small PR list (S0–S16 + N1)

Stack ≤2. Base every code PR on `main`.

**N1** = this plan’s Budget-strict PR (planner-agent owner; **not** part of #62).

| ID | Title | Scope | Depends | LOC |
|----|-------|-------|---------|-----|
| **S0** | docs #70 | This file + `docs/handoff/planner.md` (`cursor/prompt-registry-docs-ec93`) | main | docs |
| **N1** | Budget strict, no defaults | Remove `Budget` defaults `max_llm_calls=100`, `max_tokens=1_000_000`, `max_tool_calls=500` (`domain.py`); fields required (DESIGN §7.4); typed refuse if missing. **Blocks G2 P3.** | **#62 merged** | ≤300 |
| **S1** | EventType `UsageRecorded` | Type + gate tests; align `llm.types.Usage` | main | ≤200 |
| **S2** | UsageRecorded builder | Map provider Usage → payload; refuse estimates | **S1 merged** | ≤250 |
| **S3** | PricingTable | ₽/1M tokens from YAML; missing → no price | main | ≤250 |
| **S4** | UsageLedger + `cost_unverified` (#26) | Totals study/task/role/model | **S2 merged**, **S3 merged** | ≤350 |
| **S5** | BudgetTracker calls/tokens (#24) | Extend study `Budget` + `BudgetExceeded` | **S4 merged**, **N1 merged** | ≤350 |
| **S6** | BudgetTracker cost ₽ + wall time (#24) | `max_cost_rub` + time vs **S8 `effective_deadline`** | **S5 merged**, **S8 merged** | ≤300 |
| **S7** | Attempt/role budget scopes (#24) | Per-attempt / per-role ceilings | **S6 merged** | ≤300 |
| **S8** | `effective_deadline` refuse helpers | deadline > effective → typed refuse (**no clamp**); edges #46, #52 | main | ≤300 |
| **S9** | AgentError → attempt outcome | Map **G1** `AgentError` → attempt close; no port | **G1 (#66) merged** | ≤250 |
| **S10** | PlanOut / CriticVerdict schemas | **New** frozen I/O + tests (not #51 RC; not an extend of M1 plan events) | main | ≤300 |
| **S11** | #4 checks + convert → `plan_validate` | route/tools/criteria/EXP-n; call `plan_validate` (**extend**) | **S10 merged** | ≤350 |
| **S12a** | EventType `PlanDraftRecorded` | Type + gate tests | main | ≤150 |
| **S12b** | EventType `PlanCritiqueRecorded` | Type + gate tests | **S12a merged** (stack≤2) | ≤150 |
| **S12c** | EventType `PlanDecisionRecorded` | Type + gate tests | **S12b merged** (stack≤2) | ≤150 |
| **S12d** | Plan lifecycle builders | Draft/critique/decision helpers | **S12a–c merged**, **S10 merged** | ≤300 |
| **S13** | Planner via Orchestrator (#5) | Call **G1b Orchestrator**; agent-layer PromptRegistry; `plan_validate`/`LifecycleMixin.accept_plan` | **S11 merged**, **S12d merged**, **G1b (#66) merged** | ≤400 |
| **S14** | `max_plan_revisions` + modes (#5) | **Required** config field (missing → typed refuse); revise loop; CLI/API/headless | **S13 merged** | ≤350 |
| **S15** | Usage report query (#26) | Totals from journal | **S4 merged** | ≤250 |
| **S16** | E2E fake LLM smoke | Toy goal → accept; usage + budget | **S14 merged**, **S7 merged**, **S15 merged** | ≤400 |

---

## Contracts

- New EventTypes before writers; stamp refuses unknown types.
- Usage only from `llm.types.Usage`. Cost: priced rows from PricingTable; unpriced → `cost_unverified`.
- Budget scopes extend existing Budget* journal path; **N1** makes `Budget` fields required.
- Deadline: one `Study.effective_deadline()`; **refuse**, never clamp.
- Call **G1b Orchestrator**; map **G1 AgentError** (S9).
- `max_attempts` → **RC17a / #51**; do not change hard `=3` here.
- G2 **P3** waits on **merged N1** (Budget strict before profile YAML) — Edges only; not a scheduler PR.
- import-linter filesystem-complete lists; no shims; audit hand-markers only.

## Checks (DoD — every PR)

```bash
python3 -m pytest -q -m 'not perf'
python3 -m pytest -q -m perf -s    # gate_p99 ≤ 50 ms
python3 scripts/audit_heuristics.py --vs-main
lint-imports
git fetch origin main
git merge-base --is-ancestor origin/main HEAD   # merge-ref vs current main
git push
```

Plus **green GitHub CI** after push, a **CHANGELOG** entry, and a link to the relevant
**CAPABILITIES / DESIGN** section in the PR body.

## Risks

- Forking PromptRegistry / Orchestrator / RoleConfig
- Clamp or dual deadline (blocks S6/S8 / #46 / #52)
- Rewriting M1 Budget* / `plan_validate`
- Guessed `max_plan_revisions` or Budget field defaults (forbidden — required)
- Estimating tokens; stack>2; LOC>400
- Owning `max_attempts` here instead of RC17a (#51)
- G2 P3 merging before N1 (Budget defaults still guessed)
- Assigning N1 to #62 (cycle; N1 is this plan’s PR)
