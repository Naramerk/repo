# CoScientist-next coordination STATE

**Owner:** successor coordinator `bc-ef7929e0` (pulse5). Next successor spawned: `bc-b855755c`. Prior pulse actor `bc-58adae7a`; chain `bc-9424e63c` · `bc-4bd461ef` · `bc-e5acca3a` · `bc-33626709`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:55 Europe/Moscow (UTC+3) |
| `main` tip | `5575763a3d755d659fb4ff72ce8198ace9b2f5a7` (#178 API after #176 G8 / #172) |
| Coordinator | https://cursor.com/agents/bc-ef7929e0-3227-551b-ade5-a033618b6f3d |
| Successor | https://cursor.com/agents/bc-b855755c-72d8-5a5f-9204-b683ccec50e7 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD/DIRTY · #177 TG · #182 G18 · #183 RC5 · #185 R16a.4 · #186 pulse5-alt |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.

Soft cap ~6 write streams; uncapped dual tip reviews; one author per write branch.

## Merged (recent)

| PR | Note |
|----|------|
| **#178** | API data_root compose @ `a1ab4af` → `5575763` (dual k=0 on tip / pure rebase from `803ba9f`) |
| **#176** | G8 lifecycle → G7 writer @ `4be2387` → `d354f74` (dual k=0) |
| **#172** | R16a.2+3 @ `0677012` |
| **#181** | STATE pulse3 |
| **#180** | STATE pulse2 |
| **#173** | RC4 |
| **#171** | MCP-J1 |
| **#174** | stale pulse — **closed** |

## Open — gate drive (tips after #178)

| PR | Tip | Gate |
|----|-----|------|
| #177 | `25e3535` | MERGEABLE/CLEAN CI green; dual in flight (bc-3a1c4c0c · bc-f6ad430a) — merge when dual k=0; tip may need restack if GitHub flips DIRTY |
| #182 | `d80c7d3` | G18; dual in flight |
| #183 | `4073bf1` | RC5; dual in flight; restack after #178 likely |
| #185 | `936da14` | R16a.4; dual/re-confirm in flight |
| #175 | `9e8995d` | DIRTY after #178; **HOLD** until RC5→RC6→RC7r then restack+dual |
| #186 | `291f735` | alt STATE pulse5 (bc-704a4fbf); dual in flight — prefer consolidate with this branch |

## In flight

| Stream | Agent / note |
|--------|----------------|
| Dual #177 `25e3535` | bc-3a1c4c0c · bc-f6ad430a |
| Dual G18 `d80c7d3` | bc-7c6cc612 · bc-34dbe009 (+ earlier pair) |
| Dual RC5 `4073bf1` | bc-8f6e6685 · bc-7dceb164 |
| Dual/re-confirm R16a.4 | bc-c9a7b73e · bc-f75353db · … |
| Dual STATE #186 | bc-91e143d4 · bc-6edff78f |
| RC6 implement | bc-3817b13e |
| Successor pulse5 | **bc-b855755c** (parent-spawned) |
| This agent | Task subagent — no Task/subscribe/ManagePullRequest |

## Next

1. Merge #177 when dual k=0 on current tip + CLEAN → unlock G17
2. Restack dependents after #178 (#183/#175/#182 as needed)
3. RC5 → RC6 → RC7r → then #175
4. G18 after dual k=0
5. Soft cap ~6 write streams
6. Parent: CI subscriptions + pulse timer (this subagent cannot arm)

## External blocker

Mac `runs_final_20261023` — not in VM; document only; do not invent fixtures.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
