# CoScientist-next coordination STATE

**Owner:** coordinator `bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9`
(https://cursor.com/agents/bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9).
Successor **pulse10** already RUNNING:
`bc-1bbeb979-a89c-5b9f-8824-253d88680e82`. Prior: `bc-704a4fbf…` → pulse6–8.
This agent is a **Task subagent**: no CI subscribe / Task-spawn / createPR —
parent + pulse10 own those. `gh pr merge` works when gate passes.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:13 Europe/Moscow (UTC+3) / 14:13 UTC |
| `main` tip | `a881d22` (**#191** RC6 post-cap after **#192 RC7r** @ `ead63eb`) |
| Coordinator | https://cursor.com/agents/bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 · #189 G17 CONFLICTING |

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
| `codegraph init/sync` | OK |
| CodeGraph | `query materialize_foreach` → `foreach.py:213` |
| LeanCTX | `lean-ctx read coscientist/core/plan/foreach.py -m signatures` |

## Merged (this wave)

| PR | Note |
|----|------|
| **#192** | RC7r HITL reject → `ead63eb` (tip `2e76d3e`) |
| **#191** | RC6 post-cap selected-drop → `a881d22` (tip `2b74214`) |
| **#190** | STATE pulse6 → `d01e8df` (superseded by this tip) |
| **#188** | RC6 → `eccaf56` |
| **#182** / **#183** | G18 / RC5 |

**Closed duplicate:** #194 RC7r alt tip `0da80fa` — do not reopen/merge.

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| **#175 RC8** | `8a0b6f5` | **HOLD lifted** (RC7r on main). Contains `a881d22`. MERGEABLE/UNSTABLE; dual tip-match **`8a0b6f5`** (not 015eb4e/5cb9fb7) + CLEAN → **merge next**. |
| **#189 G17** | `6744ec6` | **CONFLICTING** after #191/#192. Author `bc-574724e4` still RUNNING — **do not second-author**. Restack → dual on new tip → merge. |

## RC stack

`RC5 → RC6 (#188+#191) → RC7r (#192) → **RC8 (#175)**` — RC8 unblocked.

## Parent / pulse10 MUST

1. Keep continuous successor (pulse10 live; spawn pulse11 before pulse10 stops).
2. Subscribe CI: `cursor/rc8-hypothesis-verdict-1681`, `cursor/graph-g17-toolcalled-1681`, `cursor/coordination-state-pulse9-a1b9`.
3. Dual k=0 on RC8 tip `8a0b6f5` (re-confirm agents may still say HOLD — **merge when k=0 + CLEAN**).
4. Open PR for this STATE tip → merge docs.
5. After G17 author restacks: dual + merge #189.
6. Soft cap: no more product writers.

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
