# CoScientist-next coordination STATE

**Owner:** coordinator `bc-530d81f6-8136-5cb5-b793-25d24db0e761` (successor of `bc-704a4fbf…`). Prior chain: `bc-9424e63c…` → `bc-e5acca3a…` / `bc-5e2c1d74…` / `bc-704a4fbf…`. Sibling/successor spawn: `bc-ef7929e0…` (full mandate).

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:48 Europe/Moscow (UTC+3) |
| `main` tip | `d354f742ebbc382f5ea730887ec6cc31b4260ca7` (#176 G8 merged) |
| Coordinator | https://cursor.com/agents/bc-530d81f6-8136-5cb5-b793-25d24db0e761 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD · #177 TG · #178 API (restack after #176) · #182 G18 · #183 RC5 · #174 stale |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.

## Merged (recent)

| PR | Note |
|----|------|
| **#176** | G8 lifecycle via G7 @ `4be2387` → `d354f74` (pure restack of dual-k=0 `2a56281`; blob-identical) |
| **#172** | R16a.2+R16a.3 |
| **#181** | STATE pulse3 |
| **#173** | RC4 |
| **#180** | STATE pulse2 |
| **#171** | MCP-J1 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #178 | `803ba9f` | Was dual-k=0 on `f8a9b99` (blob-identical API); **DIRTY** after #176 — restack → dual re-confirm → merge |
| #177 | `027dfac` | TG ToolCalled + opaque fix; DIRTY after #176; heuristics/author in flight |
| #182 | `ff46717` | G18; CI heuristics fail; fix in flight (bc-e8921c41) |
| #183 | `bbfb1ca` | RC5; CI fail / dual review in flight |
| #175 | `94591c8` | CLEAN; dual k=0; **HOLD** until RC5→RC6→RC7r |
| #174 | `f98c798` | DIRTY stale — close when possible |

## In flight

| Stream | Agent / note |
|--------|----------------|
| API restack after #176 | coordinator / sibling |
| TG heuristics + dual | bc-86e74634 · reviews on `027dfac` |
| G18 heuristics | bc-e8921c41 |
| RC5 dual | bc-f5f9071b · bc-e64e128b |
| RC6 | bc-3817b13e |
| R16a.4 | bc-6a0ef3e9 |
| Successor | bc-ef7929e0 |

## Limits (this run)

Task **subagent**: no `subscribe_*`, no Task spawn. Parent must CI-subscribe open heads and spawn next successor. Dual reviews uncapped; soft cap ~6 write streams.

## Next

1. Restack #178 → CI CLEAN → dual re-confirm (or pure-rebase of `f8a9b99` k=0) → merge
2. Restack #177 after TG fix green → dual → merge → G17
3. G18 heuristics green → dual → merge
4. RC5 → RC6 → RC7r → then #175
5. Close #174; R16a.4 after reporting stack

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
