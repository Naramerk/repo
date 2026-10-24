# Benchmark harness — PR plan (old vs new CoScientist)

Small PRs on `main` (plan base: `2ac0d5b`). **Stack depth ≤ 2**. **Open a
PR only after its dependency merges** (do not speculative-stack): the core
chain **2 → 3 → 4 → 5** (table rows below) lands one-at-a-time onto `main`.
Each PR: own tests; local `pytest` + `pytest -m perf` +
`scripts/audit_heuristics.py --vs-main`. Agent never merges. Tokens from
provider usage only (never estimated). No hardcodes/heuristics. **No task ids
in code** — tasks are versioned data only.

| # | Title | Scope (1 line) | Depends on |
|---|--------|----------------|------------|
| 1 | `docs(plan): benchmark harness PR list` | This plan file | — (`main`) |
| 2 | `feat(bench): typed harness errors` | `bench/errors.py` (`BenchError` hierarchy) + unit tests | 1 merged |
| 3 | `feat(bench): task + quality models` | `BenchTask`, quality/expected fields, manifest models (required only) + tests | 2 merged |
| 4 | `feat(bench): runner/suite models (Usage + UsageRecorded)` | Reuse `coscientist.llm.types.Usage`; call accounting via **scheduler-owned `UsageRecorded`** (S1, `docs/plan/scheduler.md`; DESIGN §10) — **no harness `CallCounts` type**; `RunnerSuccess`/`RunnerRefuse`, timed/suite models + tests | 3 merged |
| 5 | `feat(bench): BenchRunner Protocol` | Narrow `runtime_checkable` Protocol; new-side entrypoint = agent-layer **`Orchestrator`** (G1/G1b, [#66](https://github.com/Naramerk/repo/pull/66)) — harness does not invent a second runner API + tests | 4 merged |
| 6 | `feat(bench): HER task set from runs_final_20261023` | Versioned YAML under `bench/tasks/` derived from old-CoScientist extract `runs_final_20261023` (scenarios below) — prompts/inputs/expected from that run’s artifacts + `SUMMARY.md`/`metrics.json`/`compare.json`; **not invented**; **not §12.2-only** (includes `web`, `recovery2`; omits §12.2 rows absent from this extract) | 1 merged |
| 7 | `feat(bench): load_tasks from YAML` | `load_tasks.py` (manifest, relative paths, version match, typed refuse) + tests on HER set | 3+6 merged |
| 8 | `feat(bench): quality compare` | Exact equality vs task YAML expected (status, exp_tasks/evidence fields as declared in data); **no scores, weights, fractions, or other heuristics** + tests | 3+4 merged |
| 9 | `feat(bench): JSON + markdown report writers` | Serialize suite to `result.json` / `summary.md` (raw wall time, `Usage` / scheduler `UsageRecorded` aggregates, quality compare) + tests | 4 merged |
| 10 | `feat(bench): fake runners` | Deterministic `FakeRunner` + factories driven by loaded task data (no task ids hardcoded) + tests | 4+5 merged |
| 11 | `feat(bench): suite harness` | Wall-time both runners; attach provider `Usage` + scheduler `UsageRecorded`-derived counts; quality compare; write outputs + e2e with fakes | 5+7–10 merged |
| 12 | `feat(bench): old-system adapter` | Explicit local checkout + `module:attr`; **branch always verified** against `expected_branch` (refuse on mismatch or missing `.git` — not a config flag) for `aimclub/CoScientist_dev` `dev_1` — no path/name heuristics + tests + short doc | 5 merged |
| 13 | `feat(bench): new-system adapter` | Wire `BenchRunner` to agent-layer **`Orchestrator`** (G1/G1b, [#66](https://github.com/Naramerk/repo/pull/66)) when ready — **no `new_runner_unwired` shim**; block until Orchestrator exists | 5 merged + Orchestrator |
| 14 | `feat(bench): CLI entry` | `python -m bench run`, `scripts/run_bench.py`, `runners.example.yaml` (always verifies old checkout branch) + CLI tests | 11+12 merged (13 when ready) |
| 15 | `docs(bench): BENCHMARK.md + packaging` | User docs (HER provenance), README/CHANGELOG; **hatch wheel `packages` includes `bench`** + task YAML `force-include` | 6+14 merged |

## HER task set source (`runs_final_20261023`)

Provenance: text extract of old `dev_1` runs (`bc65cf9`), artifacts
`SUMMARY.md` / `metrics.json` / `compare.json` / per-scenario `result.json`
(+ `webdrive_result.json` where used). Item 6 materializes these as versioned
task YAML (ids = scenario names from the extract — in data files only).

| scenario | observed status (this run) | dur_s | llm_calls | tokens (usage.jsonl) | exp_tasks | evidence (observed) |
|---|---|---|---|---|---|---|
| `two_tasks` | fulfilled | 1172 | 53 | 1_097_723 | 2 | success:2 |
| `hypothesis_check` | fulfilled | 906 | 53 | 868_394 | 1 | success:1 |
| `borshevik` | partial | 2566 | 99 | 3_192_669 | 1 | null:1, success:1 |
| `alembic_nomcp` | fulfilled | 1019 | 76 | 1_694_593 | 1 | success:1 |
| `microfluidics` | timeout | 2402 | 111 | 2_613_368 | 0 | null:19 |
| `article_small` | partial | 641 | 37 | 600_791 | 1 | success:1 |
| `web` | graph:fulfilled | 835 | 0† | 1_115_909 | 2 | success:2 |
| `recovery` | graph:fulfilled | 902 | 59 | 1_003_049 | 2 | success:2 |
| `file_handoff` | unfulfilled | 1443 | 93 | 2_133_695 | 6 | success:2 |
| `recovery2` | graph:fulfilled | 955 | 60 | 1_049_306 | 2 | success:2 |

† `web`: top-level `metrics.json` / `SUMMARY.md` report `llm_calls=0`, but the
same row’s `tokens.source=usage.jsonl` records **55** provider calls /
**1_115_909** tokens (`deepseek-v4-flash`). The zero is an old-run counter
gap on the webdriver path, not “no LLM use”. Harness token/call metrics must
come from provider `Usage` / scheduler `UsageRecorded`, never from that
undercounted `llm_calls` field alone.

Expected outcomes/metrics in task YAML are taken from this extract (and
DESIGN §12.2 expected where the extract records an expected/check — never
invented prompts). Comparison metrics for the harness: wall time, provider
`Usage` / journaled scheduler `UsageRecorded`, tool events as recorded,
quality fields declared in the YAML.

## Hard rules (every PR)

- **Open a PR only after its dependency merges** (chain **2 → 3 → 4 → 5**; elsewhere wait for listed deps on `main`). **Stack depth ≤ 2**.
- No guessed defaults, name/shape inference, clamps, silent `except`/`pass`, or estimated tokens.
- Tokens: `coscientist.llm.types.Usage`; LLM-call accounting: **scheduler-owned `UsageRecorded`** (S1, `docs/plan/scheduler.md`) — **do not add harness `CallCounts`**.
- `BenchRunner` new entrypoint = agent-layer **`Orchestrator`** (G1/G1b, #66); no parallel runner API. (#54 is the MCP adapter — not the Orchestrator.)
- Task set (item 6) = **HER scenarios from `runs_final_20261023`**, versioned data only (no task ids in code).
- Quality (item 8) = exact compare vs YAML expected; no scoring heuristics.
- Old adapter: **branch always verified** against `expected_branch` (not a toggle/flag).
- New adapter (item 13): wait for Orchestrator (#66) — no unwired placeholder shim.
- Config or typed refusal when wiring/usage is missing.
- Tests for every new function; focused diff.
