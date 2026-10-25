# CoScientist-next coordination STATE

**Owner:** coordinator `bc-3705a06c-4546-5307-8a1c-27470476f3db`
(https://cursor.com/agents/bc-3705a06c-4546-5307-8a1c-27470476f3db).
Prior: pulse8 `bc-157abd96…` (#197); pulse5 `bc-704a4fbf…`.
Successors: pulse13 RUNNING. Continuous — spawn pulse14 before stop.

Task subagent limits: no CI subscribe / Task-spawn / createPullRequest; **can** `gh pr merge`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:25 Europe/Moscow (UTC+3) |
| `main` tip | `c5cca83` (#198 G14a) |
| Coordinator | https://cursor.com/agents/bc-3705a06c-4546-5307-8a1c-27470476f3db |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **#200** RC9c · **#199** G14a duplicate (DIRTY — do not merge) |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.

## Merged (recent wave)

| PR | Note |
|----|------|
| **#198** | G14a DESIGN/RM v0.8 pin + count test → `c5cca83` |
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 |
| **#189** | G17 → `e1b14b0` |
| **#175** | RC8 → `94ba209` |
| **#191** / **#192** | RC6 post-cap / RC7r |
| **#171** | MCP-J1 |

## Stack status

RC4→RC8 + G8/G14a/G17/G18 **on main**. Next after RC9c: RC10c → RC11c → RC12v → G12.

## In flight / gate drive

| PR | Tip | Gate |
|----|-----|------|
| **#200** RC9c | `d86b1e1` (restack onto `c5cca83`) | dual k=0 + CLEAN → merge |
| **#199** | `daad038` | CONFLICTING after #198 — close; do not merge |

## Next

1. Dual-review #200 tip `d86b1e1` → merge when CLEAN
2. Close #199
3. Parent: open+merge STATE pulse12 (`cursor/coordination-state-pulse12-f3db`)
4. After RC9c: RC10c (#106) one author
5. Mac fixtures blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
