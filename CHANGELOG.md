# Changelog

## Unreleased

### M1 PR7
- `InputSpec.kind: artifact | claim` (+ `ref`); `uses_claim` edge (17th) at P2; claim provenance from producing attempt (H-b).
- P2 bindings (+ `digest`) persisted on `AttemptStarted` (v3 + upcaster); `GraphEdgeAdded` fails loudly on bad endpoints.
- **`kind=verdict` deferred to PR 8** (rejected at `accept_plan` until runtime `verdict()` wiring); enum kept for forward compat.
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
