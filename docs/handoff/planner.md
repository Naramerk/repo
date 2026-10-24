# Handoff: planner.md (scheduler / usage / budgets)

Filename: `docs/handoff/planner.md`. Authoritative PR list:
[`docs/plan/scheduler.md`](../plan/scheduler.md) (**S0–S16 + N1**).

Links: [CAPABILITIES #4/#5/#23/#24/#26](../CAPABILITIES.md), [DESIGN §7](../DESIGN.md),
[agents plan #66](https://github.com/Naramerk/repo/pull/66) (G1 / G1b).

## Область

Owns **`Usage` / `UsageRecorded` / `Budget*`** and the **single `effective_deadline`**.
Calls agent-layer **G1b Orchestrator**; maps **G1 `AgentError` → attempt outcomes**
(no own AgentPort). Owns **N1** (Budget strict) as a scheduler-plan task
(planner agent; **not** G2 / not #62).

**`PromptRegistry` is owned by the agent layer** (#50). Do not fork.

Out of scope: Profile/RoleConfig (G2 [#62](https://github.com/Naramerk/repo/pull/62));
LLM/MCP clients; execution graph; research-cycle (#51, including **RC17a**
`max_attempts`); Alembic/Coder.

Rules: ≤1 open PR/agent, ≤400 lines, stack ≤2, no shims, contracts first.
**Green CI is DoD.**

**WIP:** [#70](https://github.com/Naramerk/repo/pull/70) branch
`cursor/prompt-registry-docs-ec93` is the **docs split of #50** — docs here;
code on #50. Not two implementation PRs.

## Already in main (extend, do not rewrite)

| Already on `main` | Where |
|-------------------|--------|
| `BudgetDebited`, `BudgetExceeded`, `BudgetSharesAdjusted`, `BudgetSharesRefused` | `coscientist/core/events/events.py` |
| Plan events | same `EventType` |
| `plan_validate` | `coscientist/core/plan/plan_validate.py` |
| `LifecycleMixin.accept_plan` | `coscientist/core/plan/lifecycle.py` |
| `Study.effective_deadline()` | `coscientist/core/execution/domain.py` |
| `llm.types.Usage` | `coscientist/llm/types.py` |
| `Budget` defaults 100 / 1_000_000 / 500 | `domain.py` — remove in **N1** |

**S5–S7** and **S11** **extend** these. **S10** = **new** PlanOut/CriticVerdict schemas
(not an extend). **S10 ≠ #51** (RC owns HYP/verdict). **`max_attempts=3`** → **RC17a** in #51.

## Состояние

- **#50** (`cursor/prompt-registry-ec93`): PromptRegistry code (agent layer) — open
- **#70** (`cursor/prompt-registry-docs-ec93`): this handoff + `scheduler.md` — open
- Next code after docs land: **N1** Budget strict (planner agent; Depends:
  **#62 merged** only) → then **S1** `UsageRecorded`.
  G2 **P3** Depends: **N1 merged** only.

## Сделано

- (none on `main` yet for this plan — #50 / #70 still open)

## Следующий PR

**N1** — Budget strict, no defaults (`domain.py` drop 100/1_000_000/500);
owner: planner agent; Depends: **#62 merged**. Blocks G2 P3.
Then **S1** — `UsageRecorded` EventType.

## Контракты

### Consumed

- Agent layer: **G1** AgentPort/AgentError, **G1b** Orchestrator, PromptRegistry
- G2 (#62): Profile / RoleConfig (P3 waits on **merged N1**)
- Core: `plan_validate`, `LifecycleMixin.accept_plan`, Budget* EventTypes, `effective_deadline()`
- LLM: `llm.types.Usage`
- RC (#51 **RC17a**): `max_attempts` (do not own here)

### Owned

- UsageRecorded / UsageLedger / PricingTable / `cost_unverified`
- Budget accounting extensions → existing `BudgetExceeded`
- **N1** Budget field required (no defaults)
- `effective_deadline` refuse helpers (no clamp); S6→S8
- AgentError → attempt outcome map (after G1)

## Вопросы (with owners)

1. **`UsageRecorded.cost_rub` / pricing path** — **owner: this area (S3)**.
2. **`max_plan_revisions`** — **owner: this area (S14)**. Field is **required in
   config**; missing → **typed refusal**. No guessed default. Schema stays here
   unless a later merged G2 change moves it; until then this area owns it.

## Проверки (DoD)

Green **GitHub CI** after push; CHANGELOG; CAP/DESIGN link in PR body; locally:

```bash
python3 -m pytest -q -m 'not perf'
python3 -m pytest -q -m perf -s    # gate_p99 ≤ 50 ms
python3 scripts/audit_heuristics.py --vs-main
lint-imports
git fetch origin main
git merge-base --is-ancestor origin/main HEAD   # merge-ref vs current main
git push
```

## Риски

- Forking PromptRegistry / G1/G1b / RoleConfig
- Dual deadline or clamp
- Rewriting M1 Budget* / `plan_validate`
- Guessed `max_plan_revisions` or Budget defaults; estimating tokens; stack>2; LOC>400
- Taking `max_attempts` instead of leaving it to RC17a (#51)
- G2 P3 before N1
- Assigning N1 to #62 (cycle; N1 is this plan’s PR)
