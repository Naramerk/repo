# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-9424e63c-b220-5bfb-b57c-5b2e1148accd`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 15:45 Europe/Moscow (UTC+3) |
| `main` tip | `90e98df816bd409e6f586f3c852b6895a93118df` (#168 RC17a + #167 R16a.1a) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **0** — **CRITICAL:** ManagePullRequest blocked; gh create 403 for Task-subagent |

## Merge policy

Standing grant: merge when adequacy gate passes (`AGENTS.md`). No per-PR ask.

## Merged (recent)

| PR | Note |
|----|------|
| #168 | RC17a Study.max_attempts |
| #167 | R16a.1a code-span lock |
| #166/#169/#170 | G14a / STATE note / event owners |
| #146→#161 | launch CLI stack |

## Adequacy-ready tips (dual k=0 + CI green — **open PR then merge**)

| Stream | Tip | Branch | Reviews |
|--------|-----|--------|---------|
| G8 #84 | `9a2ebd0` | `cursor/graph-g8-lifecycle-1681` | dual k=0 |
| API #153 | `7e16e53` | `cursor/api-dataroot-compose-1681` | dual k=0 |
| R16a.2 #120 | `637b073` | `cursor/r16a2-report-input-refuse-1681` | dual k=0 |

## In review / write

| Stream | Tip | Note |
|--------|-----|------|
| RC4 #100 | `9293f77` | heuristics fixed; dual tip reviews; **parent open PR** |
| TG-Call #88 | `7e24986` `cursor/tg-call-tool-called-1681` | dual tip reviews; **parent open PR**; unlocks G17 |
| STATE | this branch | `67df888`+ |

## Parent must (AS_SUBAGENT_FROM_CLOUD)

1. **ManagePullRequest immediately** for G8, API, R16a.2 (and STATE, RC4 when tip green).
2. `subscribe_github_ci` on heads; after PR CLEAN: `gh pr merge --merge`.
3. Restack dependents after each merge; R16a.3 #121 unblocks after R16a.2 lands.

## External blocker

Mac `runs_final_20261023` — document only; advance other streams.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
