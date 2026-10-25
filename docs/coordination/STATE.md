# CoScientist-next coordination STATE

**Owner:** coordinator `bc-c2678381-a3bb-5671-9493-bb97ee80f7c4`
(https://cursor.com/agents/bc-c2678381-a3bb-5671-9493-bb97ee80f7c4).
Prior pulse5 `bc-704a4fbf…`; pulse8 `bc-157abd96…` (#197); pulse14 `bc-fca82455…`.
Successor pulse16 `bc-b30590aa…` already RUNNING. Task subagent: **no**
`createPullRequest` (403); **can** `gh pr merge` + CI/PR subscribe.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:25 Europe/Moscow (UTC+3) |
| `main` tip | `c5cca83` (#198 G14a) |
| Coordinator | https://cursor.com/agents/bc-c2678381-a3bb-5671-9493-bb97ee80f7c4 |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | **#200** RC9c tip `9542893` |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack
(`AGENTS.md`). `gh pr merge --merge`. Soft cap ≤6 write streams. One author per
write branch. Dual reviewers uncapped.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK on `main` `c5cca83` |
| CodeGraph | `query "graph_node_upserted"` |
| LeanCTX | `graph_schema.yaml` lines:1-25 (v0.8) |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#198** | G14a pin DESIGN/RM + fail-closed count test → `c5cca83` (**preferred** over competing #199) |
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 → `4bc5c2c` |
| **#189** | G17 ToolCalled → `tool_call` (schema **v0.8 / 15/19**) |
| **#175** | RC8 hypothesis_verdict |
| **#191** | RC6 post-cap selected refuse |
| **#192** | RC7r HITL reject |
| **#188** | RC6 selected hyp ref |
| **#182** | G18 mcp_exchange |

## Closed without merge

| PR | Note |
|----|------|
| **#199** | Competing G14a docs-only (`daad038`); CONFLICTING after #198; closed |

## Stack status

RC4→RC5→RC6→RC7r→RC8 **on main**. Graph G8/G17/G18 + **G14a** **on main**
(schema v0.8, 15 nodes / 19 edges, fail-closed count pin).
Next: **RC9c** (#105 / #200) → RC10c → RC11c → RC12v → G12.
Optional non-blocking: plan `docs/plan/graph.md` G14a row still generic (no live v0.8 note) — do not reopen G14a writer under soft cap.

## In flight

| Stream | Note |
|--------|------|
| **#200 RC9c** | Tip `9542893` on `cursor/rc9c-hypothesis-proposed-1681` (restacked onto `c5cca83`). Sole product writer preferred: `bc-390a60f6` / restack `bc-e547bfa9` — **no second author**. Dual tip-match reviews required on **current** tip (stale `792c60f`/`d86b1e1` duals do not count). Merge when dual k=0 + CLEAN. |
| Soft-cap | Many successor coords RUNNING — **no more product writers**. Dual reviewers OK. pulse16 already alive. |

## Next

1. Dual k=0 on #200 tip `9542893` (or successor tip if restack continues) → merge when CLEAN
2. Open RC10c (#106) after RC9c lands — one author
3. Parent: open+merge this STATE pulse15 tip; spawn duals on tip-match SHA; spawn pulse17 before last successor stops
4. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
