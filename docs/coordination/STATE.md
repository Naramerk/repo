# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-9424e63c-b220-5bfb-b57c-5b2e1148accd`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:15 Europe/Moscow (UTC+3)
| `main` tip | `90e98df816bd409e6f586f3c852b6895a93118df` |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **0** — AS_SUBAGENT_FROM_CLOUD: gh create 403; **parent ManagePullRequest required** |
| Successor | spawning cloud successor with full mandate (anti-stop); previous owner remains until STATE claim |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). No per-PR ask.

## Merged (recent)

| PR | Note |
|----|------|
| #168 / #167 | RC17a / R16a.1a |
| #166/#169/#170 | G14a / STATE / event owners |
| #146→#161 | launch CLI |

## Adequacy-ready (dual k=0 + CI green — **open PR → merge**)

| Stream | Tip | Branch |
|--------|-----|--------|
| G8 #84 | `9a2ebd0` | `cursor/graph-g8-lifecycle-1681` |
| API #153 | `7e16e53` | `cursor/api-dataroot-compose-1681` |
| R16a.2 #120 | `637b073` | `cursor/r16a2-report-input-refuse-1681` |
| RC4 #100 | `9293f77` | `cursor/rc4-require-hypotheses-1681` |
| TG-Call #88 | `cf51b22` | `cursor/tg-call-tool-called-1681` |
| MCP-J1 #78 | `edcc735` | `cursor/mcp-j1-recorded-1681` |

## In flight

| RC8 #104 | `6e84fe9` `cursor/rc8-hypothesis-verdict-1681` | dual tip reviews; **parent open PR** |

| Stream | Tip | Note |
|--------|-----|------|
| RC8 #104 | `6e84fe9` `cursor/rc8-hypothesis-verdict-1681` | dual tip reviews; **parent open PR** |
| STATE | this branch | |

## Parent must

1. ManagePullRequest for all adequacy-ready tips (+ STATE `4590307`+).
2. subscribe_github_ci; `gh pr merge --merge` under gate.
3. After R16a.2 → spawn R16a.3 #121; after RC4 → RC5 #101.

## External blocker

Mac `runs_final_20261023` — document only.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
