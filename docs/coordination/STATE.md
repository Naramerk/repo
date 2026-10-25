# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 06:18 Europe/Moscow (UTC+3) |
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
| Durable fork | #160 `83bafba` |

## In flight

| PR | Tip | Status |
|----|-----|--------|
| #161 | `ee904f0` | CLI↔profile on #156+#159 — [#161](https://github.com/Naramerk/repo/pull/161); [review](https://cursor.com/agents/bc-d43e4b03-c607-5ad3-83f6-0370f804b0c4) |

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 4d | CLI ↔ production profile | [bc-83281633](https://cursor.com/agents/bc-83281633-e67b-57f7-8569-d85c370e9a39) | **done** → #161 |
| 4f | Review #161 | [bc-d43e4b03](https://cursor.com/agents/bc-d43e4b03-c607-5ad3-83f6-0370f804b0c4) | RUNNING |

Free slots: legacy replay blocked on Mac fixtures; merges await user permission.

## Blocker

Mac legacy fixtures / `runs_final_20261023` not in VM.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
