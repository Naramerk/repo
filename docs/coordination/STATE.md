# CoScientist-next coordination STATE

**Owner:** pulse actor `bc-58adae7a` (RC8 restack + coord pulse). Chain: `bc-9424e63c` · successors `bc-4bd461ef…`, `bc-e5acca3a…`. Prior: `bc-704a4fbf…`, `bc-33626709…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:45 Europe/Moscow (UTC+3) |
| `main` tip | `06770127a1d030272bb764e55f4e985c50d160bb` (#172 R16a.2+3 merged) |
| Coordinator pulse | this agent (timer `coscientist-coord-pulse`) |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD · #176 G8 · #177 TG · #178 API · #182 G18 · #183 RC5 · #174 stale |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.

## Merged (recent)

| PR | Note |
|----|------|
| **#172** | R16a.2 ReportInputRefuse + R16a.3 NIR @ `637fd73` → `0677012` |
| **#181** | STATE pulse3 |
| **#180** | STATE pulse2 |
| **#173** | RC4 @ `56acd7a` → `6432fa0` |
| **#171** | MCP-J1 |

## Open — gate drive (tips after #172)

| PR | Tip | Gate |
|----|-----|------|
| #176 | `4be2387` | MERGEABLE/UNSTABLE; dual re-review spawned (prior k=0 on `2a56281`) |
| #178 | `803ba9f` | MERGEABLE/UNSTABLE; dual re-review spawned (prior k=0 on `f8a9b99`) |
| #177 | `c247936` | MERGEABLE/UNSTABLE; dual re-review after opaque ToolError fix |
| #182 | `ff46717` | G18; MERGEABLE/UNSTABLE; dual in flight elsewhere |
| #183 | `bbfb1ca` | RC5; MERGEABLE/UNSTABLE |
| #175 | `94591c8` | CLEAN; dual k=0; **HOLD** until RC5→RC7r |
| #174 | `f98c798` | DIRTY stale — close when permissions allow |

## In flight

| Stream | Agent |
|--------|-------|
| Dual #176 tip `4be2387` | bc-47c06db0 · bc-c5e31e2a |
| Dual #178 tip `803ba9f` | bc-cc31cabf · bc-72644f3b |
| Dual #177 tip `c247936` | bc-3545f200 · bc-7ca5d664 |
| Dual G18 | bc-59dc677d · bc-89037055 |
| R16a.4 | bc-6a0ef3e9 |
| Successor pulse chain | bc-4bd461ef · bc-e5acca3a |

## Next

1. Merge #176/#178/#177 when dual k=0 on **current** tip + CLEAN CI
2. After #177 → G17
3. RC5 (#183) → RC6 → RC7r → then merge #175
4. G18 (#182) after dual k=0
5. Close #174 when API allows
6. Soft cap ~6 write streams; uncapped dual reviews

## External blocker

Mac `runs_final_20261023` — not in VM; document only; do not invent fixtures.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
