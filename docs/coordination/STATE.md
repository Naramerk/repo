# CoScientist-next coordination STATE

**Owner:** coordinator `bc-b855755c-72d8-5a5f-9204-b683ccec50e7`
(https://cursor.com/agents/bc-b855755c-72d8-5a5f-9204-b683ccec50e7).
Prior: `bc-704a4fbf…`, `bc-530d81f6…`, `bc-9424e63c…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:00 Europe/Moscow (UTC+3) |
| `main` tip | `33b6ad4d86af0f114beffe6b1f4575a6cacbc00e` (#185 R16a.4; includes #176–#178/#177/#186) |
| Coordinator | https://cursor.com/agents/bc-b855755c-72d8-5a5f-9204-b683ccec50e7 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #183 RC5 · #182 G18 · #175 RC8 **HOLD** |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
This agent is **Task subagent** — cannot subscribe CI / Task-spawn; parent must subscribe + spawn successor.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` |
| `lean-ctx --version` | `3.11.4` |
| `codegraph init/sync/status` | 0 — 316 files indexed |
| CodeGraph query | `max_attempts` → thresholds / RC17a tests |
| LeanCTX | `lifecycle.py` signatures (`create_study` / `accept_plan`) |

## Merged (recent)

| PR | Note |
|----|------|
| **#185** | R16a.4 test split → `33b6ad4` |
| **#186** | STATE pulse5 |
| **#177** | TG-Call ToolCalled → `67816d7` |
| **#178** | API data_root compose → `5575763` |
| **#176** | G8 lifecycle → `d354f74` |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 |
| **#171** | MCP-J1 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #183 RC5 | `9694bdf` | MERGEABLE; CI settling; **dual k=0** on pre-restack `4073bf1` (bc-8f6e6685 + bc-7dceb164); product blobs identical (pure rebase onto `33b6ad4`) |
| #182 G18 | `3fd05a2` | MERGEABLE; CI settling; **dual k=0** on pre-restack `d80c7d3` (bc-7c6cc612 + bc-afb0100a/bc-34dbe009); product code blobs identical |
| #175 RC8 | `7f2f5c4` | **HOLD** until RC5→RC6→RC7r |

## In flight

| Stream | Note |
|--------|------|
| RC6 | branch `cursor/rc6-selected-hyp-task-1681` @ `9648b23` (stacked on old RC5); restack after #183 |
| G17 | writers running post-TG |
| Restack agents | RC5/G18 tips already force-pushed onto main |
| Successor | parent must Task-spawn next coordinator |

## Next

1. Merge #183 when CI CLEAN on `9694bdf` (dual k=0 + pure rebase)
2. Restack RC6 → dual review → merge; then RC7r; then #175
3. Merge #182 when CI CLEAN on `3fd05a2` (independent of RC stack)
4. Parent: subscribe CI on RC5/G18/RC8 heads; spawn successor coordinator
5. Non-blocking: dedupe ToolCalled/Mcp rows in `docs/plan/graph.md` on main

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
