# CoScientist-next coordination STATE

**Owner:** coordinator `bc-2b1a7c00-6bf7-5bc2-a4ef-1446995e54be`
(https://cursor.com/agents/bc-2b1a7c00-6bf7-5bc2-a4ef-1446995e54be).
Peers: pulse13 twin `bc-d50ef046…`; pulse14 `bc-fca82455…`.
Prior: pulse8 `bc-157abd96…` ← pulse10 `bc-1bbeb979…` ← pulse5 `bc-704a4fbf…`.
Task subagent: no ManagePullRequest / Task-spawn; **can** `gh pr merge`.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:22 Europe/Moscow (UTC+3) |
| `main` tip | `e35ea2f` (#197 STATE pulse8) |
| Coordinator | https://cursor.com/agents/bc-2b1a7c00-6bf7-5bc2-a4ef-1446995e54be |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | **#198** G14a (prefer) · **#199** G14a dup |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack.
Soft cap ≤6 write streams; uncapped read-only dual reviewers. One author per write branch.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK |
| CodeGraph | `query "graph_schema"` → `GRAPH_SCHEMA_VERSION` |
| LeanCTX | `graph_schema.yaml` lines:1-20 |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 → `4bc5c2c` |
| **#189** | G17 ToolCalled → tool_call (schema v0.8 / 15 nodes) → `e1b14b0` |
| **#175** | RC8 hypothesis_verdict matrix → `94ba209` |
| **#191** | RC6 post-cap selected refuse → `a881d22` |
| **#192** | RC7r HITL reject → `ead63eb` |
| **#188** | RC6 selected hyp ref → `eccaf56` |
| **#182** | G18 mcp_exchange |
| **#183** | RC5 |
| **#177** | TG-Call |
| **#171** | MCP-J1 |

## Stack status

RC4→RC5→RC6→RC7r→RC8 **on main**. Graph G8/G17/G18 **on main** (schema **15/19 v0.8**).
Next after G14a: **RC9c** (#105) → RC10c → RC11c → RC12v → G12.

## In flight

| Stream | Note |
|--------|------|
| **#198** G14a | tip `0eaa358` `cursor/graph-g14a-design-counts-1681` — pin DESIGN/RM + fail-closed count test; **prefer** |
| **#199** G14a | tip `daad038` `cursor/graph-g14a-counts-v08-1681` — **close as dup** after #198 |
| RC9c implement | `bc-780e5126` (+ peer `bc-390a60f6`) HypothesisProposed via G7; branch tip still = main |
| Successor | pulse14 `bc-fca82455` running; spawn pulse15 before stop |

## Next

1. Dual k=0 + CLEAN → merge **#198** G14a; close **#199**
2. Open PR for RC9c when tip pushes → dual → merge
3. RC10c / RC11c / RC12v stack
4. Parent/ManagePullRequest: open PR for this pulse13 tip if create API blocked
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
