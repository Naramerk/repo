# DESIGN_HYP_DELTA — hypothesis verdict model (M1 PR 8b)

Base: DESIGN_M1.md with PR 1–8 merged. Rationale: [`HYPOTHESIS_LIT.md`](HYPOTHESIS_LIT.md).
Scope: claim classification, the hypothesis verdict, question disposition mapping, and the report payload. No change to the task/attempt lifecycle.

## Reconciliation with merged PR 8 (code wins on mechanics)

| Written SPEC | Merged code / this PR |
|---|---|
| Named PR 20; projection-only / no new events | **PR 8b**; **keep `VerdictSnapshotsRecorded`** |
| StudyCreated v3→v4 for study fields | **v5→v6** for `supported_needed` (`confirmations_needed` took v4; aggregate took v5) |
| Separate `min_independent_support` | Named **`supported_needed`** (default 2, ≥2); **do not** reuse `confirmations_needed` |
| validity failure caps the question | **Per-experiment** `excluded_claims` / `validity_failed`; keep **`unknown_role`** |
| Event names `ClaimRecorded` / `CriterionFrozen` | Real journal: claim/criterion attrs on **`GraphNodeUpserted`** |
| E-value aggregation in M1 | **Deferred** — `aggregate='evalue'` rejected at StudyCreated until core computes p-values |
| Unknown enums raise on replay | **Do not raise** for claim outcome/validity unknowns (coerce / exclude); TaskReason upcasters unchanged |

**Assumed from PR 8:** check `role` prediction|validity|unknown; `VerdictSnapshotsRecorded`; `confirmations_needed` as snapshot `k`.

## 1. Graph attrs (no new node or edge kinds)

| Element | Attr | Type | Rule |
|---|---|---|---|
| claim | `validity` | `valid \| invalid` | from validity checks (PR 8) |
| claim | `outcome` | `consistent \| inconsistent \| indeterminate \| null` | null iff invalid |
| claim | `severe` | bool | §3; **false** unless sesoi or min_power>0 on frozen criterion |
| claim | `independence_key` | str | `sha256(JCS({dataset_digest, method, code_digest}))` from **InputBinding digests** + method; fallback `experiment_id` (task id, never attempt id) |
| claim | `p_value`, `effect{est,ci_low,ci_high}`, `power` | nullable | effect from **check results**, never agent raw |
| criterion | `sesoi`, `min_power`, `alpha`, `predicts_null` | frozen under R17 | from plan `RequirementRef.params` at freeze; alpha default 0.05 |
| study | `confirmations_needed` | int | **PR8 snapshot `k`** (bind digest); not the severe-support gate |
| study | `supported_needed` | int ≥ 2 | severe-support threshold (default 2) |
| study | `aggregate` | `count` only | e-value deferred |

Legacy `met/not_met/unassessed` is derived for storage/compat; `verdict()` reads `outcome`.

## 2. Events

| Event | Version | Change | Upcaster |
|---|---|---|---|
| `StudyCreated` | v5 → **v6** | + `supported_needed` (default 2) | `2` |
| Criterion freeze | attrs | + `sesoi`, `min_power`, `alpha`, `predicts_null` | old: null / null / 0.05 / absent |
| Claim write | attrs | + validity/outcome/severe/independence_key/numerics | old: severe=false; independence_key=sha256(experiment_id); numerics null |
| `StudyFinalized` | v3 | `hypotheses[]` via `hypothesis_report_row` | old: `[]` |

Legacy claims are never severe → never `supported` (V5). Persisted `VerdictSnapshotsRecorded` win on replay (no softer recomputation).

## 3. `classify` / severe

1. Validity FAILED or R17 violated → invalid, outcome null, severe false.
2. Prediction checks: any FAILED → inconsistent; all PASSED (≥1) → consistent; else indeterminate.
3. Null prediction (`predicts_null` from **frozen criterion**) is consistent only if TOST holds with **core-computed** effect and criterion `sesoi`.
4. `severe` = consistent ∧ frozen ∧ (sesoi set ∨ min_power>0) ∧ (min_power null ∨ power≥min_power). **No transitional min_power=0 stamp.**

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

## 5. `verdict(h)`

- No claims → `untested`.
- `inc` / `con` distinct by **core** `independence_key`.
- `|inc| ≥ k_ref`: refuted if priority; else conflict → inconclusive if con non-empty.
- `|con| ≥ supported_needed` ∧ ≥1 **capable** refute intent → `supported`.
- `|con| ≥ 1` → `survived`.
- Else inconclusive; `validity_failed` when only invalid remain.

**Capable refute intent:** intent=refute ∧ frozen ∧ (sesoi|min_power>0) ∧ prediction-role check. Planner label alone is not enough.

## 6. E-value — deferred

`aggregate='evalue'` rejected at StudyCreated with a clear error. DESIGN note: revisit when the core computes p-values.

## 7. Question disposition

- Without hypotheses: supported→answered, survived→survived, refuted→refuted, else inconclusive.
- With hypotheses: answered if ≥1 supported; survived if ≥1 survived and none supported; refuted if all refuted; else inconclusive.
- Implemented via `disposition_for_question` in dispositions/finalize.

## 8. Invariants

- **V1** Deterministic; replay equals persisted `VerdictSnapshotsRecorded`.
- **V2** Removing stub/partial/invalid/lit changes no scientific verdict.
- **V3** supported ⇒ ≥ supported_needed distinct core keys, all severe, ≥1 capable refute.
- **V4** refuted ⇒ ≥ k_ref distinct counted inconsistent; validity alone never refutes.
- **V5** Legacy claims never give supported.

## 9. Report `hypotheses[]`

Built by `hypothesis_report_row` / `hypothesis_row` in finalize → `minimal_report.hypotheses` and `StudyFinalized.hypothesis_verdicts`.

PR8 priority-refuted note wording restored: `refuted; n supporting results were stub/partial`.

## 10. Tests

Unit + property (`record_claim` V1–V5) + executor e2e: independence_key from bindings, no-sesoi never supported, one severe → survived on defaults, snapshot replay, evalue rejected.
