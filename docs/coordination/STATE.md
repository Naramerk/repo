# CoScientist-next coordination STATE

**Owner:** coordinator `bc-1bbeb979-a89c-5b9f-8824-253d88680e82`
(https://cursor.com/agents/bc-1bbeb979-a89c-5b9f-8824-253d88680e82).
Prior pulse9 `bc-036c2b4b…`; pulse8 `bc-157abd96…`; pulse5 `bc-704a4fbf…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:14 Europe/Moscow (UTC+3) |
| `main` tip | `a881d22` (#191 RC6 post-cap; includes #192 RC7r @ `ead63eb`) |
| Coordinator | https://cursor.com/agents/bc-1bbeb979-a89c-5b9f-8824-253d88680e82 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 · #189 G17 |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.
Writers currently over soft cap — **do not spawn more product writers**.

## Merged (recent)

| PR | Note |
|----|------|
| **#191** | RC6 post-cap selected drop → `a881d22` |
| **#192** | RC7r HITL reject → `ead63eb` (#194 closed as dup) |
| **#190** | STATE pulse6 (stale; superseded by this pulse) |
| **#188** | RC6 selected hyp ≥1 task → `eccaf56` |
| **#182** | G18 mcp_exchange → `bac4c62` |
| **#183** | RC5 hypotheses artifact → `882756b` |
| **#185** | R16a.4 test split → `33b6ad4` |
| **#177** | TG-Call ToolCalled → `67816d7` |
| **#186** | STATE pulse5 |
| **#178** | API data_root compose |
| **#176** | G8 lifecycle |
| **#172** | R16a.2+R16a.3 |
| **#173** | RC4 |
| **#171** | MCP-J1 |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #175 RC8 | `8a0b6f5` | MERGEABLE/UNSTABLE; RC stack clear (#192 merged); dual k=0 tip-match + CLEAN → **merge next** |
| #189 G17 | `f70cd2a` | MERGEABLE/UNSTABLE; restacked onto `a881d22` (CHANGELOG w/ RC7r); dual on **this** tip + CLEAN → merge |

## In flight

| Stream | Note |
|--------|------|
| Dual RC8 `8a0b6f5` | tip re-confirm after post-RC7r restack |
| Dual G17 `f70cd2a` | prior duals on `6744ec6`/`7fa4677` superseded — retarget |
| G17 restack author | tip moved `6744ec6`→`f70cd2a`; no second writer |
| Successor pulse11 | parent must spawn before this agent stops |

## Next

1. Merge #175 RC8 when dual k=0 + CLEAN + tip-match `8a0b6f5`
2. Merge #189 G17 when dual k=0 + CLEAN + tip-match `f70cd2a`
3. Parent: subscribe CI on both heads; spawn dual tip-match reviewers; spawn pulse11
4. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
