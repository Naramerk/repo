# CoScientist-next coordination STATE

**Owner:** coordinator pulse7 `bc-2cd33141-4a1a-5627-890f-b4d3fb41d286`
(https://cursor.com/agents/bc-2cd33141-4a1a-5627-890f-b4d3fb41d286).
Siblings: pulse7 `bc-2d2097c2…`, pulse8 `bc-157abd96…` (parent-spawned), pulse6
`bc-5b7a4e5e…` still RUNNING — coordinate; do not duplicate writers on same branches.
Prior pulse6 STATE #190 superseded by this tip.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:06 Europe/Moscow (UTC+3) / ~14:06 UTC |
| `main` tip | `eccaf561` (#188 RC6 after #182 G18) |
| Coordinator | https://cursor.com/agents/bc-2cd33141-4a1a-5627-890f-b4d3fb41d286 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #189 G17 CONFLICTING · #175 RC8 HOLD · #190 pulse6 (supersede) |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). `gh pr merge --merge`.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.

## Merged (this wave)

| PR | Tip → merge | Note |
|----|-------------|------|
| **#182** G18 | `765b3d6` → `bac4c62` | fold Mcp*Recorded → mcp_exchange |
| **#188** RC6 | `ca945e4` → `eccaf561` | dual k=0 (bc-257fe7ad · bc-5cead3a5) + CLEAN + CI |
| **#183** RC5 | `9694bdf` → `882756b` | prior wave |
| **#185** R16a.4 | → `33b6ad4` | prior |
| **#177** TG-Call | → `67816d7` | prior |
| **#186** STATE pulse5 | | #174 already closed |

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #189 G17 | `3251f4e` | **CONFLICTING** after G18; restack agents in flight — no second author |
| #175 RC8 | `5cb9fb7` | **HOLD** until RC6→**RC7r** (RC5+RC6 on main; need RC7r PR) |
| #190 pulse6 | `ac6b324` | superseded by pulse7 STATE — close after pulse7 PR opens |

## Superseded STATE tips (no open PR / close if opened)

| Branch | Note |
|--------|------|
| `cursor/coordination-state-pulse4-e9d9` | superseded; no PR — do not open |
| `cursor/coordination-state-pulse5b-6f3d` | superseded by #186 / pulse6–7; no PR — do not open |
| #184 pulse4-accd | already CLOSED |
| #174 | already CLOSED |

## In flight (soft-cap aware)

| Stream | Note |
|--------|------|
| Restack G17 #189 | `bc-c8b5d2d8`, `bc-a8d1c2fe`, `bc-9ea9f715` — one author wins; others idle |
| RC7r implement | `bc-ff968929`, `bc-45bb8130` after RC6 — **prefer single author**; open PR then dual |
| Fix RC6 post-cap | `bc-1f246a5f` — non-blocking follow-up from RC6 review; do not block RC7r |
| Dual G17 | tip reviews wait for **new** restacked SHA (3251f4e stale for merge) |
| Successor pulse8 | `bc-157abd96` already running |

## Next

1. Restack #189 → dual k=0 on **new** tip → merge
2. Land **RC7r** PR (after RC6) → dual → merge
3. Restack #175 RC8 after RC7r → dual → merge (HOLD until then)
4. Close #190 once pulse7 STATE PR merges
5. Soft cap: stop spawning parallel writers on G17/RC7r/RC8
6. External Mac fixtures remain blocked — document only

## External blocker

Mac `runs_final_20261023` — **document only**; not in VM. Do not invent inputs.

## Context tools (pulse7 preflight)

| Check | Result |
|-------|--------|
| codegraph | 1.6.2 |
| lean-ctx | 3.11.4 |
| codegraph status/sync | init+sync OK (316 files) |
| CodeGraph query | `assert_plan_refs` (empty on this checkout — RC6 on main) |
| LeanCTX | `docs/coordination/STATE.md` lines:1-40 |

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
