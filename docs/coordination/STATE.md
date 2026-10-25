# CoScientist-next coordination STATE

**Owner:** successor coordinator `bc-ef7929e0` (pulse5). Prior pulse actor `bc-58adae7a`; chain `bc-9424e63c` · `bc-4bd461ef` · `bc-e5acca3a` · `bc-33626709`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:52 Europe/Moscow (UTC+3) |
| `main` tip | `d354f742ebbc382f5ea730887ec6cc31b4260ca7` (#176 G8 merged after #172) |
| Coordinator | https://cursor.com/agents/bc-ef7929e0-3227-551b-ade5-a033618b6f3d |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD · #177 TG · #178 API · #182 G18 · #183 RC5 · #184 pulse4-accd · #185 R16a.4 · #174 stale |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.

Soft cap ~6 write streams; uncapped dual tip reviews; one author per write branch.

## Merged (recent)

| PR | Note |
|----|------|
| **#176** | G8 lifecycle → G7 writer @ `4be2387` → `d354f74` (dual k=0: bc-fcb42bb3 / bc-ec5f62cf + handoffs) |
| **#172** | R16a.2+3 @ `0677012` |
| **#181** | STATE pulse3 |
| **#180** | STATE pulse2 |
| **#173** | RC4 |
| **#171** | MCP-J1 |

## Open — gate drive (tips after #176)

| PR | Tip | Gate |
|----|-----|------|
| #178 | `a1ab4af` | MERGEABLE/UNSTABLE (restack after #176); prior dual k=0 on `803ba9f` — **re-dual on current tip** + CLEAN CI |
| #177 | `25e3535` | MERGEABLE/UNSTABLE; prior `c247936` dual **k=1** (heuristics); tip advanced — dual on **current** tip |
| #182 | `d80c7d3` | G18; MERGEABLE/UNSTABLE; dual in flight / tip moved |
| #183 | `4073bf1` | RC5; MERGEABLE/UNSTABLE |
| #185 | `936da14` | R16a.4; MERGEABLE/UNSTABLE |
| #175 | `9e8995d` | MERGEABLE/UNSTABLE; **HOLD** until RC5→RC6→RC7r |
| #184 | `09b1f71` | CLEAN (STATE pulse4-accd); dual not banked on tip |
| #174 | `f98c798` | DIRTY stale — close when API allows (this agent: 403) |

## In flight (observed)

| Stream | Agent / note |
|--------|----------------|
| Restack after #176 | bc-cf14b94b (API/TG tips already advanced) |
| Dual #178 stale tip `803ba9f` | bc-0fc9155a / bc-d2105a75 k=0 — **superseded tip**; need dual on `a1ab4af` |
| Dual #177 tips | prior k=1 on `c247936`; newer reviews on `027dfac` in flight |
| Dual G18 / RC5 / R16a.4 / RC8 re-confirm | multiple RUNNING reviewers (parent-spawned) |
| RC6 implement | bc-3817b13e |
| G18 heuristics fix | bc-e8921c41 |
| TG heuristics fix | bc-86e74634 |
| This coordinator | no Task / ManagePullRequest — parent must spawn duals + open PRs |

## Next

1. Dual re-review **current** tips `#178 a1ab4af` / `#177 25e3535` (and G18/RC5 after CI CLEAN) → merge when dual k=0 + CLEAN
2. After #177 → unlock G17
3. RC5 (#183) → RC6 → RC7r → then merge #175 (HOLD)
4. G18 (#182) after dual k=0 on current tip
5. Close #174 when permissions allow; supersede #184 with this pulse5 when PR opened
6. Soft cap ~6 write streams

## Tooling note (this pulse)

- `gh` integration: **cannot** `pr close` / `user` (403); merge may still work when gate passes
- No ManagePullRequest / Task in this cloud subagent — parent opens pulse5 PR + spawns reviewers
- Preflight: codegraph `1.6.2`, lean-ctx `3.11.4`, `codegraph query create_app`, lean-ctx `app.py` signatures

## External blocker

Mac `runs_final_20261023` — not in VM; document only; do not invent fixtures.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
