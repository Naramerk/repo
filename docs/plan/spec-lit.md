# Plan: CAPABILITIES #1 (SPEC → ResearchSpec) + #3 (LIT literature)

Area owner: ResearchSpec / literature (this agent).
Base: `main` @ `2ac0d5b`. Do **not** merge; stack on `main` unless `Depends` says otherwise.
Out of scope (siblings): agent-layer wiring / real MCP client binding, hypotheses/verdicts, HITL policy engine beyond `spec_approval` hook, planner, GraphBuilder, report, profiles.

Hard rules: no hardcodes/heuristics (config or typed refuse); markers only by hand; every new function has tests; each PR passes local `pytest` + `perf` + `audit_heuristics.py --vs-main`. Depend on narrow Protocols; unit-test with fake LLM and fake MCP (no network).

Acceptance anchors:
- **#1** — role `tz_spec` at stage SPEC → `ResearchSpec` (`object`, `goal`, `scope`, `constraints`, `success_metrics`) schema-valid; `spec_approval` per HITL policy (DESIGN §7.5).
- **#3** — experiment `LIT-n` (`method=literature_search`) when `plan.literature.needed`: ≥5 `artifact(role=paper)` with DOI or URL (dedupe by them); every claim ≥1 evidence with `SourceQuote` (`quote_in_source`); 0 claims without source; ≥1 full text `status=parsed`; failed download → `status=download_failed` (not dropped). MCP `papers_search` / paper analysis via **Protocol** (sibling wires real MCP).

---

## PR list

| # | Branch | Title | Scope (1 line) | Depends |
|---|--------|-------|----------------|---------|
| 1 | `cursor/spec-lit-plan-28fb` | docs: spec-lit PR plan | This file only — full planned PR list for #1/#3. | `main` |
| 2 | `cursor/spec-lit-research-spec-types-28fb` | feat(agents): ResearchSpec typed models | Frozen pydantic `ResearchSpec` (+ Goal/Constraint/SuccessMetric field types) matching CAPABILITIES #1 fields; no runner. | `main` (after #1) |
| 3 | `cursor/spec-lit-research-spec-out-28fb` | feat(agents): ResearchSpecOut + questions | Graph-facing `ResearchSpecOut` (`questions[]` ≥1) aligned with `graph_schema.yaml` / `questions_from_spec_output`. | #2 |
| 4 | `cursor/spec-lit-validate-spec-28fb` | feat(agents): validate_research_spec | Pure `validate_research_spec` → ok \| typed `SpecRefuse(schema_invalid\|…)`; no LLM. | #3 |
| 5 | `cursor/spec-lit-llm-complete-port-28fb` | feat(agents): LlmCompletePort | Narrow `Protocol` for one structured completion (role, messages, output model); fake LLM test double. | `main` (parallel) |
| 6 | `cursor/spec-lit-tz-spec-config-28fb` | feat(agents): tz_spec RoleConfig | Config-only role binding (model, prompt ref, output schema ref, max schema retries); refuse if missing — no defaults in code. | #5 |
| 7 | `cursor/spec-lit-tz-spec-runner-28fb` | feat(agents): tz_spec runner | `run_tz_spec(request, port, config)` → `ResearchSpec` via LLM JSON + validate; one schema retry then typed refuse. | #4, #6 |
| 8 | `cursor/spec-lit-spec-approval-hook-28fb` | feat(agents): spec_approval after SPEC | Hook: after valid SPEC, open/respect `HitlPolicy.gates["spec_approval"]` (interactive open; headless/auto → critic path via existing experimental HITL). No new HITL engine. | #7 |
| 9 | `cursor/spec-lit-literature-out-types-28fb` | feat(agents): LiteratureOut models | Frozen `LiteratureOut` (`papers`, `quotes`, `claims`) + paper draft / claim draft types; stance forbidden on input. | `main` (parallel w/ #2) |
| 10 | `cursor/spec-lit-paper-identity-28fb` | feat(agents): paper DOI/URL identity | Pure normalize + identity key + dedupe-by-DOI/URL (no fetch); refuse blank identity. | #9 |
| 11 | `cursor/spec-lit-papers-search-port-28fb` | feat(agents): PapersSearchPort | Narrow Protocol: `search` / `download` / `parse` shaped like MCP papers_search + paper-analysis; typed errors. | `main` (parallel) |
| 12 | `cursor/spec-lit-fake-papers-mcp-28fb` | test(agents): FakePapersSearch | In-memory fake implementing PapersSearchPort (parsed + download_failed fixtures); no network. | #11 |
| 13 | `cursor/spec-lit-validate-literature-28fb` | feat(agents): validate_literature_out | Pure validator for #3 ACs (paper count/identity, claim↔SourceQuote, parsed≥1, download_failed kept); typed refuse. | #9, #10 |
| 14 | `cursor/spec-lit-researcher-config-28fb` | feat(agents): researcher RoleConfig | Config for literature role (tool names, min_sources from config, prompt/model refs); refuse if `min_sources` absent. | #11 |
| 15 | `cursor/spec-lit-researcher-runner-28fb` | feat(agents): researcher LIT runner | `run_literature_search(ctx, port, llm, config)` → `LiteratureOut` via Protocol tools + LLM claims/quotes; no GraphBuilder. | #12, #13, #14, #5 |
| 16 | `cursor/spec-lit-lit-needed-gate-28fb` | feat(agents): plan.literature.needed gate | Skip/refuse LIT runner when `plan.literature.needed` is false (typed); when true, require method=`literature_search`. | #15 |
| 17 | `cursor/spec-lit-ac-papers-min5-28fb` | test(agents): AC ≥5 papers + dedupe | Acceptance tests: ≥5 paper artifacts with DOI/URL; duplicate DOI/URL collapses. | #15 |
| 18 | `cursor/spec-lit-ac-claims-sourced-28fb` | test(agents): AC claims need SourceQuote | Acceptance tests: every claim ≥1 evidence with SourceQuote; 0 unsourced claims. | #15 |
| 19 | `cursor/spec-lit-ac-parsed-failed-28fb` | test(agents): AC parsed + download_failed | Acceptance tests: ≥1 `status=parsed`; failed download stays as `download_failed` (not omitted). | #15 |
| 20 | `cursor/spec-lit-integration-spec-lit-28fb` | test(agents): SPEC→LIT fake e2e | Headless fake-LLM + fake-MCP path: request → ResearchSpec → (stub plan.literature.needed) → LiteratureOut meeting #1+#3 validators. | #8, #16–#19 |

---

## Stacking notes

- **On `main` (no stack):** #1, #2, #5, #9, #11 once #1 lands (or in parallel with #1 if docs-only conflict-free).
- **Stacked on prior area PR (GitHub `base_branch` = parent head):** #3←#2, #4←#3, #6←#5, #7←#4+#6 (merge base = #4 after #6 on main, or stack #7 on #4 and rebase when #6 merges), #8←#7, #10←#9, #12←#11, #13←#9+#10, #14←#11, #15←#12+#13+#14+#5, #16←#15, #17–#19←#15, #20←#8+#16.
- Sibling **Agent layer / MCP client** owns real `McpClient` → `PapersSearchPort` adapter and tool catalog registration; this area never imports `coscientist.mcp` from agents (import-linter). Sibling **graph** owns `GraphBuilder` from `ResearchSpecOut`/`LiteratureOut`. Sibling **HITL** owns gate engine; we only call existing `open_hitl` / policy read for `spec_approval`.

## Per-PR checklist

1. One coherent slice; own tests for every new function.
2. Local: `pytest -q -m "not slow"`; `pytest -q -m perf`; `python scripts/audit_heuristics.py --vs-main`.
3. Push + open PR immediately; do not wait for GitHub CI; never merge.
