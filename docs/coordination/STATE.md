# CoScientist-next coordination STATE

**Owner:** coordinator `bc-9424e63c-b220-5bfb-b57c-5b2e1148accd`
(https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd).
Successors: `bc-4bd461ef…`, `bc-5b7a4e5e…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:16 Europe/Moscow (UTC+3) |
| `main` tip | `e1b14b0` (#189 G17) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | (none product at pulse) — RC9c + G14a implementers in flight |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). Soft cap ≤6 write streams.

## Merged (recent wave)

| PR | Note |
|----|------|
| **#189** | G17 ToolCalled → tool_call (schema v0.8 / 15 nodes) → `e1b14b0` |
| **#175** | RC8 hypothesis_verdict matrix → `94ba209` |
| **#191** | RC6 post-cap selected refuse hotfix → `a881d22` |
| **#192** | RC7r HITL reject → `ead63eb` |
| **#190** | STATE pulse6 |
| **#188** | RC6 selected hyp ref |
| **#183** | RC5 |
| **#182** | G18 mcp_exchange |
| **#177** | TG-Call |
| **#185** | R16a.4 |

## Stack status

RC4→RC5→RC6→RC7r→RC8 **on main**. Graph G8/G17/G18 **on main**. Next: **RC9c** (#105), **G14a** (DESIGN counts), then RC10c/RC11c/RC12v → G12.

## In flight

| Stream | Note |
|--------|------|
| RC9c implement | HypothesisProposed via G7 |
| G14a implement | DESIGN §16 counts vs schema v0.8 |

## Next

1. Open PRs for RC9c / G14a → dual → merge
2. RC10c / RC11c / RC12v stack
3. G14 schema kinds after G14a; G15 drop 2nd write path
4. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
