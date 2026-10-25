# CoScientist-next coordination STATE

**Owner:** coordinator `bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9`
(https://cursor.com/agents/bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9).
Prior: `bc-704a4fbf…` → pulse6–8 (`bc-b855755c…` / `bc-5b7a4e5e…` /
`bc-2d2097c2…` / `bc-157abd96…`). This agent is a **Task subagent**: no CI
subscribe, no Task-spawn, no reliable PR create/merge API — **parent must**
subscribe CI, open/merge PRs when gate passes, and spawn successor **pulse10**
before this agent stops.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:09 Europe/Moscow (UTC+3) / 14:09 UTC |
| `main` tip | `d01e8df` (**#190** STATE pulse6 after **#188 RC6** @ `eccaf56`) |
| Coordinator | https://cursor.com/agents/bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #192 RC7r · #191 RC6 post-cap · #189 G17 · #175 RC8 HOLD |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). Dual independent
`review-ready` (k=0) on **current tip** + `MERGEABLE`+`CLEAN` + required CI
green + stack order. Soft cap ≤6 write streams; uncapped read-only dual
reviewers. One author per write branch.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | Indexed; sync OK (exit 0) |
| CodeGraph | `query materialize_foreach` → `foreach.py:213` |
| LeanCTX | `lean-ctx read coscientist/core/plan/foreach.py -m signatures` |

## Merged (recent window)

| PR | Note |
|----|------|
| **#190** | STATE pulse6 → `d01e8df` (stale content; this tip supersedes) |
| **#188** | RC6 selected hyp ≥1 task → `eccaf56` |
| **#182** | G18 MCP fold → `bac4c62` |
| **#183** | RC5 hypotheses artifact → `882756b` |
| #185 #186 #177 #178 #176 | prior pulse window |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| **#192 RC7r** | `2e76d3e` | MERGEABLE/UNSTABLE; based on `eccaf56` (missing #190 docs-only). Dual k=0 on tip after CLEAN → merge. Author: `bc-238e64cf` (+ peers) — **one author**. |
| **#191 RC6 post-cap** | `2b74214` | MERGEABLE/UNSTABLE on `d01e8df`. Fixes fail-open after #188 (cap drops selected). Dual k=0 + CLEAN → merge (independent of RC7r). |
| **#189 G17** | `6744ec6` | Was `7fa4677` CI-red (import-linter); tip moved — MERGEABLE/UNSTABLE. Duals must tip-match **`6744ec6`** (not 7fa4677). Author: G17 fix `bc-574724e4` — **do not second-author**. |
| **#175 RC8** | `015eb4e` | Restacked onto `d01e8df` (was `5cb9fb7`). **HOLD** until **#192 RC7r** merges; then dual k=0 on then-tip → merge. |

## RC stack (strict)

`RC5 (#183) → RC6 (#188) → **RC7r (#192 / #103)** → RC8 (#175)`

1. Dual + merge **#192** when CLEAN + tip-match k=0.
2. Restack **#175** if tip drifts; dual + merge.
3. **#191** and **#189** can merge in parallel with RC7r (no stack dep) once gated.

## In flight (do not duplicate writers)

| Stream | Agent / note |
|--------|----------------|
| RC7r | `bc-238e64cf` (preferred); also `bc-ff968929`, `bc-45bb8130` — enforce one author |
| G17 CI fix | `bc-574724e4` tip `6744ec6` |
| Dual G17 | several on stale `7fa4677` — re-target or re-spawn on `6744ec6` |
| RC6 post-cap | tip `2b74214` (#191); authors IDLE |
| Soft cap | write streams overloaded — **spawn reviewers/successor only**, not more product writers |

## Parent MUST (Task-subagent limits)

1. **Spawn successor pulse10** continuous coordinator before this agent stops.
2. Subscribe CI: `cursor/rc7r-hitl-reject-1681`, `cursor/rc6-post-cap-selected-fix-1681`, `cursor/graph-g17-toolcalled-1681`, `cursor/rc8-hypothesis-verdict-1681`.
3. Spawn dual independent reviewers (k=0) on **current** tips when CI greens: #192 `2e76d3e`, #191 `2b74214`, #189 `6744ec6`.
4. Merge under AGENTS gate immediately when dual + CLEAN + tip-match; preserve RC stack for #175.
5. Open PR for this STATE tip if createPullRequest blocked here.

## External blocker

Mac `runs_final_20261023` — document only; do not invent fixtures.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
