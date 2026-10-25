# CoScientist-next coordination STATE

**Owner:** coordinator pulse17 `bc-2cd33141-4a1a-5627-890f-b4d3fb41d286`
(https://cursor.com/agents/bc-2cd33141-4a1a-5627-890f-b4d3fb41d286).
Peers: pulse14–16 (`bc-fca82455…`, `bc-c2678381…`, `bc-b30590aa…`); successor
pulse18 `bc-5271e78a…` spawned. Soft-cap: do not add more RC9c writers.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:27 Europe/Moscow (UTC+3) / ~14:27 UTC |
| `main` tip | `c5cca83` (#198 G14a on #197 pulse8) |
| Coordinator | https://cursor.com/agents/bc-2cd33141-4a1a-5627-890f-b4d3fb41d286 |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | **#200** RC9c tip `b998bf1` |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack.
`gh pr merge --merge`. Soft cap ≤6 write streams. One author per write branch.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` |
| `lean-ctx --version` | `3.11.4` |
| `codegraph init/sync` | OK (334 files) |
| CodeGraph | `query "HypothesisProposed"` (empty on main — lives on #200) |
| LeanCTX | `STATE.md` lines:1-25 |

## Merged (post-pulse7 wave)

| PR | Note |
|----|------|
| **#198** | G14a DESIGN/RM counts → schema v0.8 / 15/19 → `c5cca83` |
| **#197** | STATE pulse8 |
| **#195** | STATE pulse7 |
| **#189** | G17 ToolCalled → tool_call |
| **#175** | RC8 hypothesis_verdict |
| **#192** | RC7r HITL reject |
| **#191** | RC6 post-cap selected refuse |
| **#190** | STATE pulse6 (closed/superseded) |
| **#188** / **#182** | RC6 / G18 |

## Stack status

RC4→RC8 + G8/G17/G18 + **G14a** on main. Schema **v0.8 / 15/19**.
Next: **RC9c** (#200 / #105) → RC10c → RC11c → RC12v → G12.

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| #200 RC9c | `b998bf1` | MERGEABLE/UNSTABLE; CI pending; duals `bc-73446545` / `bc-5293befb` on **this** tip (prior 792c60f/0b4eee0 superseded) |
| #201 STATE | `ef6e034` | post-G14a docs; may supersede or merge alongside pulse17 |

## Superseded

| Item | Note |
|------|------|
| pulse4-e9d9 / pulse5b | do not open |
| #174 / #199 | closed |
| Duals on `792c60f` | tip moved — re-review `b998bf1` only |

## In flight

| Stream | Note |
|--------|------|
| RC9c #200 | prefer sole author on `cursor/rc9c-hypothesis-proposed-1681`; restack agents idle once tip stable |
| Dual RC9c `b998bf1` | bc-73446545 · bc-5293befb |
| Successor pulse18 | bc-5271e78a |
| Parent | CI/PR subscribe on #200 (Task subagents cannot subscribe) |

## Next

1. Await dual k=0 + CI green on #200 tip `b998bf1` → `gh pr merge 200 --merge`
2. Open/merge this pulse17 STATE; close superseded STATE PRs (#201 if stale)
3. Queue RC10c / RC11c / RC12v → G12
4. Soft-cap writers; keep successor chain
5. Mac fixtures — document only

## External blocker

Mac `runs_final_20261023` — **document only**; not in VM.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
