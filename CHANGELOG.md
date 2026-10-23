# Changelog

## Unreleased

### M1 PR8b
- Hypothesis verdict model (`docs/DESIGN_HYP_DELTA.md`): claim `validity`/`outcome`/`severe`/`independence_key`; stub|partial|lit|invalid excluded **before** counting.
- `independence_key` base = `experiment_id`; count-time merge by **transitive root-data** digest overlap (code vs data from core **`ResultRecorded.output_kinds`** / plan **OutputSpec** — never agent `media_type` or InputBinding slot names; all-code upstream → intermediate is a root; diamond DFS uses on_stack vs done) or **structural** share-any criterion template (same target, empty roots; empty plan criteria → `catalog.defaults["experiment"]`). No TaskSpec/goal/title/plan_id hash. Method is not in the key.
- **Symmetric trust:** `Artifact.producer` stamped by write path (`put`→agent, `put_core`→core, `put_sandbox`→sandbox). `effect.trusted` from producer; agent cannot set it. Trusted evidence required for **both** `refuted` (`k_ref`) and severe/`supported`. Untrusted failing metrics → indeterminate, never refutation.
- On journal read/replay: **never trust** stored `severe`/`refute_capable`/`effect.trusted=True` — recompute (`trusted` from `effect.producer`).
- Study: **`supported_needed`** (default 2, ≥2); `confirmations_needed` = PR8 snapshot `k`. StudyCreated **v6**.
- `aggregate='evalue'` rejected at create (deferred until core computes p-values).
- Report: `hypothesis_report_row` in finalize; failures set dedicated `hypotheses_unavailable` (not a fake hypothesis id). Bind snapshots win on replay; finalize recomputes report rows.
- Keeps PR8: check roles, per-experiment `validity_failed` / `unknown_role`, `VerdictSnapshotsRecorded`.

### M1 PR8
- Runtime `verdict()` wired into `dispositions()` / finalize (Study `confirmations_needed` / `refutations_needed` / `refutation_priority`); supported → answered; hypothesis verdicts in StudyFinalized.
- `InputSpec.kind=verdict` enabled; `VerdictSnapshotsRecorded` persists full snapshots (not digest-only); invalidated on claim/`about` changes (scoped to affected target).
- Claim carry-overs: worst provenance; no FAILED claims; tight whitelist (`target`/`checked_against`/`reason`/`premise_idx`); core-derived counting fields (`criterion_status` from checked_against ReqStatus; `frozen` from plan-accept criterion nodes; `accepted` = SUCCESS ∧ validated); intent from producing experiment's `tests` edge; namespaced claim ids; unknown targets rejected; `about` required.
- `open_runtime` (core + experimental) builds Executor via `make_executor` (live `get_graph`); `catalog=` required (no test-catalog fallback); one `k`=`confirmations_needed` everywhere.
- `accepted` = recorded scientific result ∧ structural claim validation (not criteria pass). Infra failures only drop claims (`result_class`).
- Check `role` (Duhem–Quine): catalog-only; `RequirementTemplate.role` **required** in catalog format; plan mismatch rejected; `unknown_role` exclusion never forces a verdict; validity failure is per-experiment (`excluded_claims`), inconclusive only when no valid claims remain; scientific FAILED no retry / no H-c cascade.
- `hypothesis_verdicts` kept separate from bind `verdict_snapshots` cache; Q/H grouping keys are kind-prefixed.
- StudyCreated schema_version=4 + v3→v4 upcaster for `confirmations_needed` (no longer piggybacked on v2→v3).
- R17 runtime metadata wiring deferred (DESIGN note + xfail); pure-rule path remains in PR 6.
- Totality: garbage claim attrs dropped (never raise into finalize/view/start_attempt).
- Priority-refuted provenance exception unchanged; report/snapshot `note` when stub/partial supports accompany priority-win (`refuted; N supporting results were stub/partial`).
- Compat: `view()` with graph/study uses PR8 dispositions; PR7 journals without counting claims stay `not_attempted`.

### M1 PR7
- `InputSpec.kind: artifact | claim` (+ `ref`); `uses_claim` edge (17th) at P2.
- `Executor.record` writes claim nodes with `attrs.provenance` from the producing attempt; unknown → bind as `stub` (never real).
- P2 bindings (+ metadata `digest`) on `AttemptStarted` (v3 + upcaster); `GraphEdgeAdded` fails loudly.
- `kind=verdict` landed in **PR 8**.
- TaskCreated v4→v5 upcaster defaults `kind=artifact`.
- #12 leftovers: R17 confirm-only; refmodel `intent` Literal; gen edge attrs; cancel `finalize_mod`; r17 module-level import.

### M1 PR6
- `tests.intent` (confirm|refute, edge attr in graph_schema): refute+met → verdict `survived`; `Study.refutations_needed` / `refutation_priority` at create.
- Pure rules only: `k_ref` / `refutation_priority` / `intent` take effect in production once **PR 8** wires `verdict()` into runtime finalize.
- Provenance ceiling: ≥ k_ref real priority refs stay `refuted` (stub/partial supports do not cap a refutation).
- R17 wired into `counts()`/`verdict()` via `claim_counts_under_r17` (criterion before cited evidence; threshold-source excluded).
- Leftovers: H-c+cancel test; H-c+budget via `record_budget_debit` (`_artifact_store` already uses `executor.artifacts`).

### M1 follow-up (post PR5)
- Confirm `delegated_costly ≤ max_cost_rub` is create_study-only; replay test pins `Study.model_validate` / local_project reopen.
- Skip H-c when `_budget_exceeded` / `_study_cancelled` so children close with budget/cancelled, not `input_unavailable`.
- Graph `experiment.method` aligned to optional (matches `TaskSpec.method`).

### M1 PR5
- `InputSpec.mode` default is `forbid` (stubs only with an explicit mode); TaskCreated v3→v4 upcaster restores `use_partial` for old journals.
- `TaskSpec.tolerate_failed_dep` and H-c: FAILED/SKIPPED parents without `InputSpec` skip children as `input_unavailable`. Stage tasks (SPEC…REPORT) are excluded from H-c so a failed SPEC still allows PLAN/REPORT (DESIGN §7.5).
- `ExperimentMethod` enum matches graph schema; `method=simulation` is scientific metadata and does **not** upgrade stub/partial input provenance.

### M1 PR4 (API note)
- `coscientist.core.intake.intake` / `coscientist.core.reasons.intake` are **async** (`await intake(...)`). Sync call sites must use `asyncio.run` or an event loop. Classifier timeouts use `asyncio.wait_for(asyncio.to_thread(...))`.
