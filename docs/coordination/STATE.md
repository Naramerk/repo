# CoScientist-next coordination STATE

**Owner:** coordinator `bc-530d81f6-8136-5cb5-b793-25d24db0e761` (successor of `bc-704a4fbf…`). Sibling: `bc-ef7929e0…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:52 Europe/Moscow (UTC+3) |
| `main` tip | `5575763` (#178 API merged after #176 G8) |
| Coordinator | https://cursor.com/agents/bc-530d81f6-8136-5cb5-b793-25d24db0e761 |
| Repo | https://github.com/Naramerk/repo |
| Open | #175 RC8 HOLD · #177 TG · #182 G18 · #183 RC5 · #184 STATE stale · #185 R16a.4 · #174 stale |

## Merge policy

Standing grant: `gh pr merge --merge` when dual k=0 + MERGEABLE/CLEAN + required CI green + tip match/pure rebase + stack order.

## Merged (this session)

| PR | Note |
|----|------|
| **#178** | API data_root compose @ `a1ab4af` → `5575763` (pure restack of dual-k=0 `f8a9b99`) |
| **#176** | G8 @ `4be2387` → `d354f74` (pure restack of dual-k=0 `2a56281`) |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 |
| **#171** | MCP-J1 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #185 | R16a.4 | MERGEABLE; CI settling; dual-review needed |
| #177 | TG | DIRTY after #176/#178 — restack + dual |
| #182 | G18 | heuristics fix in flight |
| #183 | RC5 | dual/CI in flight; stack before RC8 |
| #175 | RC8 | **HOLD** until RC5→RC6→RC7r |
| #184 | STATE pulse4 | CLEAN but stale vs this tip — supersede/close |
| #174 | pulse 704a | DIRTY stale — close |

## Limits

This run is a **Task subagent**: no CI subscriptions, no Task spawn, no `gh pr create`. Parent must subscribe + spawn successor. Soft cap ~6 writers.

## Next

1. Dual-review + merge #185 when CLEAN
2. Restack #177 → dual → merge → G17
3. G18 green → dual → merge
4. RC5 → RC6 → RC7r → #175
5. Close #174/#184 stale STATE PRs

## External blocker

Mac `runs_final_20261023` — document only.
