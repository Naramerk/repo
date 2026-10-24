# Benchmark harness — PR plan (old vs new CoScientist)

Stacked small PRs on `main` (base `2ac0d5b` at plan time). Each PR is one
coherent slice, own tests, local `pytest` + `pytest -m perf` +
`scripts/audit_heuristics.py --vs-main` green. Never merge from the agent.
Tokens always from provider usage (never estimated). No hardcodes/heuristics.

Integration surface for the parallel agent/MCP work: `BenchRunner` Protocol
(PR 4). Old reference: `aimclub/CoScientist_dev` branch `dev_1` via explicit
local checkout + `module:attr` (may be inaccessible — typed refuse).

| # | Title | Scope (1 line) | Depends on |
|---|--------|----------------|------------|
| 1 | `docs(plan): benchmark harness PR list` | This plan file + structure test | — |
| 2 | `feat(bench): typed harness errors` | `bench/errors.py` (`BenchError` hierarchy) + unit tests | 1 |
| 3 | `feat(bench): task + quality models` | `BenchTask`, `QualityExpectation`, manifest models (required fields only) + tests | 2 |
| 4 | `feat(bench): provider usage + call/result models` | `ProviderTokenUsage` (sum check), `CallCounts`, `RunnerSuccess`/`RunnerRefuse`, timed/suite models + tests | 3 |
| 5 | `feat(bench): BenchRunner Protocol` | Narrow `runtime_checkable` Protocol; no adapters yet + tests | 4 |
| 6 | `feat(bench): versioned easy task set v1` | `bench/tasks/v1/` YAML only (manifest + varied easy research tasks); no loader | 1 |
| 7 | `feat(bench): load_tasks from YAML` | `load_tasks.py` (manifest + relative paths, version match, typed refuse) + tests using v1 | 3, 6 |
| 8 | `feat(bench): quality scoring` | Exact report-field / plan-criterion tallies vs task expectation + tests | 3, 4 |
| 9 | `feat(bench): JSON + markdown report writers` | `result.json` / `summary.md` serializers + tests | 4 |
| 10 | `feat(bench): fake runners` | Deterministic `FakeRunner` + factories (no task ids in code) + tests | 4, 5 |
| 11 | `feat(bench): suite harness` | Time both runners, score quality, assemble `BenchSuiteResult` + e2e with fakes | 5, 7–10 |
| 12 | `feat(bench): old-system adapter` | `OldRunnerConfig` + checkout/`module:attr`/`verify_branch`; typed refuse + tests + doc stub | 5 |
| 13 | `feat(bench): new-system adapter` | `NewRunnerConfig` + injected/entrypoint; `new_runner_unwired` refuse + tests | 5 |
| 14 | `feat(bench): CLI entry` | `python -m bench run`, `scripts/run_bench.py`, `runners.example.yaml` + CLI tests | 11–13 |
| 15 | `docs(bench): BENCHMARK.md + packaging` | User docs, README/CHANGELOG pointers, hatch package/`force-include` for tasks | 6, 14 |

## Hard rules (every PR)

- No guessed defaults, name/shape inference, clamps, silent `except`/`pass`, or estimated tokens.
- Config or typed refusal when wiring/usage is missing.
- Tests for every new function; focused diff (~≤300 changed lines).
- Do not start a later PR until the previous needed dependency is opened (stack on main or on parent PR head as noted in the PR body).
