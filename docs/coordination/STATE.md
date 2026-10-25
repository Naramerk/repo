# CoScientist-next coordination STATE

**Owner:** coordinator `bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9`
(https://cursor.com/agents/bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9).
Successors live: pulse10 `bc-1bbeb979…`, pulse11 `bc-f305b1d7…`,
pulse12 `bc-3705a06c…`. Prior chain from `bc-704a4fbf…`.
Task-subagent limits: no CI subscribe / Task-spawn / createPR — parent owns
those. `gh pr merge` OK under AGENTS gate.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:17 Europe/Moscow (UTC+3) / 14:17 UTC |
| `main` tip | `e1b14b0` (**#189 G17** after **#175 RC8**) |
| Coordinator | https://cursor.com/agents/bc-036c2b4b-a8c7-53a8-ae66-cada866fa1b9 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **none** (product gate queue clear) |

## Merge policy

Standing grant: dual independent `review-ready` (k=0) on **current tip** +
`MERGEABLE`+`CLEAN` + required CI green + stack order (`AGENTS.md`). Soft cap
≤6 write streams; uncapped dual reviewers. One author per write branch.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK |
| CodeGraph | `query materialize_foreach` → `foreach.py:213` |
| LeanCTX | `lean-ctx read coscientist/core/plan/foreach.py -m signatures` |

## Merged (this wave — verified on main)

| PR | → tip | Note |
|----|-------|------|
| **#189** | `e1b14b0` | G17 ToolCalled → tool_call (`f70cd2a`) |
| **#175** | `94ba209` | RC8 hypothesis_verdict matrix (`8a0b6f5`) |
| **#191** | `a881d22` | RC6 post-cap selected-drop |
| **#192** | `ead63eb` | RC7r HITL reject |
| **#190** | `d01e8df` | STATE pulse6 (superseded by this tip) |
| **#188** / **#182** / **#183** | | RC6 / G18 / RC5 |

**Closed duplicate:** #194 RC7r alt — do not reopen.

## Active backlog (unblocked — spawn next)

| Stream | Issue | Note |
|--------|-------|------|
| **M4 RC** | **#105 RC9c** | HypothesisProposed via G7 — **next**; G7 #64 on main; stacks after RC8 |
| M4 RC | #106 RC10c | after RC9c |
| M4 RC | #107 RC11c | after RC9c |
| M4 RC | #96 RC12v | after RC11c+RC8 — unlocks G12 |
| M4 RC | #108 RC17 / #109 RC20 | after RC stack |
| Graph | G14a / G12 | G12 waits RC12v |
| M5 | #119 R16a.1a / #153 | reporting / API — parallel OK if soft cap allows |

## Parent / pulse12 MUST

1. Keep continuous successor (spawn pulse13 before pulse12 stops).
2. **Spawn one RC9c implementer** on #105 (sole author) → dual → merge.
3. Open+merge PR for this STATE tip (`cursor/coordination-state-pulse9-a1b9`).
4. Soft cap: prefer RC9c writer + duals; avoid duplicate RC9c authors.

## External blocker

Mac `runs_final_20261023` — document only; do not invent fixtures.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
