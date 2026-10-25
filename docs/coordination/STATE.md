# CoScientist-next coordination STATE

**Owner:** coordinator `bc-3705a06c-4546-5307-8a1c-27470476f3db`
(https://cursor.com/agents/bc-3705a06c-4546-5307-8a1c-27470476f3db).
Prior: pulse8 `bc-157abd96…` (#197); pulse11 `bc-f305b1d7…`; pulse5 `bc-704a4fbf…`.
Successors: pulse13 `bc-d50ef046…` / `bc-2b1a7c00…` RUNNING. Continuous — spawn pulse14 before stop.

Task subagent limits: no CI subscribe / Task-spawn / createPullRequest; **can** `gh pr merge`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:21 Europe/Moscow (UTC+3) |
| `main` tip | `e35ea2f` (#197 STATE pulse8) |
| Product tip | `e1b14b0` (#189 G17; RC8 `94ba209`) |
| Coordinator | https://cursor.com/agents/bc-3705a06c-4546-5307-8a1c-27470476f3db |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | none yet — drive RC9c + prefer one G14a |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.

## Merged (recent wave)

| PR | Note |
|----|------|
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 → `4bc5c2c` |
| **#189** | G17 ToolCalled → tool_call (schema v0.8 / 15 nodes) → `e1b14b0` |
| **#175** | RC8 hypothesis_verdict matrix → `94ba209` |
| **#191** | RC6 post-cap selected refuse → `a881d22` |
| **#192** | RC7r HITL reject → `ead63eb` |
| **#188** | RC6 selected hyp |
| **#182** | G18 mcp_exchange |
| **#171** | MCP-J1 |

## Stack status

RC4→RC5→RC6→RC7r→RC8 **on main**. Graph G8/G17/G18 **on main**.
Next product: **RC9c** (#105) → RC10c → RC11c → RC12v → G12.
G14a residual: pin prose + fail-closed count test (prefer tip with test).

## In flight

| Stream | Note |
|--------|------|
| RC9c | branch `cursor/rc9c-hypothesis-proposed-1681` (tip still = main); sole author **`bc-390a60f6`** — do not second-author (`bc-780e5126` duplicate) |
| G14a | prefer `cursor/graph-g14a-design-counts-1681` @ `0eaa358` (v0.8 pin + schema count test); drop parallel `…-v08-1681` |
| Successor pulse13 | already RUNNING |

## Next

1. Parent: open PR for this STATE branch → merge when CLEAN
2. Open RC9c PR when tip has commits → dual k=0 → merge
3. Open+dual+merge preferred G14a tip (or close if superseded no-op after review)
4. After RC9c: RC10c (#106) one author
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
