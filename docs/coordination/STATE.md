# CoScientist-next coordination STATE

**Owner:** coordinator `bc-704a4fbf-db89-5945-a0f2-4069baad8e76`
(https://cursor.com/agents/bc-704a4fbf-db89-5945-a0f2-4069baad8e76).
Successors in flight: pulse13–16. Prior: `bc-157abd96…`, `bc-1bbeb979…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:25 Europe/Moscow (UTC+3) |
| `main` tip | `c5cca83` (#198 G14a counts) |
| Coordinator | https://cursor.com/agents/bc-704a4fbf-db89-5945-a0f2-4069baad8e76 |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | #200 RC9c (restack after #198) |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack (`AGENTS.md`). Soft cap ≤6 write streams; one author per branch.

## Merged (recent)

| PR | Note |
|----|------|
| **#198** | G14a DESIGN/RM counts → schema v0.8 / 15/19 → `c5cca83` |
| **#197** | STATE pulse8 |
| **#189** | G17 ToolCalled → tool_call |
| **#175** | RC8 hypothesis_verdict |
| **#192** | RC7r HITL reject |
| **#191** | RC6 post-cap |
| **#188** | RC6 |
| **#183** | RC5 |
| **#182** | G18 mcp_exchange |

## Stack status

RC4→RC8 **on main**. Graph G8/G17/G18 + **G14a docs** on main (schema already v0.8).
**G14a is done** — do not spawn more G14a writers. Stale 13-node branches must not merge.

Next product: **RC9c** (#105) → RC10c / RC11c / RC12v → G12/G15.

## In flight

| Stream | Note |
|--------|------|
| RC9c #200 | tip restack after #198; dual after CLEAN |
| Successor coords | keep chain; do not park |

## Next

1. Restack + dual + merge #200 RC9c
2. RC10c / RC11c / RC12v
3. Mac `runs_final_20261023` — document only (external)

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
