# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 11:15 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint | `cursor/coordination-checkpoint-1681` → PR #155 |

## Review-ready stacks (merge when adequacy gate passes)

| Stack | PRs / tips | Independent review |
|-------|------------|-------------------|
| Launch CLI | #146 `407a525` → #147 `eb6502e` → #156 `e43dc2a` → **#161** `ee904f0` | WS1 / WS1c / [WS4f](https://cursor.com/agents/bc-d43e4b03-c607-5ad3-83f6-0370f804b0c4) |
| MCP / events / invoke | #138 `ddd49a0` → #65 `5aa05e7` → #157 `392083e` | WS2b / WS2d |
| Coder sandbox | #150 `807d7e3` → #158 `4a50517` | WS3 / WS3d |
| Study thresholds | #154 `dee10d5` | WS3 |
| Production profiles | #159 `71b4599` | WS4c |
| Durable fork | #160 `83bafba` | WS4e |

All listed tips had CI 10/10 at last observation.

## Active workstreams

None product-coding. Free slots held.

## Merge policy

Standing grant (permanent): merge when adequacy gate passes (`AGENTS.md`).

## Merge progress

In progress — bottom-up stacks after adequacy re-check.

## Remaining external blocker

**Legacy fixtures** — Mac paths / `runs_final_20261023` not in VM → blocks live legacy comparison.


## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
