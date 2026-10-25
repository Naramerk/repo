# CoScientist-next coordination STATE

**Owner:** coordinator `bc-f305b1d7-5075-5ff4-a36b-810ffc5fa411`
(https://cursor.com/agents/bc-f305b1d7-5075-5ff4-a36b-810ffc5fa411).
Prior #197 pulse8. Concurrent pulse12–15 RUNNING.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~14:25 UTC |
| `main` tip | `c5cca83` (#198 G14a) |
| Coordinator | https://cursor.com/agents/bc-f305b1d7-5075-5ff4-a36b-810ffc5fa411 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #200 RC9c |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Soft cap ≤6 write streams; **one author per write branch**. Uncapped read-only duals.

## Merged (recent wave)

| PR | Note |
|----|------|
| **#198** | G14a DESIGN/RM pin + fail-closed count test → `c5cca83` |
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 |
| **#189** | G17 |
| **#175** | RC8 |
| **#191** | RC6 post-cap |
| **#192** | RC7r |
| **#199** | G14a duplicate — **closed** (superseded by #198) |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #200 RC9c | `d86b1e1` | Restacked onto #198 main; MERGEABLE/UNSTABLE; **need dual k=0 on this tip** (prior duals on `792c60f` do not tip-match) |

## Next

1. Dual tip-match `d86b1e1` + CLEAN → `gh pr merge 200 --merge`
2. Parent open+merge STATE pulse11 `cursor/coordination-state-pulse11-a411`
3. RC10c after RC9c
4. Soft cap — dual-only until #200 lands
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
