# CoScientist-next coordination STATE

**Owner:** coordinator `bc-d50ef046-13b0-5f5d-b5c1-7be13e8a767a`
(https://cursor.com/agents/bc-d50ef046-13b0-5f5d-b5c1-7be13e8a767a).
Peers: pulse13 twin `bc-2b1a7c00…`; pulse14 `bc-fca82455…`; pulse15 `bc-c2678381…`.
Prior pulse8 `bc-157abd96…`. Task subagent: no CI subscribe / Task-spawn / ManagePullRequest;
**can** try `gh pr merge` (create/close/review often 403).

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:24 Europe/Moscow (UTC+3) |
| `main` tip | `c5cca83` (#198 G14a on #197 pulse8) |
| Coordinator | https://cursor.com/agents/bc-d50ef046-13b0-5f5d-b5c1-7be13e8a767a |
| Repo | https://github.com/Naramerk/repo |
| Open product PRs | **#200** RC9c (restack); **#199** G14a duplicate — close |

## Merge policy

Standing grant: dual k=0 tip-match + `MERGEABLE`+`CLEAN` + CI green + stack.
`gh pr merge --merge`. Soft cap ≤6 write streams; one author per write branch.
Do **not** spawn duplicate RC9c/G14a authors.

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
| **#198** | G14a DESIGN/RM counts pin + fail-closed test → `c5cca83` |
| **#197** | STATE pulse8 → `e35ea2f` |
| **#195** | STATE pulse7 |
| **#189** | G17 ToolCalled → tool_call (schema v0.8) |
| **#175** | RC8 hypothesis_verdict matrix |
| **#192** | RC7r |
| **#191** / **#188** | RC6 |
| **#183** | RC5 |
| **#182** | G18 mcp_exchange |

## Stack status

RC4→RC8 **on main**. Graph G8/G17/G18 + **G14a** **on main** (schema **v0.8 / 15/19**).
Next: **RC9c** (#200 / #105) → RC10c / RC11c / RC12v → G12; then G14 schema; G15.

## Open — gate drive

| PR | Tip | Gate |
|----|-----|------|
| **#200** RC9c | `792c60f` | **CONFLICTING** vs main (CHANGELOG vs G14a) + ingest-tar FAIL on pre-restack tip. Sole author restack → dual k=0 + CLEAN → merge |
| **#199** G14a | `daad038` | **Duplicate** of merged #198 — close (DIRTY) |

## In flight

| Stream | Note |
|--------|------|
| RC9c implement | Prefer `bc-390a60f6` (earlier); twin `bc-780e5126` — **no third**. Restack `cursor/rc9c-hypothesis-proposed-1681` onto `c5cca83` |
| Dual G14a | completed via merge of #198 |
| Dual #199 | ignore — closing |
| Successor coords | pulse14/15 already running — **do not** spawn more twins; arm timer on parent |

## Next

1. Close #199
2. RC9c author restack #200 → dual tip review → merge
3. Then RC10c / RC11c / RC12v → G12; G14 schema; G15
4. Parent: open PR for `cursor/coordination-state-pulse13-767a`; CI/PR subscribe on #200 head after restack; arm ~15–20m pulse
5. External Mac fixtures remain blocked — do not invent

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
