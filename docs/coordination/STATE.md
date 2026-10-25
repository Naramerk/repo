# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 05:43 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint | `cursor/coordination-checkpoint-1681` → PR #155 |

## Open critical path

| PR | Tip | Status | Next |
|----|-----|--------|------|
| #146→#147→#156 | `407a525`/`eb6502e`/`e43dc2a` | all **review-ready** | Merge w/ user permission |
| #138→#65 | `ddd49a0`/`5aa05e7` | both **review-ready** | Merge w/ user permission |
| #157 | `392083e` | resolve→invoke on #65; push CI green; PR opened by coordinator | [review](https://cursor.com/agents/bc-00651302-68c2-54ca-9532-280dd5a9e111) |
| #150 | `807d7e3` | **review-ready** | Merge w/ permission; workflow attach [bc-9286243b](https://cursor.com/agents/bc-9286243b-8ec8-56dc-a2d9-365145795bc4) |
| #154 | `dee10d5` | **review-ready** | Merge w/ user permission |

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 2c | Wire resolve→invoke | [bc-935c16bc](https://cursor.com/agents/bc-935c16bc-032c-5e8f-bdd5-10b7ae06b6b4) | **done** → [#157](https://github.com/Naramerk/repo/pull/157) |
| 2d | Review #157 | [bc-00651302](https://cursor.com/agents/bc-00651302-68c2-54ca-9532-280dd5a9e111) | RUNNING |
| 3c | Attach #150 coder to workflow | [bc-9286243b](https://cursor.com/agents/bc-9286243b-8ec8-56dc-a2d9-365145795bc4) | RUNNING |

## Missing inputs

Mac bootstrap/legacy fixtures still unavailable → blocks live legacy replay.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
