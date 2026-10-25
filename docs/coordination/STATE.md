# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 05:36 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint | `cursor/coordination-checkpoint-1681` → PR #155 |

## Open critical path

| PR | Tip | Status | Next |
|----|-----|--------|------|
| #146 | `407a525` | **review-ready** | Merge w/ user permission |
| #147 | `eb6502e` | **review-ready** | Merge w/ #146 |
| #156 | `e43dc2a` | **review-ready** CI 10/10 ([WS1c](https://cursor.com/agents/bc-1e469c9f-255c-555d-8784-a8e067aae3af)) | Merge w/ #147 stack |
| #150 | `807d7e3` | **review-ready** on main | Merge w/ permission; workflow attach stack in flight |
| #154 | `dee10d5` | **review-ready** | Merge w/ user permission |
| #138 | `ddd49a0` | **review-ready** | Merge w/ user permission |
| #65 | `5aa05e7` | **review-ready** | Merge after #138; invoke wiring stack in flight |

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 1c | Review #156 | [bc-1e469c9f](https://cursor.com/agents/bc-1e469c9f-255c-555d-8784-a8e067aae3af) | **done** → review-ready |
| 2c | Wire resolve→invoke_tool | [bc-935c16bc](https://cursor.com/agents/bc-935c16bc-032c-5e8f-bdd5-10b7ae06b6b4) | RUNNING |
| 3c | Attach #150 coder to workflow | *(assigning)* | new branch on #150 tip |

Review-ready stack awaiting **explicit merge permission**: #146→#147→#156, #138→#65, #150, #154.

## Missing inputs

Mac bootstrap/legacy fixtures still unavailable in VM → blocks live legacy replay.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
