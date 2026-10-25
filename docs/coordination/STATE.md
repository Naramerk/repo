# CoScientist-next coordination STATE

**Owner:** coordinator `bc-157abd96-b014-5f8a-b225-2e7cb0f3afc0`
(https://cursor.com/agents/bc-157abd96-b014-5f8a-b225-2e7cb0f3afc0).
Peers/successors: pulse10–12 (`bc-1bbeb979…`, `bc-f305b1d7…`, `bc-3705a06c…`).
Prior: `bc-704a4fbf…` → `bc-5b7a4e5e…` / `bc-2d2097c2…` / `bc-036c2b4b…`.
Task subagent: no CI subscribe / Task-spawn / createPullRequest; **can** `gh pr merge`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:18 Europe/Moscow (UTC+3) |
| `main` tip | `4bc5c2c` (#195 STATE pulse7 on #189 G17 / #175 RC8) |
| Coordinator | https://cursor.com/agents/bc-157abd96-b014-5f8a-b225-2e7cb0f3afc0 |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | none — RC9c (#105) + G14a implementers in flight |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack.
Soft cap ≤6 write streams (was exceeded during RC6–RC8 wave — prefer reviewers only until RC9c/G14a PRs open). One author per write branch.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK |
| CodeGraph | `query "graph_fold mcp tool_called"` |
| LeanCTX | `graph_fold/__init__.py` signatures |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#195** | STATE pulse7 → `4bc5c2c` |
| **#189** | G17 ToolCalled → tool_call (schema v0.8) → `e1b14b0` |
| **#175** | RC8 hypothesis_verdict matrix → `94ba209` |
| **#191** | RC6 post-cap selected refuse → `a881d22` |
| **#192** | RC7r HITL reject → `ead63eb` |
| **#190** | STATE pulse6 (stale tip; superseded) |
| **#188** | RC6 selected hyp ref → `eccaf56` |
| **#183** | RC5 |
| **#182** | G18 mcp_exchange |

## Stack status

RC4→RC5→RC6→RC7r→RC8 **on main**. Graph G8/G17/G18 **on main**.
Next: **RC9c** (#105), **G14a** (DESIGN counts vs schema v0.8), then RC10c/RC11c/RC12v → G12.

## In flight

| Stream | Note |
|--------|------|
| RC9c implement | `bc-390a60f6` HypothesisProposed via G7 |
| G14a implement | `bc-6e563f4c` DESIGN §16 counts |
| Successor coords | pulse10–12 running — keep chain; do not park |

## Next

1. Open PRs for RC9c / G14a → dual k=0 → merge
2. RC10c / RC11c / RC12v stack
3. G14 schema kinds after G14a; G15 drop 2nd write path
4. Parent: open PR for this pulse8 tip if create API blocked for Task subagents
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
