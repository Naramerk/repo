# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 05:33 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint | `cursor/coordination-checkpoint-1681` → PR #155 |

## Open critical path

| PR | Tip | Status | Next |
|----|-----|--------|------|
| #146 | `407a525` | **review-ready** | Merge w/ user permission |
| #147 | `eb6502e` | **review-ready** | Merge w/ #146 |
| #156 | `e43dc2a` | live-run stack on #147 · **CI 10/10** | [review](https://cursor.com/agents/bc-1e469c9f-255c-555d-8784-a8e067aae3af) |
| #150 | `807d7e3` | **review-ready** on main | Merge w/ user permission |
| #154 | `dee10d5` | **review-ready** | Merge w/ user permission |
| #138 | `ddd49a0` | **review-ready** ([WS2b](https://cursor.com/agents/bc-0b85c3e9-2f9c-5236-8680-609a4703cebe)) | Merge w/ user permission |
| #65 | `5aa05e7` | **review-ready** (same) | Merge after #138; wiring stack in flight |

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 1c | Review #156 | [bc-1e469c9f](https://cursor.com/agents/bc-1e469c9f-255c-555d-8784-a8e067aae3af) | RUNNING |
| 2b | Review #138+#65 | [bc-0b85c3e9](https://cursor.com/agents/bc-0b85c3e9-2f9c-5236-8680-609a4703cebe) | **done** → both review-ready |
| 2c | Wire resolve→invoke_tool/provenance | [bc-935c16bc](https://cursor.com/agents/bc-935c16bc-032c-5e8f-bdd5-10b7ae06b6b4) | RUNNING · `cursor/tool-resolve-invoke-provenance-1681` on #65 |

## Missing inputs

Mac bootstrap/legacy fixtures still unavailable in VM.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
