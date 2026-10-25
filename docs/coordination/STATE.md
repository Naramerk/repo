# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-33626709-cebe-4d4f-9734-38a2f6c21681`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 15:05 Europe/Moscow (UTC+3) |
| `main` tip | `1ef577801bf817a168a77aeea286a12250561ee8` (includes #159–#165, #146–#147, #156, #161) |
| Coordinator | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Attempted successor | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd (cloud subagent: no Task/ManagePullRequest; prepared write tips only) |
| Repo | https://github.com/Naramerk/repo |
| Open write tips | STATE / R16a.1a / RC17a / graph owners / G14a (PRs opening) |

## Merge policy

Standing grant (permanent): merge when adequacy gate passes (`AGENTS.md` § Merge authority). No per-PR ask.

Code quality (`AGENTS.md` § Code quality): prefer reuse/deletion over layered add-ons; do not paper over contract drift with a second path.

Parallelism (`AGENTS.md` § Roles): soft cap ~6 write streams; uncapped parallel read-only reviews; one author per write branch.

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
| #147 | CLI `coscientist run` honest cycle |
| #164 | parallel-agents concurrency guidance |
| #162 | coordination merge-progress STATE |
| #156 | live MCP ToolRouter path + wired-roles seams |
| #161 | production profile via fail-closed `load_profile` |
| #165 | STATE after launch stack on main |
| #149 | R16a typed MinimalReport |
| #152 | R16a.1 reporting formats json/md/html |
| #143 | G6 JobStore + Job* events |
| #142 / #151 | study view/events + cancel FastAPI/WS |

## Launch CLI stack

**Done on `main`:** #146 → #147 → #156 → #161 (linear, adequacy-merged).

## Active backlog (coordinator priority, unblocked)

| Stream | Issue / slice | Note |
|--------|---------------|------|
| M5 reporting | #119 R16a.1a | Code-span locking test (fix already on main in #152) |
| M4 RC | #99 RC17a | Wire `Study.max_attempts` from profile; typed refuse |
| Graph unlock | #87 / #88 / #89 | Assign event owners for G12/G17/G18 |
| Graph | #84 G8 | Lifecycle graph writes → G7 writer |
| Graph docs | G14a | DESIGN §16 counts vs `graph_schema.yaml` |
| M5 API | #153 | Composition root: DataRootStudyService + real data_root tests |

Close when confirmed: #130 (G6 landed as #143), #117/#118 (merged as #149/#152).

## Remaining external blocker

**Legacy fixtures** — Mac paths / `runs_final_20261023` not in VM → blocks live legacy comparison. Do not invent inputs; advance other streams.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
