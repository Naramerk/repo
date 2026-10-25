# CoScientist-next coordination STATE

**Owner:** coordinator `bc-9424e63c-b220-5bfb-b57c-5b2e1148accd`. Successors: `bc-704a4fbf…`, `bc-5e2c1d74…`, `bc-e5acca3a…`. Prior: `bc-33626709…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:36 Europe/Moscow (UTC+3) |
| `main` tip | `6432fa05483bad3ece2d3b40674a03b87e5fe11d` (#173 RC4 + #179 STATE + #171 MCP-J1) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #172/#174–#178/#180 — restack after RC4 in flight |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.

## Merged (recent)

| PR | Note |
|----|------|
| **#173** | RC4 #100 @ `56acd7a` — dual k=0 ([Review #173 tip 56acd7a](bc-4dd76b84-ad79-5f67-9191-0c9fb1dde481) · [2nd review #173 56acd7a](bc-597aa938-38c9-5bbd-a358-e38db69bca9c)) → `6432fa0` |
| **#179** | STATE pulse → `bd92707` |
| **#171** | MCP-J1 → `f3a0030` |
| #168/#167 | RC17a / R16a.1a |

## Open — gate drive

| PR | Stream | Tip | Gate |
|----|--------|-----|------|
| #172 | R16a.2+#121 | was `5291fa1` dual k=0 | CONFLICTING after #173 — restack [Restack #172 after RC4](bc-48603139-f437-5b64-9c24-ccd34c9d5dac) then re-review/merge |
| #176 | G8 #84 | was `51a2861` | CONFLICTING (CHANGELOG + lifecycle) — restack [Restack G8 API TG RC8 after RC4](bc-e2222b76-491d-5ea5-8a28-635f432c353f) |
| #178 | API #153 | was `a511351` | CONFLICTING CHANGELOG — same restack |
| #177 | TG #88 | was `f633c61` | CONFLICTING CHANGELOG — same restack |
| #175 | RC8 #104 | was `d91befe` | restack; **hold merge** until RC5–RC7r |
| #180 | STATE pulse2 | this branch | docs |
| #174 | stale STATE | `f98c798` | close after #180 |

## In flight

| Stream | Agent |
|--------|-------|
| Restack #172 | bc-48603139 |
| Restack G8/API/TG/RC8 | bc-e2222b76 |
| G18 | bc-0524f861 |
| RC5 | bc-10ec685d |
| Dual re-reviews (pre-RC4 tips may be stale) | see cloud list |

## Next after merges

1. After #172 restack+dual k=0 → merge → R16a.4 #122
2. RC5 #101 (in flight) → RC6…
3. After #177 → G17
4. G18 after MCP-J1 (in flight)
5. After #176 → G7 consumers / RC9c when unblocked

## External blocker

Mac `runs_final_20261023` — document only; do not invent.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
