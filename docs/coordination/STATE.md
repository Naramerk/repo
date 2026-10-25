# CoScientist-next coordination STATE

**Owner:** coordinator `bc-6395f9da-2e65-51ea-a848-930e6aefccac`
(https://cursor.com/agents/bc-6395f9da-2e65-51ea-a848-930e6aefccac).
Parent: `bc-9424e63c`. Prior pulse8 #197 (dual skipped — process miss).
No ManagePullRequest / Task-spawn / gh review / GitHub subscribe here;
`gh pr merge` OK under AGENTS adequacy gate.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 ~17:25 Europe/Moscow (UTC+3) |
| `main` tip | `c5cca83` (#198 G14a pin+test) |
| Coordinator | https://cursor.com/agents/bc-6395f9da-2e65-51ea-a848-930e6aefccac |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **#200** RC9c tip `0b4eee0` MERGEABLE/UNSTABLE |

## Merge policy

Standing grant: dual independent `review-ready` (k=0) on **current tip** +
`MERGEABLE`+`CLEAN` + required CI green + tip match/pure rebase + stack.
Soft cap ≤6 write streams; one author per write branch. **Never merge without dual.**

## Context tools preflight (this coordinator)

| Step | Result |
|------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync` | OK |
| CodeGraph | `query graph_node_upserted` / `hypothesis_node_id` |
| LeanCTX | `graph_writer.py` signatures |

## Merged (recent wave)

| PR | Note |
|----|------|
| **#198** | G14a DESIGN/RM pin v0.8 15/19 + fail-closed count test → `c5cca83` |
| **#197** | STATE pulse8 → `e35ea2f` (dual skipped — process miss) |
| **#195** | STATE pulse7 |
| **#189** | G17 → `e1b14b0` |
| **#175** | RC8 → `94ba209` |
| **#191**/**#192** | RC6 hotfix / RC7r |
| **#188**/**#183**/**#182** | RC6 / RC5 / G18 |

## Stack status

RC4→RC8 + G8/G17/G18 + **G14a** on main (schema **v0.8 / 15/19**).
Next: **#200 RC9c** → RC10c → RC11c → RC12v → G12.
Next graph schema: **G14** remaining `llm_call` / `job` / `plan_expansion`.

## In flight

| Stream | Note |
|--------|------|
| #200 RC9c | tip `0b4eee0` restacked on #198 by this coordinator; local 11 passed; need **independent dual** + CLEAN → merge |
| STATE pulse7b | this tip — parent open PR |
| Successors | keep **one** live; soft-cap writers |

## Next

1. Parent: dual-review #200 `0b4eee0` (restack author ≠ sole dual) → merge when CLEAN
2. Open/merge this STATE pulse7b
3. Spawn RC10c after #200; optional one G14 schema implementer
4. Mac fixtures external — document only

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
