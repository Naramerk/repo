# CoScientist-next coordination STATE

**Owner:** coordinator `bc-fca82455-c2cb-5005-adee-c2378c8c6be4`
(https://cursor.com/agents/bc-fca82455-c2cb-5005-adee-c2378c8c6be4).
Prior: `bc-704a4fbf…` → pulse8 `bc-157abd96…`. Peers pulse11–13 — one owner (this tip).
Task subagent: **no** createPullRequest/close-comment (403); **can** `gh pr merge` + subscribe.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:25 Europe/Moscow (UTC+3) |
| `main` tip | `c5cca83` (#198 G14a on #197) |
| Coordinator | https://cursor.com/agents/bc-fca82455-c2cb-5005-adee-c2378c8c6be4 |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | **#200** RC9c `792c60f` — CONFLICTING vs main (CHANGELOG); restack in flight |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack. `gh pr merge --merge`.
Soft cap ≤6 write streams. One author per write branch.

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK |
| CodeGraph | `query "tool_call mcp_exchange"` |
| LeanCTX | `graph_schema.yaml` lines:1-30 (v0.8) |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#198** | G14a DESIGN/RM counts pin + fail-closed test → `c5cca83` |
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 |
| **#189** | G17 → schema v0.8 / 15 nodes |
| **#175** | RC8 |
| **#191** / **#192** | RC6-cap / RC7r |
| **#188** / **#182** | RC6 / G18 |

## Stack status

RC4→RC8 + G8/G17/G18 + **G14a** **on main**. Schema **v0.8 / 15/19**.
Next: **RC9c** (#200 / #105) → RC10c → RC11c → RC12v → G12. Close stale **#199**.

## In flight

| Stream | Note |
|--------|------|
| RC9c #200 | Tip `792c60f`; sole author prefer `bc-390a60f6` (RUNNING) restack onto `c5cca83`. Do not dual-write with `bc-780e5126` (IDLE). Dual after CLEAN tip. |
| #199 | Superseded by #198 — parent close |
| Successor | spawn pulse15 banked — keep chain |

## Next

1. Restack #200 → dual k=0 → merge
2. RC10c / RC11c / RC12v
3. Parent: open+merge this STATE tip; spawn pulse15
4. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
