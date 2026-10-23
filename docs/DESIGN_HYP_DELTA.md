# DESIGN_HYP_DELTA — hypothesis verdict model (M1 PR 8b)

Base: DESIGN_M1.md with PR 1–8 merged. Rationale: [`HYPOTHESIS_LIT.md`](HYPOTHESIS_LIT.md).
Scope: claim classification, the hypothesis verdict, question disposition mapping, and the report payload. No change to the task/attempt lifecycle.

## Reconciliation with merged PR 8 (code wins on mechanics)

| Written SPEC | Merged code / this PR |
|---|---|
| Named PR 20; projection-only / no new events | **PR 8b**; **keep `VerdictSnapshotsRecorded`** |
| StudyCreated v3→v4 for study fields | **v4→v5** (`confirmations_needed` already took v4) |
| Separate `min_independent_support` | **Reuse `confirmations_needed`** as the support threshold (do not duplicate) |
| validity failure caps the question | **Per-experiment** `excluded_claims` / `validity_failed`; `inconclusive(validity_failed)` only when nothing valid remains; keep **`unknown_role`** |
| Event names `ClaimRecorded` / `CriterionFrozen` | Real journal: claim/criterion attrs on **`GraphNodeUpserted`** (and freeze attrs when the freeze path writes them) |
| PR 8 stub note dropped | **Kept** and merged with excluded stub/partial confirmation wording |

**Assumed from PR 8** (not redefined): check `role` prediction|validity|unknown; failed prediction → refute evidence; validity / unknown exclusions; stub note.

**Assumed from PR 6/7:** `tests.intent`; `refutations_needed` / `refutation_priority`; `survived`; R17; `uses_claim`.

## 1. Graph attrs (no new node or edge kinds)

| Element | Attr | Type | Rule |
|---|---|---|---|
| claim | `validity` | `valid \| invalid` | from validity checks (PR 8); stored for replay |
| claim | `outcome` | `consistent \| inconsistent \| indeterminate \| null` | null iff invalid |
| claim | `severe` | bool | §3 classify |
| claim | `independence_key` | str | `sha256(JCS({dataset_digest, method, code_digest}))` of producing experiment |
| claim | `p_value`, `effect{est,ci_low,ci_high}`, `power` | nullable | optional numerics |
| criterion | `sesoi`, `min_power`, `alpha` | float\|null / float\|null / float | frozen under R17; alpha default 0.05 |
| study | `confirmations_needed` | int | **support threshold** (was also legacy k); profile default may be 2 |
| study | `aggregate` | `count \| evalue` | default `count` |
| study | `evalue_alpha` | float | default 0.05 |
| hypothesis | `verdict` | enum | projection (+ persisted via `VerdictSnapshotsRecorded`) |

Legacy `met/not_met/unassessed` is **derived** for storage/compat only; `verdict()` does **not** read it:
- met ⇔ outcome=consistent
- not_met ⇔ outcome=inconsistent
- unassessed ⇔ outcome ∈ {indeterminate, null}

## 2. Events

| Event | Version | Change | Upcaster |
|---|---|---|---|
| `StudyCreated` | v4 → **v5** | + `aggregate`, `evalue_alpha` (confirmations_needed already on v4) | `count`, `0.05` |
| Criterion freeze write | attrs | + `sesoi`, `min_power`, `alpha` | old: null / null / 0.05 |
| Claim write (`GraphNodeUpserted`) | attrs | + `validity`, `outcome`, `severe`, `independence_key`, numerics | old: validity from PR8 checks (absent→valid); outcome from status; severe=false; independence_key=sha256(experiment_id); numerics null |
| `StudyFinalized` | v2 → **v3** | + `hypotheses[]` / minimal_report.hypotheses | old: `[]` |

Unknown enum values raise on replay. Legacy (upcasted) claims are never `severe` → never `supported` (V5).

## 3. `classify(claim, criterion, checks)`

1. If a validity check FAILED or R17 is violated → `validity=invalid`, `outcome=null`, `severe=false`.
2. Over prediction checks: any FAILED → `inconsistent`; all PASSED and ≥1 → `consistent`; else `indeterminate`.
3. Null / “no effect” prediction is consistent only if TOST holds: `sesoi` set, `effect` set, and `-sesoi < ci_low` and `ci_high < sesoi`; else `indeterminate`.
4. `severe` = consistent ∧ criterion frozen before first evidence ∧ (`min_power` is null ∨ power ≥ min_power) ∧ (`sesoi` or `min_power` is set).

## 4. Counting filter

```
counts(c) = existing_counts(c)
            and provenance == real
            and validity == valid
            and outcome in {consistent, inconsistent}
            and not literature
```

Excluded reasons: `stub`, `partial`, `invalid` (keep `validity_failed` and `unknown_role`), `indeterminate`, `lit`, `r17`, `superseded`.

## 5. `verdict(h)`

- No claims at all → `untested`.
- `inc` = inconsistent counted, distinct by `independence_key`; `con` = consistent ∧ severe counted, distinct by key.
- `aggregate=evalue` → §6.
- `|inc| ≥ k_ref`: `refuted` if `refutation_priority`; else `refuted` if `con` empty, otherwise `inconclusive`.
- `0 < |inc| < k_ref` → `inconclusive`.
- `|con| ≥ confirmations_needed` ∧ ≥1 con from `tests.intent=refute` → `supported`.
- `|con| ≥ 1` → `survived`.
- Else `inconclusive`; and `inconclusive(validity_failed)` if only invalid claims exist (PR 8).

## 6. E-value mode (POPPER)

- `calibrate(p) = κ·p^(κ−1)`, κ=0.5 (Vovk & Wang 2021).
- Product over supporting / refuting claims; shared `independence_key` → max only.
- `E_refute ≥ 1/α` → `refuted`. `E_support ≥ 1/α` and ≥2 distinct keys → `supported`. Else `survived` if any consistent severe, else `inconclusive`.

## 7. Question disposition

- Without hypotheses: supported→answered, survived→survived, refuted→refuted, else inconclusive.
- With hypotheses: answered if ≥1 h supported; survived if ≥1 survived and none supported; refuted if all refuted; else inconclusive.
- `based_on` = counted claims of deciding hypotheses.
- Refuted premise → ill_posed (unchanged). `survivors_le` counts h with verdict ≠ refuted (unchanged).

## 8. Invariants

- **V1** Deterministic; replay equals persisted snapshot.
- **V2** Removing stub/partial/invalid/lit changes no verdict.
- **V3** supported ⇒ ≥ confirmations_needed distinct severe keys, ≥1 intent=refute.
- **V4** refuted ⇒ ≥ k_ref distinct counted inconsistent; validity failure alone never refutes.
- **V5** Legacy claims never give supported.

## 9. Report `hypotheses[]`

Count toward the 10 KB `minimal_report` limit; truncate after `questions`:

```json
{"hypothesis_id","question_id","verdict",
 "rule":"k_ref|support_min|single_severe|inc_below_k_ref|no_counted|evalue|validity_failed",
 "counted":[{"claim_id","outcome","intent","severe","independence_key"}],
 "excluded":[{"claim_id","reason"}],
 "conflict":{"supporting":[],"refuting":[]}|null,
 "severity_note","auxiliary_assumptions","legacy_claims","e_values"|null}
```

Renderer fixed text:

- survived/supported: `survived ≠ proven; tests that could have refuted: …`
- refuted with confirmations: `refuted despite n supporting claims (conflict)`
- excluded non-empty: `n claims excluded (stub a, invalid b, …)` — merge with PR 8 stub note
- indeterminate: `underpowered or no SESOI`

## 10. Tests

`untested_no_claims`, `validity_failure_never_refutes`, `stub_partial_excluded_before_counting` (V2), `lit_not_counted`, `r17_violation_invalid`, `null_claim_requires_tost`, `underpowered_is_indeterminate`, `severe_requires_frozen_sesoi_or_power`, `duplicate_runs_count_once`, `refuted_k_ref_1`, `refuted_k_ref_2_needs_independent`, `inc_below_k_ref_inconclusive`, `refutation_priority_false_conflict_inconclusive`, `refuted_with_confirmations_reports_conflict`, `survived_single_severe`, `supported_needs_two_independent_one_refute_intent`, `disposition_mapping_with_hypotheses`, `evalue_product_threshold`, `evalue_shared_key_max_only`, upcasters, `legacy_claims_never_supported`, `verdict_replay_equals_finalized`, `report_hypotheses_payload_schema`, `minimal_report_hypotheses_truncation_le_10kb`. Add `record_claim` to the property test with V1–V5. Update `refmodel.verdict`.

## 11. DESIGN_M1 edits

- §3: replace provenance-ceiling wording with the §4 counting filter.
- §2 rule 1: replace with §7 disposition mapping.
- §14: PR **8b** row (this document).
