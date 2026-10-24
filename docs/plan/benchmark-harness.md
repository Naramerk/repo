# Benchmark harness — PR plan (old vs new CoScientist)

Small PRs on `main` (plan base: `2ac0d5b`). **Stack depth ≤ 2** (PR →
optional parent → `main` only; no deeper stacks). Each PR: own tests, local
`pytest` + `pytest -m perf` + `scripts/audit_heuristics.py --vs-main`. Agent
never merges. Tokens from provider usage only (never estimated). No
hardcodes/heuristics.

| # | Title | Scope (1 line) | Depends on |
|---|--------|----------------|------------|
| 1 | `docs(plan): benchmark harness PR list` | This plan file | — (`main`) |
| 2 | `feat(bench): typed harness errors` | `bench/errors.py` (`BenchError` hierarchy) + unit tests | 1 (or `main` once 1 merges) |
| 3 | `feat(bench): task + quality models` | `BenchTask`, `QualityExpectation`, manifest models (required fields only) + tests | 2 |
| 4 | `feat(bench): call counts + runner/suite models` | Reuse `coscientist.llm.types.Usage` for tokens (no parallel token type); `CallCounts`, `RunnerSuccess`/`RunnerRefuse`, timed/suite models + tests | 3 |
| 5 | `feat(bench): BenchRunner Protocol` | Narrow `runtime_checkable` Protocol; no adapters yet + tests | 4 |
| 6 | `feat(bench): task set from DESIGN §12.2` | Versioned YAML for §12.2 scenarios only (`two_tasks`, `file_handoff`, `borshevik`, `article_small`, `microfluidics`, `hypothesis_check`, `alembic_nomcp`, `coder_timeout`, `continue_session`, `recovery`, cost/`two_tasks`) — inputs/expected from design + baseline refs, **not invented prompts** | 1 |
| 7 | `feat(bench): load_tasks from YAML` | `load_tasks.py` (manifest, relative paths, version match, typed refuse) + tests on §12.2 set | 3+6 (stack ≤2: open after both on `main`, or stack on the later of 3/6) |
| 8 | `feat(bench): quality compare` | Exact equality checks: required report fields present, plan-criterion id→status match vs task YAML; **no scores, weights, fractions, or other heuristics** + tests | 3+4 |
| 9 | `feat(bench): JSON + markdown report writers` | Serialize suite results to `result.json` / `summary.md` (raw metrics + compare outcomes only) + tests | 4 |
| 10 | `feat(bench): fake runners` | Deterministic `FakeRunner` + factories (no task ids hardcoded) + tests | 4+5 |
| 11 | `feat(bench): suite harness` | Wall-time both runners, attach `Usage`/calls/quality compare, write outputs + e2e with fakes | 5+7–10 (land deps on `main` first; this PR → `main`) |
| 12 | `feat(bench): old-system adapter` | Explicit local checkout + `module:attr` + optional `verify_branch` for `aimclub/CoScientist_dev` `dev_1`; typed refuse if missing — no path/name heuristics + tests + short doc | 5 |
| 13 | `feat(bench): new-system adapter` | Wire real new `BenchRunner` from the agent/MCP layer **when it exists** — **no `new_runner_unwired` shim**; block this PR until that runner is available | 5 + new runner |
| 14 | `feat(bench): CLI entry` | `python -m bench run`, `scripts/run_bench.py`, `runners.example.yaml` + CLI tests | 11+12 (13 when ready) |
| 15 | `docs(bench): BENCHMARK.md + packaging` | User docs, README/CHANGELOG pointers, hatch package / task YAML `force-include` | 6+14 |

## Hard rules (every PR)

- No guessed defaults, name/shape inference, clamps, silent `except`/`pass`, or estimated tokens.
- Reuse `coscientist.llm.types.Usage` for provider token fields (item 4); do not invent a second usage model.
- Task set (item 6) = DESIGN §12.2 scenario table only.
- Quality (item 8) = exact presence/status compare; no scoring heuristics.
- New adapter (item 13) waits for the real runner — no unwired placeholder shim.
- Config or typed refusal when wiring/usage is missing.
- Tests for every new function; focused diff; **stack ≤ 2**.
