# CoScientist-next coordination STATE

**Owner:** coordinator `bc-9424e63c-b220-5bfb-b57c-5b2e1148accd`
(https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd).
Successors in flight: `bc-5b7a4e5e…`, `bc-e5acca3a…`. Prior pulse5 owner `bc-704a4fbf…`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:01 Europe/Moscow (UTC+3) |
| `main` tip | `882756b` (#183 RC5) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #175 RC8 HOLD · #182 G18 · #188 RC6 · #189 G17 |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`. No per-PR ask.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.

## Merged (recent)

| PR | Note |
|----|------|
| **#183** | RC5 hypotheses artifact @ `9694bdf` → `882756b` |
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
| #188 RC6 | `ca945e4` | MERGEABLE/UNSTABLE; dual on tip in flight; merge after dual k=0 + CLEAN |
| #182 G18 | `765b3d6` | MERGEABLE/UNSTABLE; dual on tip in flight |
| #189 G17 | `3251f4e` | opened after TG; needs dual + CLEAN (prefer over older `…-fold-1681` branch) |
| #175 RC8 | `7f2f5c4` | **HOLD** until RC6→RC7r; CONFLICTING restack in flight |

## In flight

| Stream | Note |
|--------|------|
| Dual RC6 `ca945e4` | tip reviews after main restack |
| Dual G18 `765b3d6` | post-RC5 restack tip |
| RC7r implement | after RC6 tip |
| RC8 restack | HOLD merge |
| Successor pulse6 | `bc-5b7a4e5e` |

## Next

1. Merge #188 RC6 when dual k=0 + CLEAN → then RC7r PR → dual → merge
2. Merge #182 G18 when dual k=0 + CLEAN
3. Dual + merge #189 G17
4. Restack #175 after RC6+RC7r; then dual + merge
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
