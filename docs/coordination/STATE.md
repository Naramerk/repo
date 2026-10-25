# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-2d2097c2-3aae-58a6-a56e-6cc02cf70634`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:16 Europe/Moscow (UTC+3) |
| `main` tip | `e1b14b0` (#189 G17 + #175 RC8 + #192 RC7r + #191 + #188 + #182 + #183) |
| Coordinator | https://cursor.com/agents/bc-2d2097c2-3aae-58a6-a56e-6cc02cf70634 |
| Successors | pulse8–11 (`bc-157abd96` … `bc-f305b1d7`) |
| Repo | https://github.com/Naramerk/repo |
| Open write tips | none on RC6–RC8 / G17–G18 wave — **cleared** |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). Soft cap ~6 writers; uncapped dual reviews; one author per branch.

## Merged (this coordinator wave)

| PR | Note |
|----|------|
| **#189** | G17 ToolCalled → tool_call → `e1b14b0` |
| **#175** | RC8 hypothesis_verdict matrix → `94ba209` |
| **#192** | RC7r HITL reject → no PlanAccepted |
| **#191** | RC6 post-cap selected re-assert |
| **#190** | STATE pulse6 |
| **#188** | RC6 selected hyp ≥1 task |
| **#182** | G18 mcp_exchange |
| **#183** | RC5 hypotheses artifact |

## Active backlog (unblocked)

| Stream | Issue | Note |
|--------|-------|------|
| M4 RC | #105 RC9c | HypothesisProposed via G7 — next after RC8 |
| M4 RC | #106 / #107 | RC10c / RC11c after RC9c |
| M4 RC | #96 RC12v | HypothesisVerdictRecorded (unlocks G12) |
| M4 | #99 RC17a | confirm on main / close if done |
| M5 | #119 R16a.1a | code-span locking test |
| Graph | G14a | DESIGN §16 counts vs schema |

## Next

1. Assign **one** author RC9c (#105) — soft-cap reset after wave clear
2. Dual → merge RC9c → RC10c → RC11c
3. Parent: open+merge STATE pulse7 tip `cursor/coordination-state-pulse7-0634`
4. Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
