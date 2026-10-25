# CoScientist-next coordination STATE

**Owner:** coordinator only (`bc-9424e63c-b220-5bfb-b57c-5b2e1148accd`).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 15:30 Europe/Moscow (UTC+3) |
| `main` tip | `90e98df816bd409e6f586f3c852b6895a93118df` (#168 RC17a + #167 R16a.1a) |
| Coordinator | https://cursor.com/agents/bc-9424e63c-b220-5bfb-b57c-5b2e1148accd |
| Previous owner | https://cursor.com/agents/bc-33626709-cebe-4d4f-9734-38a2f6c21681 |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | **none** — write tips ready; parent must ManagePullRequest |

## Merge policy

Standing grant (permanent): merge when adequacy gate passes (`AGENTS.md` § Merge authority). No per-PR ask.

Code quality: reuse/deletion over bandaids. Parallelism: ~6 write streams; uncapped reviewers; one author per write branch.

## Merged (recent)

| PR | Note |
|----|------|
| #168 | RC17a Study.max_attempts from profile |
| #167 | R16a.1a MD code-span lock |
| #166 / #169 / #170 | G14a counts; STATE note; event owners |
| #146→#147→#156→#161 | launch CLI stack |

## Active streams (tips ready — **open PRs**)

| Stream | Issue | Tip / branch |
|--------|-------|--------------|
| G8 | #84 | `9a2ebd0` `cursor/graph-g8-lifecycle-1681` — dual tip reviews in flight |
| API compose | #153 | `7e16e53` `cursor/api-dataroot-compose-1681` — FastAPI __init__ fix; dual tip reviews in flight; **parent open PR** |
| R16a.2 | #120 | `637b073` `cursor/r16a2-report-input-refuse-1681` — ruff I001 fixed; dual tip reviews; **parent open PR**
| RC4 | #100 | implementer spawning `cursor/rc4-require-hypotheses-1681` |
| STATE | docs | this branch |

## Parent must (AS_SUBAGENT_FROM_CLOUD — no ManagePullRequest / no CI subscribe receive)

1. Open PRs (base `main`) for G8, API #153, R16a.2, STATE — then subscribe CI on heads.
2. Adequacy-merge when dual k=0 + CLEAN + green on tip.
3. After each merge wave: restack dependents + update STATE.

## External blocker

Mac `runs_final_20261023` not in VM — document only; advance other streams.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged`.
