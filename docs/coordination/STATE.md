# CoScientist-next coordination STATE

**Owner:** coordinator `bc-1bbeb979-a89c-5b9f-8824-253d88680e82`
(https://cursor.com/agents/bc-1bbeb979-a89c-5b9f-8824-253d88680e82).
Successors: pulse11 `bc-f305b1d7…`, pulse12 `bc-3705a06c…`. Prior pulse5 `bc-704a4fbf…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:18 Europe/Moscow (UTC+3) |
| `main` tip | `e1b14b0` (#189 G17; includes #175 RC8 @ `94ba209`) |
| Coordinator | https://cursor.com/agents/bc-1bbeb979-a89c-5b9f-8824-253d88680e82 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | *(none)* — queue cleared |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.

## Merged (recent)

| PR | Note |
|----|------|
| **#189** | G17 ToolCalled→tool_call @ `f70cd2a` → `e1b14b0` |
| **#175** | RC8 hypothesis_verdict matrix @ `8a0b6f5` → `94ba209` |
| **#191** | RC6 post-cap selected drop → `a881d22` |
| **#192** | RC7r HITL reject → `ead63eb` (#194 closed as dup) |
| **#190** | STATE pulse6 (stale) |
| **#188** | RC6 selected hyp ≥1 task → `eccaf56` |
| **#182** | G18 mcp_exchange → `bac4c62` |
| **#183** | RC5 hypotheses artifact → `882756b` |
| **#185** | R16a.4 test split → `33b6ad4` |
| **#177** | TG-Call ToolCalled → `67816d7` |
| **#186** | STATE pulse5 |
| **#178** | API data_root compose |
| **#176** | G8 lifecycle |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 |
| **#171** | MCP-J1 |

## Open — gate drive

| Stream | Tip / issue | Gate |
|--------|-------------|------|
| **RC9c** | #105 HypothesisProposed via G7 | implementer `bc-390a60f6` in flight → dual → merge |
| **G14a** | DESIGN §16 counts | implementer `bc-6e563f4c` in flight → dual → merge |
| STATE pulse10 | `cursor/coordination-state-pulse10-0e82` | parent: open PR + merge |
| STATE pulse11 | `cursor/coordination-state-pulse11-a411` | parent: open PR (may supersede) |

## In flight

| Stream | Note |
|--------|------|
| RC9c implement | next research-cycle after RC8 |
| G14a implement | parallel graph docs (independent) |
| Successors pulse11/12 | continuous; spawn pulse13 before stop |
| Soft cap | 2 product writers — OK; no more until PRs open |

## Next

1. Dual + merge RC9c when tip CLEAN (then RC10c / RC11c stack)
2. Dual + merge G14a when tip CLEAN
3. Parent: open/merge STATE PR(s); subscribe CI on new heads; spawn pulse13
4. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
