# CoScientist-next coordination STATE

**Owner:** coordinator `bc-157abd96-b014-5f8a-b225-2e7cb0f3afc0`
(https://cursor.com/agents/bc-157abd96-b014-5f8a-b225-2e7cb0f3afc0).
Peers/successors: pulse10–12 (`bc-1bbeb979…`, `bc-f305b1d7…`, `bc-3705a06c…`).
Prior: `bc-704a4fbf…` → `bc-5b7a4e5e…` / `bc-2d2097c2…` / `bc-036c2b4b…`.
Task subagent: no CI subscribe / Task-spawn / createPullRequest; **can** `gh pr merge`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-28 ~07:35 Europe/Moscow (UTC+3) |
| `main` tip | `9ba7e9a` (#296 strict-sanitizer; prior #294 S1 evidence_grounded, #293 receipt-repair) |
| Coordinator | https://cursor.com/agents/bc-157abd96-b014-5f8a-b225-2e7cb0f3afc0 |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | #298 S2 acceptance-route closure (`65f3044`) — independent review in flight |

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
| **#296** | strict-sanitizer wire-boundary (#265) → `9ba7e9a`, review k=0 |
| **#294** | S1 acceptance-route resolver (evidence_grounded) → `e25a66e` |
| **#293** | LiteratureOut receipt-binding repair (#289) → `1139bb0` |
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
| S2 route-closure review | PR #298 `65f3044`; on review-ready k=0 + CI green → merge per standing grant |
| Live relaunch | after #298: restart 8487 (picks up deadline_s 14400 presets) → POST /console/runs calc+bor7 → `bench/lab/glm/live/watch_and_approve.py` |
| Laws wave #290 | owner-ticked axes pending implementation (after relaunch) |
| Backlog | #295 report honesty (need-stage skips), #297 prompt/wire strict mismatch, #274 new-domain task, #271 branch prune |

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
