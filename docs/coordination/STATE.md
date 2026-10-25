# CoScientist-next coordination STATE

**Owner:** coordinator `bc-4bd461ef-6226-520c-b76b-b58a0f247cf2` (successor of `bc-9424e63c…`).

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 17:10 Europe/Moscow (UTC+3) |
| `main` tip | `d01e8df` (#188 RC6 + #182 G18 + #183 RC5 + #177 + #178 + #176 + #185 + #172) |
| Coordinator | https://cursor.com/agents/bc-4bd461ef-6226-520c-b76b-b58a0f247cf2 |
| Repo | https://github.com/Naramerk/repo |

## Merge policy

Standing grant: `gh pr merge --merge` when adequacy gate passes. Soft cap ~6 writes; one author/branch; Mac fixtures external only.

## Merged (mandate)

| PR | Note |
|----|------|
| **#188** | RC6 |
| **#182** | G18 |
| **#183** | RC5 |
| **#177** | TG-Call |
| **#178** | API data_root |
| **#176** | G8 |
| **#185** | R16a.4 |
| **#172** | R16a.2+R16a.3 |

## Open

| PR | Tip | Gate |
|----|-----|------|
| #189 G17 | `0e43fe9` | restack atop G18 (15 nodes); CI + dual → merge |
| #175 RC8 | CLEAN | **HOLD** until RC7r |
| #190 STATE pulse6 | stale | close vs this claim |

## Author transfer

| Branch | Note |
|--------|------|
| `cursor/graph-g17-toolcalled-1681` | bc-4bd461ef rebuilt tip preserving G18 |

## Next

1. Dual-review #189 `0e43fe9` → merge
2. RC7r → merge → #175
3. Parent: open claim PR; spawn successor if parking

## External blocker

Mac `runs_final_20261023` — document only.
