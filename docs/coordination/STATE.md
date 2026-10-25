# CoScientist-next coordination STATE

**Owner:** coordinator pulse `bc-704a4fbf` (successor spawned each pulse; forge parent retains ManagePullRequest).
Workers return separate handoffs; coordinator merges facts here.

## Observation

| Field | Value |
|-------|-------|
| Observation time | 2026-10-25 16:28 Europe/Moscow (UTC+3) |
| `main` tip | `f3a0030c902c8a8c0fab06c9857d725c0a41f3c0` (#171 MCP-J1 merged) |
| Coordinator pulse | `bc-704a4fbf` |
| Repo | https://github.com/Naramerk/repo |
| Open PRs | #172 R16a.2+R16a.3 · #173 RC4 · #174 STATE — see `OPEN_PR_QUEUE.md` |

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
| [#172](https://github.com/Naramerk/repo/pull/172) R16a.2+#120 + R16a.3+#121 | `d67756e` | check rollup | dual review in flight | NIR framing-only |
| [#173](https://github.com/Naramerk/repo/pull/173) RC4 / #100 | `9293f77` | check rollup | dual review in flight | unblocks RC5 |
| [#174](https://github.com/Naramerk/repo/pull/174) coordination STATE | pulse tip | docs | n/a (docs) | this queue |

## Merged (adequacy OK) — recent

| PR | Note |
|----|------|
| #171 | **MCP-J1** `McpRequestRecorded`/`McpResponseRecorded` — dual k=0 on `edcc735`; G18 now unblocked |
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
| M4 RC | #104 RC8 | **No tip** — blocked on RC4→RC5→RC6→RC7r |
| Graph | **G18** | Unlocked by #171 on main — assign fold when capacity |
| Graph | #84 G8 | Lifecycle graph writes → G7 writer |
| M5 API | #153 | DataRootStudyService composition root |

## Remaining external blocker

**Legacy fixtures** — Mac paths / `runs_final_20261023` not in VM → blocks live legacy comparison. Do not invent inputs; advance other streams.

## Status vocabulary

`implemented` | `mock-verified` | `live-verified` | `review-ready` | `merged` · `done`/`partial`/`blocked`.
