# DESIGN_HYP_DELTA — hypothesis verdict model (M1 PR 8b)

Base: DESIGN_M1.md with PR 1–8 merged. Rationale: [`HYPOTHESIS_LIT.md`](HYPOTHESIS_LIT.md).
Scope: claim classification, the hypothesis verdict, question disposition mapping, and the report payload. No change to the task/attempt lifecycle.

## Reconciliation with merged PR 8 (code wins on mechanics)

| Written SPEC | Merged code / this PR |
|---|---|
| Named PR 20; projection-only / no new events | **PR 8b**; **keep `VerdictSnapshotsRecorded`** for bind |
| StudyCreated v3→v4 for study fields | **v5→v6** for `supported_needed` (`confirmations_needed` took v4; aggregate took v5) |
| Separate `min_independent_support` | Named **`supported_needed`** (default 2, ≥2); **do not** reuse `confirmations_needed` |
| validity failure caps the question | **Per-experiment** `excluded_claims` / `validity_failed`; keep **`unknown_role`** |
| Event names `ClaimRecorded` / `CriterionFrozen` | Real journal: claim/criterion attrs on **`GraphNodeUpserted`** |
| E-value aggregation in M1 | **Deferred** — `aggregate='evalue'` rejected at StudyCreated until core computes p-values |
| Unknown enums raise on replay | **Do not raise** for claim outcome/validity/unknown role strings — coerce to null / exclude / default; TaskReason upcasters unchanged |
| SESOI from plan params | **Catalog template only** (`uses_sesoi`, `sesoi`, bounds, `predicts_null`); `RequirementRef.params` rejected at `accept_plan` |
| `min_power` severe path | **Removed** until the core computes power |

**Assumed from PR 8:** check `role` prediction|validity|unknown; `VerdictSnapshotsRecorded`; `confirmations_needed` as snapshot `k`.

## 1. Graph attrs (no new node or edge kinds)

| Element | Attr | Type | Rule |
|---|---|---|---|
| claim | `validity` | `valid \| invalid` | from validity checks (PR 8) |
| claim | `outcome` | `consistent \| inconsistent \| indeterminate \| null` | null iff invalid |
| claim | `severe` | bool | §3; **recomputed on read** — never trust stored `True` |
| claim | `independence_key` | str | base `sha256(JCS({experiment_id}))`; count-time merge by root-data overlap / structural key |
| claim | `data_digests` | list(str) | **root** data content digests (§5) |
| claim | `criterion_template_ids` | list(str) | structural independence (§5); no goal/title/plan_id |
| claim | `effect{est,ci_low?,ci_high?,kind,trusted}` | nullable | from **CheckResult.effect** (core evaluation), never agent claim attrs |
| criterion | `sesoi`, `uses_sesoi`, `predicts_null`, `predicted_direction`, `alpha` | frozen under R17 | from **catalog template** at freeze; alpha default 0.05 |
| study | `confirmations_needed` | int | **PR8 snapshot `k`** (bind digest); not the severe-support gate |
| study | `supported_needed` | int ≥ 2 | severe-support threshold (default 2) |
| study | `aggregate` | `count` only | e-value deferred |

Legacy `met/not_met/unassessed` is derived for storage/compat; `verdict()` reads `outcome`.

## 2. Events

| Event | Version | Change | Upcaster |
|---|---|---|---|
| `StudyCreated` | v5 → **v6** | + `supported_needed` (default 2) | `2` |
| Criterion freeze | attrs | + catalog `sesoi` / `uses_sesoi` / `predicts_null` / `predicted_direction` | old: null / false / absent |
| Claim write | attrs | + validity/outcome/severe/independence_key/data_digests/numerics | old: severe=false; independence_key=sha256(experiment_id) |
| `StudyFinalized` | v3 | `hypotheses[]` via `hypothesis_report_row`; optional `hypotheses_unavailable` | old: `[]` / null |

Legacy claims are never severe → never `supported` (V5). On replay/read, **`severe` / `refute_capable` are recomputed** (or reset to False); stored `True` is never trusted.

**Snapshots vs finalize:** bind prefers persisted `VerdictSnapshotsRecorded` (no softer recomputation on replay). Finalize **recomputes** `hypothesis_report_row` / dispositions from the graph at finalize time and journals them on `StudyFinalized.hypothesis_verdicts`.

## 3. `classify` / severe

1. Validity FAILED or R17 violated → invalid, outcome null, severe false.
2. Prediction checks: any FAILED → inconsistent; all PASSED (≥1) → consistent; else indeterminate.
3. Null prediction (`predicts_null` from **catalog template**, frozen on criterion) is consistent only if **numerical TOST** holds: real CI (`ci_low < ci_high`) from the metric artifact strictly inside (−sesoi, +sesoi). **Never invent CI from a point estimate** (`ci_low = ci_high = est` is not TOST).
4. `severe` = consistent ∧ frozen ∧ catalog `uses_sesoi` ∧ check `used_sesoi` ∧ (null → **trusted** TOST; else point effect exceeds sesoi in `predicted_direction`). **No `min_power` path.**
5. Exact equality (`value_matches`) is a point check: never sets `used_sesoi`, never TOST.

### TOST trust (metric artifact producers)

| Writer | Path | `effect.trusted` | Severe / refute-capable (null) |
|---|---|---|---|
| Agent | `LocalArtifactStore.put` via `AgentTaskOutput.outputs` (Executor.record) | **false** | **no** — even with a real CI |
| Core CAS | `put_cas` for route digests / reports | n/a (not metric checks) | — |
| Sandbox / core metric (future) | not in M1 | would be **true** | yes, when CI present |

**Finding:** today every metric document evaluated by `evaluate_code_check` comes from an agent-written artifact (`artifacts.put` / agent outputs). Core therefore stamps `trusted=False`. Equivalence may be **numerically consistent** when the agent supplies a CI, but it is **not severe** and **not refute-capable** until a trusted producer exists.

## 4. Counting filter

```
counts(c) = existing_counts(c)
            and provenance == real
            and validity == valid
            and outcome in {consistent, inconsistent}
            and not literature
```

Excluded: stub, partial, invalid (`validity_failed`/`unknown_role`), indeterminate, lit, r17, superseded.
Only-excluded bags → `inconclusive` (not `untested`).

## 5. Independence sources

- Base key: `sha256(JCS({experiment_id}))` (task id; never attempt id). **Method is not in the key.**
- **Code vs data:** decided by the **core catalog**, not the planner. An input artifact is DATA unless its binding input name is listed in `Catalog.code_output_names`. **Ignore** planner `OutputSpec.role` / `media_type` for this decision.
- **Root digests:** walk `InputBinding` → producer `ResultRecorded.inputs` transitively. Roots are artifacts with no upstream producer (or a producer with no artifact inputs). Code hops (catalog names) are skipped. Two experiments are one source if their **root-data digest sets overlap** (a dataset through an intermediate step therefore merges with the raw one).
- **Structural merge (no distinct root data):** same target hypothesis/question ∧ same sorted `criterion_template_ids` ∧ both have empty root-data sets → one source. **Do not** hash whole TaskSpec; **never** use goal, title, or plan_id.

### Residual risk

- Catalog `code_output_names` mis-declaration (labeling data names as code) can hide shared roots; treat catalog edits as trusted config.
- Intermediate producers that omit `ResultRecorded.inputs` / `output_map` break the provenance walk (roots stop early) — journal shape is a core invariant.
- Structural merge collapses all no-input experiments that share the same criterion templates against one target; authors who need distinct sources without data must use distinct criterion template ids (or bind distinct root data).

## 6. `verdict(h)`

- No claims → `untested`.
- `inc` / `con` distinct by independence **source** (§5).
- `|inc| ≥ k_ref`: refuted if priority; else conflict → inconclusive if con non-empty.
- `|con| ≥ supported_needed` ∧ ≥1 **capable** refute intent → `supported`.
- `|con| ≥ 1` → `survived`.
- Else inconclusive; `validity_failed` when only invalid remain.

**Capable refute intent:** intent=refute ∧ frozen ∧ catalog `uses_sesoi` ∧ prediction-role check that `used_sesoi` ∧ (null → trusted CI). Planner label alone is not enough. Stored `refute_capable=True` is never trusted on read.

## 7. E-value — deferred

`aggregate='evalue'` rejected at StudyCreated with a clear error. DESIGN note: revisit when the core computes p-values.

## 8. Question disposition

- Without hypotheses: supported→answered, survived→survived, refuted→refuted, else inconclusive.
- With hypotheses: answered if ≥1 supported; survived if ≥1 survived and none supported; refuted if all refuted; else inconclusive.
- Implemented via `disposition_for_question` in dispositions/finalize.

## 9. Invariants

- **V1** Deterministic; bind replay equals persisted `VerdictSnapshotsRecorded`.
- **V2** Removing stub/partial/invalid/lit changes no scientific verdict.
- **V3** supported ⇒ ≥ supported_needed distinct core sources, all severe, ≥1 capable refute.
- **V4** refuted ⇒ ≥ k_ref distinct counted inconsistent; validity alone never refutes.
- **V5** Legacy claims never give supported (absent/`severe=False` on read; forged `severe=True` recomputed).

## 10. Report `hypotheses[]`

Built by `hypothesis_report_row` / `hypothesis_row` in finalize → `minimal_report.hypotheses` and `StudyFinalized.hypothesis_verdicts`.

If `report_hypothesis_verdicts` fails, the report records a dedicated **`hypotheses_unavailable`** field (exception class name) with empty `hypotheses[]` — **not** a fake hypothesis id string.

PR8 priority-refuted note wording restored: `refuted; n supporting results were stub/partial`.

## 11. Tests

Unit + property (`record_claim` V1–V5) + executor e2e:

- Independence bypasses: planner **role swap**, **goal/title reword** (plan revision), **intermediate step** provenance.
- Root-data overlap merge; structural merge (empty roots + same templates); method ignored.
- TOST: real CI required; agent-written metrics → `trusted=false` → not severe / not refute-capable.
- `value_matches` never sets `used_sesoi`.
- Forged old journal `severe=True` / `refute_capable=True` recomputed on replay.
- No-sesoi never supported; one severe → survived on defaults; snapshot replay; evalue rejected; planner sesoi params rejected.
