# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 05:23 Europe/Moscow (UTC+3) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| `main` SHA | `7250dab348d9098dcdd1e637f6cf2fe985a94ab0` |
| Checkpoint branch | `cursor/coordination-checkpoint-1681` → PR #155 |
| Handoffs | `docs/coordination/handoffs/ws3-coscientist-reviewer-150-154.md` |

## Open critical path

| PR | Tip | Status | Next |
|----|-----|--------|------|
| #146 | `407a525` | **review-ready** ([WS1](https://cursor.com/agents/bc-8070d1ed-04e3-5bee-9745-5dee4467cf7d)) | Merge only with user permission |
| #147 | `eb6502e` | **review-ready** (same) | Live-run stack in flight; merge with #146 |
| #150 | `807d7e3` (`fb01a30`) | **review-ready** on main@7250dab ([WS3](https://cursor.com/agents/bc-5b6d027c-4eaf-593a-98a3-60886909770f) + [WS3b rebase](https://cursor.com/agents/bc-91467096-f690-5c24-96af-5f75cc722d52)) | Merge only with user permission; workflow attach later |
| #154 | `dee10d5` | **review-ready** (k=0, same WS3) | Merge only with user permission; sole author `bc-2bf00ea8` |
| #138→#65 | `7865016` / `0996804` | #138 CONFLICTING | [WS2 rebase](https://cursor.com/agents/bc-43883508-110a-5472-bb36-559df8c1b0e5) RUNNING |

### Live-run residuals (#146/#147 stack)

Cassette-only ToolRouter; LIT/HYP unwired / REPORT cancelled (honest). Addressed by WS1b without reopening author tips.

## Active workstreams (max 3)

| # | Stream | Owner | Status |
|---|--------|-------|--------|
| 1 | Launch review #146+#147 | [bc-8070d1ed](https://cursor.com/agents/bc-8070d1ed-04e3-5bee-9745-5dee4467cf7d) | **done** |
| 1b | Live-run on #147 | [bc-0890d5ee](https://cursor.com/agents/bc-0890d5ee-8967-566d-90d8-2ffd40517fc9) | RUNNING |
| 2 | Rebase #138 + restack #65 | [bc-43883508](https://cursor.com/agents/bc-43883508-110a-5472-bb36-559df8c1b0e5) | RUNNING |
| 3 | Review #150+#154 | [bc-5b6d027c](https://cursor.com/agents/bc-5b6d027c-4eaf-593a-98a3-60886909770f) | **done** |
| 3b | Rebase #150 onto main | [bc-91467096](https://cursor.com/agents/bc-91467096-f690-5c24-96af-5f75cc722d52) | **done** → tip `807d7e3`, CI 10/10 |

## Context-tools (coordinator)

CodeGraph `1.6.2` + LeanCTX `3.11.4` installed; see prior preflight. Missing Mac bootstrap/legacy fixtures unchanged.

## Dependency order

1. #146→#147 review-ready — await merge permission; parallel live-run stack.
2. #138 rebase → #65 restack → provenance/`invoke_tool` wiring.
3. #150 rebased/review-ready — await merge permission; then workflow attach + Linux preflight; #154 merge when permitted.
4. Production profile + durable fork; legacy regressions when fixtures exist.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · worker `done`/`partial`/`blocked`.
