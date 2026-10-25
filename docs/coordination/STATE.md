# CoScientist-next coordination STATE

**Owner:** successor coordinator `bc-ef7929e0` (pulse5b). Next successor: `bc-b855755c`. Prior: `bc-58adae7a` · `bc-9424e63c` · `bc-33626709`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:56 Europe/Moscow (UTC+3) |
| `main` tip | `67816d774d7d2614e60bd5fdda26363a5f3ce64b` (#177 TG after #178/#176/#172) |
| Coordinator | https://cursor.com/agents/bc-ef7929e0-3227-551b-ade5-a033618b6f3d |
| Successor | https://cursor.com/agents/bc-b855755c-72d8-5a5f-9204-b683ccec50e7 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD · #182 G18 CLEAN · #183 RC5 CLEAN · #185 R16a.4 CLEAN · #186 pulse5-alt |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). Soft cap ~6 write streams.

## Merged (recent)

| PR | Note |
|----|------|
| **#177** | TG ToolCalled @ `25e3535` → `67816d7` (dual k=0: bc-3a1c4c0c · bc-f6ad430a) |
| **#178** | API data_root @ `a1ab4af` → `5575763` |
| **#176** | G8 @ `4be2387` → `d354f74` |
| **#172** | R16a.2+3 |
| **#174** | stale — closed |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #183 | `4073bf1` | CLEAN — merge when dual k=0; then RC6→RC7r |
| #182 | `d80c7d3` | CLEAN — merge when dual k=0 |
| #185 | `936da14` | CLEAN — merge when dual k=0 |
| #175 | `327b33b` | UNSTABLE; **HOLD** until RC5→RC7r |
| #186 | `291f735` | alt STATE pulse — consolidate |

## Next

1. Unlock **G17** after #177 (spawn implementer if free write slot)
2. Merge #183/#182/#185 when dual k=0 on those tips
3. RC5→RC6→RC7r→#175
4. Parent: CI subscriptions + pulse timer (Task subagent cannot)
5. Soft cap ~6 write streams

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
