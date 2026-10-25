# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 06:00 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint | `cursor/coordination-checkpoint-1681` → PR #155 |

## Open critical path

| PR / stack | Tip | Status |
|------------|-----|--------|
| #146→#147→#156 | `407a525`/`eb6502e`/`e43dc2a` | **review-ready** — merge w/ permission |
| #138→#65→#157 | `ddd49a0`/`5aa05e7`/`392083e` | **review-ready** — merge w/ permission |
| #150→#158 | `807d7e3`/`4a50517` | **review-ready** — merge w/ permission |
| #154 | `dee10d5` | **review-ready** — merge w/ permission |
| #159 | `71b4599` | production profile/catalog — PR opened by coordinator | [review](https://cursor.com/agents/bc-09b07a70-f1fe-52c5-9bb4-c7de86a668da) |
| durable fork | — | [bc-9780a7e0](https://cursor.com/agents/bc-9780a7e0-957c-5922-84c1-a803bfcf6a1a) RUNNING |

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 4a | Durable fork child journal | [bc-9780a7e0](https://cursor.com/agents/bc-9780a7e0-957c-5922-84c1-a803bfcf6a1a) | RUNNING |
| 4b | Production profile/catalog | [bc-f53a01b5](https://cursor.com/agents/bc-f53a01b5-44fe-5aea-b6ca-305a4fff3b91) | **done** → [#159](https://github.com/Naramerk/repo/pull/159) |
| 4c | Review #159 | [bc-09b07a70](https://cursor.com/agents/bc-09b07a70-f1fe-52c5-9bb4-c7de86a668da) | RUNNING |

## Blocker for legacy replay

Mac bootstrap/legacy fixtures still unavailable in VM.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
