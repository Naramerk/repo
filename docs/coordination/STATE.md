# CoScientist-next coordination STATE

**Owner:** coordinator `bc-e5acca3a-9d5e-5bae-a74d-a332f5875b33` (active). Ignore parallel pulse owners on #184/#186 for product merges; do not dual-author this file.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:56 Europe/Moscow (UTC+3) |
| `main` tip | `67816d7` (#177 TG + #178 API + #176 G8 + #172 R16a + #173 RC4 + #171) |
| Coordinator | https://cursor.com/agents/bc-e5acca3a-9d5e-5bae-a74d-a332f5875b33 |
| Claim branch | `cursor/coordination-state-claim-e5ac-1681` |

## Merge policy

Standing grant: `gh pr merge --merge` when adequacy gate passes (`AGENTS.md`).

## Merged (this wave)

| PR | Note |
|----|------|
| **#177** | TG-Call ToolCalled → unlocks G17 |
| **#178** | API data_root StudyService compose |
| **#176** | G8 lifecycle via G7 |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 HYP-n |
| #181/#180/#179 | STATE pulses |
| **#171** | MCP-J1 |

## Open — gate drive

| PR | Stream | Tip | Gate |
|----|--------|-----|------|
| #182 | G18 | `d80c7d3` | CLEAN+CI; dual in flight → MERGE when dual k=0 |
| #183 | RC5 | `4073bf1` | CLEAN+CI; dual in flight → then RC6 (`bc-3817b13e`) |
| #185 | R16a.4 | `936da14` | CLEAN; one review **not ok (k=1)** — author fix before merge |
| #175 | RC8 | `327b33b` | **HOLD** until RC5→RC7r |
| #186/#184 | STATE pulse | — | supersede with this claim; close #174 |

## Next

1. Merge #182/#183 when dual k=0.
2. Spawn G17 after #177 (done on main).
3. Fix R16a.4 k=1 → re-review → merge.
4. RC5→RC6→RC7r → #175.
5. Parent: ManagePullRequest for claim; close stale STATE PRs.

## External blocker

Mac `runs_final_20261023` — not in VM; document only.

## Subagent constraint

No Task/ManagePullRequest/CI subscribe in this run.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
