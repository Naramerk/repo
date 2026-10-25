# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-704a4fbf-db89-5945-a0f2-4069baad8e76`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:25 Europe/Moscow (UTC+3) |
| `main` tip | `90e98df816bd409e6f586f3c852b6895a93118df` (#167 R16a.1a + #168 RC17a) |
| Coordinator | https://cursor.com/agents/bc-704a4fbf-db89-5945-a0f2-4069baad8e76 |
| Previous | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd (obligations assumed) |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **0** — AS_SUBAGENT: no ManagePullRequest; `gh pr create` 403 → **parent must open** (see store `OPEN_PR_QUEUE.md`) |

## Merge policy

Standing grant (permanent): merge when adequacy gate passes (`AGENTS.md` § Merge authority). No per-PR ask.

Code quality (`AGENTS.md` § Code quality): prefer reuse/deletion over layered add-ons; do not paper over contract drift with a second path.

Parallelism (`AGENTS.md` § Roles): soft cap ~6 write streams; uncapped parallel read-only reviews; one author per write branch.

## Context tools preflight (this owner)

| Check | Result |
|-------|--------|
| `codegraph --version` | `1.6.2` (exit 0) |
| `lean-ctx --version` | `3.11.4` (exit 0) |
| `codegraph init/sync/status` | OK (316 files indexed) |
| CodeGraph query | `StudyService lifecycle graph writer` + `callers graph_node_upserted` |
| LeanCTX | `graph_writer.py -m signatures` |

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
| #166 | G14a schema counts |
| #167 | R16a.1a code-span lock |
| #168 | RC17a Study.max_attempts from profile |
| #169 / #170 | STATE successor + event owners |

## Adequacy-ready (dual k=0 + CI green — **open PR → merge**)

| Stream | Tip | Branch | Dual reviews |
|--------|-----|--------|--------------|
| G8 #84 | `9a2ebd0` | `cursor/graph-g8-lifecycle-1681` | handoffs + tip CI green |
| API #153 | `7e16e53` | `cursor/api-dataroot-compose-1681` | bc-b2065673 + bc-5512506d |
| R16a.2 #120 | `637b073` | `cursor/r16a2-report-input-refuse-1681` | bc-066ed97f + bc-c971ffd5 |
| RC4 #100 | `9293f77` | `cursor/rc4-require-hypotheses-1681` | bc-0d4fc8e2 + bc-442302af |
| TG-Call #88 | `cf51b22` | `cursor/tg-call-tool-called-1681` | bc-20cb6044 + bc-7db5b313 |

## In review / tip drift

| Stream | Tip | Note |
|--------|-----|------|
| MCP-J1 #78 | `edcc735` | was `ab83a8d` (k=1 pyproject); fix pushed; dual re-review after CI green |
| RC8 | — | implementer https://cursor.com/agents/bc-aca2a7c6-47de-5cee-aef1-5e62d17d8489 RUNNING |

## Active backlog (after PR merges)

| Stream | Issue | Depends |
|--------|-------|---------|
| R16a.3 | #121 NIR format | after R16a.2 |
| RC5 | #101 | after RC4 |
| G17 | #87 | after TG-Call |
| G18 | #89 | after MCP-J1 |
| RC11c / RC12v | #107 / owners | after RC8 chain |

## Remaining external blocker

**Legacy fixtures** — Mac paths / `runs_final_20261023` not in VM → blocks live legacy comparison. Do not invent inputs; advance other streams.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
