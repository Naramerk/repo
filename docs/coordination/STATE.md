# CoScientist-next coordination STATE

**Owner:** coordinator pulse `bc-704a4fbf` (successor spawned each pulse; forge parent retains ManagePullRequest).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:25 Europe/Moscow (UTC+3) |
| `main` tip | `90e98df816bd409e6f586f3c852b6895a93118df` (#168 RC17a latest) |
| Coordinator pulse | `bc-704a4fbf` |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #171 MCP-J1 · #172 R16a.2+R16a.3 · #173 RC4 — see `OPEN_PR_QUEUE.md` |

## Merge policy

Standing grant (permanent): merge when adequacy gate passes (`AGENTS.md` § Merge authority). No per-PR ask.

1. Independent review **review-ready** (k=0) on the tip (dual)
2. `mergeable=MERGEABLE` and `mergeStateStatus=CLEAN`
3. Required CI green on that tip
4. Tip SHA matches the reviewed tip (or pure rebase)
5. Stack order preserved

Code quality (`AGENTS.md` § Code quality): prefer reuse/deletion over layered add-ons; do not paper over contract drift with a second path.

Parallelism (`AGENTS.md` § Roles): soft cap ~6 write streams; uncapped parallel read-only reviews; one author per write branch.

## In review (not yet adequacy-merged)

| PR | Tip | CI | Dual review | Note |
|----|-----|-----|-------------|------|
| [#171](https://github.com/Naramerk/repo/pull/171) MCP-J1 / #78 | `edcc735` | CLEAN (prior tip `ab83a8d` red; fixed source_modules) | re-review in flight after k=1 on `ab83a8d` | G18 unlock after merge |
| [#172](https://github.com/Naramerk/repo/pull/172) R16a.2+#120 + R16a.3+#121 | `d67756e` | CI running | dual review in flight | NIR framing-only |
| [#173](https://github.com/Naramerk/repo/pull/173) RC4 / #100 | `9293f77` | tip green pre-PR; PR checks re-running | dual review in flight | unblocks RC5 |

## Merged (adequacy OK) — recent

| PR | Note |
|----|------|
| #168 | RC17a `Study.max_attempts` from profile |
| #167 | R16a.1a MD code-span lock |
| #170 | graph event owners RC12v / TG-Call / MCP-J1 |
| #169 | coordination STATE successor |
| #166 | G14a kind counts 13/19 |
| #138→#65→#157 | events / catalog / invoke provenance |
| #150→#158 | coder sandbox |
| #160 | durable fork |
| #146→#147→#156→#161 | Launch CLI stack |
| #154 | RC3t Study thresholds |
| #149 / #152 | R16a / R16a.1 reporting |
| #143 | G6 Job* events |

## Active backlog (coordinator priority)

| Stream | Issue / slice | Note |
|--------|---------------|------|
| M5 reporting | #120/#121 | **In review** as #172 |
| M4 RC | #100 RC4 | **In review** as #173; then RC5 #101 |
| M4 RC | #104 RC8 | **No tip** — blocked on RC4→RC5→RC6→RC7r critical path |
| MCP / graph | #78 MCP-J1 | **In review** as #171; G18 after land |
| Graph | #84 G8 | Lifecycle graph writes → G7 writer |
| M5 API | #153 | DataRootStudyService composition root |

## Remaining external blocker

**Legacy fixtures** — Mac paths / `runs_final_20261023` not in VM → blocks live legacy comparison. Do not invent inputs; advance other streams.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
