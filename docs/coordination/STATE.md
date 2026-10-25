# CoScientist-next coordination STATE

**Owner:** coordinator `bc-d50ef046-13b0-5f5d-b5c1-7be13e8a767a`
(https://cursor.com/agents/bc-d50ef046-13b0-5f5d-b5c1-7be13e8a767a).
Peers: pulse13 twin `bc-2b1a7c00…`; pulse14 `bc-fca82455…`; prior pulse8 `bc-157abd96…`.
Task subagent: no CI subscribe / Task-spawn / ManagePullRequest; **can** `gh pr merge`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:22 Europe/Moscow (UTC+3) |
| `main` tip | `e35ea2f` (#197 STATE pulse8 on #195 / RC8+G17 wave) |
| Coordinator | https://cursor.com/agents/bc-d50ef046-13b0-5f5d-b5c1-7be13e8a767a |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | **#198** G14a (prefer); **#199** G14a duplicate — close |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack.
`gh pr merge --merge`. Soft cap ≤6 write streams; one author per write branch.
Do **not** spawn duplicate RC9c/G14a authors (already in flight).

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK |
| CodeGraph | `query "RC9 hypothesis"` |
| LeanCTX | `docs/plan/research-cycle.md` lines:30-40 |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 → `4bc5c2c` |
| **#189** | G17 ToolCalled → tool_call (schema v0.8) |
| **#175** | RC8 hypothesis_verdict matrix |
| **#192** | RC7r HITL reject |
| **#191** | RC6 post-cap selected refuse |
| **#188** | RC6 selected hyp ref |
| **#183** | RC5 |
| **#182** | G18 mcp_exchange |

## Stack status

RC4→RC5→RC6→RC7r→RC8 **on main**. Graph G8/G17/G18 **on main** (schema **v0.8 / 15/19**).
Next product: **G14a** (#198) → **RC9c** (#105) → RC10c / RC11c / RC12v → G12; then G14 schema; G15.

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| **#198** G14a | `0eaa358` | Prefer: pin + fail-closed count test; duals in flight; merge after dual k=0 + CLEAN |
| **#199** G14a | `daad038` | **Duplicate** of #198 (no count test; has plan.md row) — close; optional plan follow-up after #198 |

## In flight

| Stream | Note |
|--------|------|
| Dual G14a `0eaa358` | `bc-ff63f40b` + `bc-ac11d618` |
| RC9c implement | Prefer sole author `bc-390a60f6` (earlier); twin `bc-780e5126` — **do not** add a third; branch `cursor/rc9c-hypothesis-proposed-1681` still empty vs main |
| G14a implement | `bc-6e563f4c` → #198; `bc-155d270a` → #199 (duplicate — park) |
| Successor coords | pulse14 `bc-fca82455` already running — keep chain; avoid more twins |

## Next

1. Close #199; dual k=0 + CLEAN → merge **#198** G14a
2. RC9c (#105) PR when tip has commits → dual → merge (no duplicate authors)
3. Then RC10c / RC11c / RC12v → G12; G14 schema; G15
4. Parent: CI/PR subscribe on #198 (+ RC9c when open); arm ~15–20m pulse; spawn successor only if pulse14 stalls
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
