# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 12:16 Europe/Moscow (UTC+3) |
| `main` tip | `a3c7384f50f1c09f77f284f54aa0db93c6fa896b` (includes #159, #163, #146, #154) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |

## Merge policy

Standing grant (permanent): merge when adequacy gate passes (`AGENTS.md` § Merge authority). No per-PR ask.

Code quality (`AGENTS.md` § Code quality): prefer reuse/deletion over layered add-ons; do not paper over contract drift with a second path.

## Merged (adequacy OK)

| PR | Note |
|----|------|
| #138 | events payload schemas |
| #65 | G3 catalog / ToolResolved |
| #157 | resolve→invoke_tool/provenance |
| #150 | coder sandbox |
| #158 | coder workflow attach |
| #160 | durable fork child journal |
| #155 | coordination checkpoint (AGENTS, roles, STATE, context tools) |
| #159 | production catalog + research.v1 roles |
| #163 | shared `study_kind_attrs` on continue_session |
| #146 | AgentPort adapter on MCP ToolRouter |
| #154 | RC3t required Study thresholds |

## In restack / review

| Stack | PRs | Notes |
|-------|-----|-------|
| Launch CLI | **#147 → #156 → #161** | Base stack after #146 on `main`; restack/review before merge under standing adequacy policy |

## Remaining external blocker

**Legacy fixtures** — Mac paths / `runs_final_20261023` not in VM → blocks live legacy comparison.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
