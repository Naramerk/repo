# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 06:14 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint | `cursor/coordination-checkpoint-1681` → PR #155 |

## Review-ready (await explicit merge permission)

| Stack | Tips |
|-------|------|
| Launch | #146 `407a525` → #147 `eb6502e` → #156 `e43dc2a` |
| MCP/events | #138 `ddd49a0` → #65 `5aa05e7` → #157 `392083e` |
| Coder | #150 `807d7e3` → #158 `4a50517` |
| Thresholds | #154 `dee10d5` |
| Profiles | #159 `71b4599` |
| Durable fork | #160 `83bafba` (CI 10/10; [WS4e](https://cursor.com/agents/bc-f5db1af8-1766-51e7-8283-a640f816c650)) |

Handoff: `docs/coordination/handoffs/ws4a-coscientist-reviewer-durable-fork-160.md`

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 4d | CLI ↔ production profile | [bc-83281633](https://cursor.com/agents/bc-83281633-e67b-57f7-8569-d85c370e9a39) | RUNNING |
| 4e | Review #160 | [bc-f5db1af8](https://cursor.com/agents/bc-f5db1af8-1766-51e7-8283-a640f816c650) | **done** → review-ready |

Free slots held: legacy regression needs Mac fixtures; merge stacks need user permission.

## Blocker

Mac legacy fixtures / `runs_final_20261023` not in VM → no live legacy comparison yet.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
