# CoScientist-next coordination STATE

**Owner:** coordinator `bc-704a4fbf-db89-5945-a0f2-4069baad8e76`
(https://cursor.com/agents/bc-704a4fbf-db89-5945-a0f2-4069baad8e76).
Successors in flight: `bc-ef7929e0…`. Prior: `bc-9424e63c…`, `bc-e5acca3a…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~16:55 Europe/Moscow (UTC+3) |
| `main` tip | `d354f742ebbc382f5ea730887ec6cc31b4260ca7` (#176 G8) |
| Coordinator | https://cursor.com/agents/bc-704a4fbf-db89-5945-a0f2-4069baad8e76 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD · #177 TG · #178 API · #182 G18 · #183 RC5 · #184 STATE · #185 R16a.4 |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
This agent is AS_SUBAGENT — CI subscribe must be done by parent; timer pulse kept.

## Merged (recent)

| PR | Note |
|----|------|
| **#176** | G8 lifecycle @ tip → `d354f74` |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 |
| **#171** | MCP-J1 |
| **#174** | closed stale DIRTY (superseded by #184/#pulse5) |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #178 API | `a1ab4af` | MERGEABLE; CI settling; dual re-confirm in flight |
| #177 TG | `25e3535` | opaque restore on write-path; dual review tip in flight — merge only k=0 |
| #183 RC5 | `4073bf1` | CI + dual in flight; merge before RC6/RC8 |
| #185 R16a.4 | `936da14` | tip moved from `4224b2b`; dual re-review spawned |
| #182 G18 | `d80c7d3` | CI queued; heuristics fix may advance tip |
| #184 STATE | `09b1f71` | CLEAN docs; dual review spawned |
| #175 RC8 | `9e8995d` | **HOLD** until RC5→RC6→RC7r |

## In flight

| Stream | Note |
|--------|------|
| Dual API `a1ab4af` | multiple re-confirm agents |
| Dual TG `25e3535` | after opaque restore |
| Dual RC5 `4073bf1` | |
| Dual R16a.4 `936da14` | |
| Dual #184 | |
| G18 fix | bc-e8921c41 |
| RC6 (after RC5) | bc-3817b13e |
| Successor | bc-ef7929e0 |

## Next

1. Merge #178/#183/#185/#184 when dual k=0 + CLEAN + tip match
2. TG #177 only after dual k=0 on live tip (opaque+heuristics)
3. RC5 → RC6 → RC7r → then #175
4. G18 after dual; then G17 after TG
5. Parent: subscribe CI on #177/#178/#182/#183/#185

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
