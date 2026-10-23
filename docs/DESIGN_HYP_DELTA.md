# DESIGN_HYP_DELTA — hypothesis verdict model (PR 20, after PR 8)

Base: DESIGN_M1.md (rev 5) with PR 1–8 merged. Rationale: HYPOTHESIS_LIT.md. Scope: claim classification, the hypothesis verdict, question disposition mapping, and the report payload. No change to the task/attempt lifecycle.

**Assumed from PR 8** (this spec relies on them and does not redefine them):
- `check.role: prediction | validity`;
- failed prediction → criterion `not_met` (refute evidence);
- failed validity → claim `invalid`, which caps the verdict at inconclusive;
- the stub note in the report.

**Assumed from PR 6/7:**
- `tests.intent`;
- `Study.refutations_needed` (k_ref) and `Study.refutation_priority`;
- the `survived` verdict, R17 and `uses_claim`.

Event and attr names that RM v0.4 owns (`ClaimRecorded`, `CriterionFrozen`, `counts()`) are referred to by their role. The implementing agent must confirm the exact names against `graph_schema.yaml` and `core/events.py` before coding.

## 1. Graph schema deltas (`graph_schema.yaml`)

No new node kinds and no new edge kinds. The count stays at 19 edge kinds.

| Element | Attr | Type | Default / rule |
|---|---|---|---|
| node `claim` | `validity` | `valid \| invalid` | computed from validity checks (PR 8); stored for replay |
| node `claim` | `outcome` | `consistent \| inconsistent \| indeterminate \| null` | null iff validity=invalid; computed (§3.1) |
| node `claim` | `severe` | bool | computed (§3.1) |
| node `claim` | `independence_key` | str | sha256(JCS({dataset_digest, method, code_digest})) of the producing experiment; computed |
| node `criterion` | `sesoi` | float \| null | smallest effect size of interest, same unit as `metric`; frozen with criterion (R17) |
| node `criterion` | `min_power` | float \| null | e.g. 0.8; frozen with criterion |
| node `criterion` | `alpha` | float | default 0.05; frozen |
| node `hypothesis` | `verdict` | `supported \| survived \| refuted \| inconclusive \| untested` | projection-only (not written as an attr event) |
| edge `tests` (experiment→hypothesis) | `intent` | `confirm \| refute` | exists (PR 6); unchanged |
| node `claim` | `stance` | computed (RM §2) | unchanged; derived: consistent→supports, inconsistent→refutes, else none |

Removed semantics: `met/not_met/unassessed` is no longer read by `verdict()`. It is still stored for backward compatibility and derived as:
- `met ⇔ outcome=consistent`
- `not_met ⇔ outcome=inconsistent`
- `unassessed ⇔ outcome ∈ {indeterminate, null}`

Study (frozen at `StudyCreated`, as with k_ref):
- `Study.min_independent_support: int = 2`
- `Study.aggregate: "count" | "evalue"`, default `"count"`
- `Study.evalue_alpha: float = 0.05`

## 2. Journal events

| Event | Version | Change | Upcaster |
|---|---|---|---|
| `StudyCreated` | v3 → v4 | + `min_independent_support`, `aggregate`, `evalue_alpha` | v3→v4: `2`, `"count"`, `0.05` |
| `CriterionFrozen` (RM name) | vN → vN+1 | + `sesoi`, `min_power`, `alpha` | old: `sesoi=null, min_power=null, alpha=0.05` |
| `ClaimRecorded` (RM name) | vN → vN+1 | + `validity`, `outcome`, `severe`, `independence_key`, `p_value: float\|null`, `effect: {est, ci_low, ci_high}\|null`, `power: float\|null` | old: `validity` from PR 8 checks (if absent → `valid`); `outcome` from status (`met→consistent`, `not_met→inconsistent`, `unassessed→indeterminate`); `severe=false`; `independence_key=sha256(experiment_id)`; numerics `null` |
| `StudyFinalized` | v2 → v3 | `minimal_report.questions[]` + `hypotheses[]` (§5); `question_dispositions` unchanged in shape | old: `hypotheses=[]` |

Rules:
- Upcasters live in `core/upcasters.py` and are pure.
- Unknown enum values raise `ValueError` on replay.
- Upcasted old claims are never `severe`, so an old study cannot be upgraded to `supported` by replay. It can only stay at `survived` or below, and the report says `legacy_claims: n`.
- No new event types. Verdicts stay projection-computed: the same pure function serves `StudyView`, `StudyFinalized` and replay (test in §6).

## 3. Algorithm

### 3.1 Claim classification (at claim write time, deterministic)

```
classify(claim, criterion, checks):
  if any(c.role == validity and c.status == FAILED for c in checks) or violates_R17(claim):
      return validity=invalid, outcome=null, severe=false
  pred = [c for c in checks if c.role == prediction]
  if any(c.status == FAILED for c in pred):            # PR 8 semantics
      outcome = inconsistent
  elif all(c.status == PASSED for c in pred) and pred:
      outcome = consistent
  else:
      outcome = indeterminate
  # Equivalence/null claims: "no effect" is consistent only if TOST holds
  if claim.predicts_null and outcome == consistent:
      if criterion.sesoi is null or claim.effect is null
         or not (-sesoi < effect.ci_low and effect.ci_high < sesoi):
          outcome = indeterminate
  severe = outcome == consistent
           and criterion.frozen_at_seq < first_evidence_seq(claim)   # R17
           and (criterion.min_power is null or (claim.power or 0) >= criterion.min_power)
           and (criterion.sesoi is not null or criterion.min_power is not null)
  return valid, outcome, severe
```

### 3.2 Counting filter (extends RM §4.1 `counts`)

```
counts(c) = RM_counts(c)                     # accepted, not superseded (§11), etc.
            and c.provenance == real          # stub/partial excluded BEFORE counting
            and c.validity == valid
            and c.outcome in {consistent, inconsistent}
            and not c.is_lit                  # literature sets thresholds/priors only
excluded(c) = reason in {stub, partial, invalid, indeterminate, lit, r17, superseded}
```
`partial` is excluded from counts but listed. This is stricter than the old ceiling and simpler: there is a single filter and no ceiling.

### 3.3 Hypothesis verdict

```
verdict(h, study):
  C   = [c for c in claims_about(h) if counts(c)]
  if not claims_about(h): return untested
  inc = distinct_by(independence_key, [c for c in C if c.outcome == inconsistent])
  con = distinct_by(independence_key, [c for c in C if c.outcome == consistent and c.severe])
  if study.aggregate == "evalue":
      return evalue_verdict(h, C, study)        # §3.4
  if len(inc) >= study.k_ref and study.refutation_priority:
      return refuted                            # conflict listed if con non-empty
  if len(inc) >= study.k_ref and not study.refutation_priority:
      return refuted if not con else inconclusive
  if inc:                                       # 0 < inc < k_ref
      return inconclusive
  if len(con) >= study.min_independent_support
     and any(intent(c) == refute for c in con):
      return supported
  if len(con) >= 1:
      return survived
  return inconclusive
```
`intent(c)` is the `tests.intent` of the edge (experiment(c) → h). Duplicate runs with the same `independence_key` count once.

### 3.4 Optional e-value aggregation (POPPER, arXiv:2502.09858)

```
evalue_verdict(h, C, study):
  # H0 = "h's implication is false" for confirm intent; "h holds" for refute intent
  E_support = Π calibrate(c.p_value) for c in C if c.p_value is not null and supports_h(c)
  E_refute  = Π calibrate(c.p_value) for c in C if c.p_value is not null and refutes_h(c)
  calibrate(p) = κ·p^(κ−1), κ = 0.5            # Vovk & Wang 2021 p-to-e calibrator
  if E_refute  >= 1/alpha: return refuted
  if E_support >= 1/alpha and distinct(independence_key) >= 2: return supported
  return survived if any consistent severe else inconclusive
```
Product validity assumes independent tests (sequential e-process). Claims sharing an `independence_key` contribute only the max e-value.

### 3.5 Disposition mapping (replaces DESIGN_M1 §2 rule 1)

- Question without hypotheses: supported → answered, survived → survived, refuted → refuted, otherwise inconclusive.
- Question with hypotheses: answered if ≥1 h is supported; survived if ≥1 h survived and none is supported; refuted if all h are refuted; otherwise inconclusive.
- `based_on` (DESIGN_M1 §1) = all counted claims of the deciding hypotheses.
- Premises (§2.1): a premise is refuted ⇒ the question is ill_posed (unchanged).
- Loop predicate `survivors_le` (§4) counts h with verdict ∉ {refuted} (unchanged).

## 4. Invariants

- V1: `verdict` is a pure function of the projection. Replay gives an identical result.
- V2: a stub, partial, invalid or lit claim never changes a verdict. Removing it from the graph leaves every verdict unchanged.
- V3: `supported ⇒ ∃ ≥ min_independent_support distinct keys, all severe, and ≥1 with intent=refute`.
- V4: `refuted ⇒ ≥ k_ref distinct counted inconsistent claims`. A validity failure alone never yields refuted.
- V5: legacy (upcasted) claims never give `supported`.

## 5. Report payload (main report + `minimal_report`)

`hypotheses[]` items. Schema, JCS-serialized; they count toward the 10 KB limit, with truncation order after `questions`:
```json
{"hypothesis_id": "...", "question_id": "...",
 "verdict": "supported|survived|refuted|inconclusive|untested",
 "rule": "k_ref|support_min|single_severe|inc_below_k_ref|no_counted|evalue",
 "counted": [{"claim_id": "...", "outcome": "...", "intent": "...", "severe": true, "independence_key": "..."}],
 "excluded": [{"claim_id": "...", "reason": "stub|partial|invalid|indeterminate|lit|r17|superseded"}],
 "conflict": {"supporting": ["claim_id"], "refuting": ["claim_id"]} | null,
 "severity_note": "string | null",
 "auxiliary_assumptions": ["validity check names that passed"],
 "legacy_claims": 0,
 "e_values": {"support": 1.0, "refute": 1.0} | null}
```
Deterministic text the renderer must emit:
- for `survived` and `supported`: "survived ≠ proven; tests that could have refuted: …";
- for `refuted` with confirmations: "refuted despite n supporting claims (conflict)";
- when `excluded` is non-empty: "n claims excluded (stub a, invalid b, …)", which merges with the PR 8 stub note;
- for every `indeterminate`: "underpowered or no SESOI".

`sections.real/stub/human` are unchanged.

## 6. Tests

`test_verdict_untested_no_claims`, `test_validity_failure_never_refutes`, `test_stub_partial_excluded_before_counting` (V2 property), `test_lit_not_counted`, `test_r17_violation_invalid`, `test_null_claim_requires_tost`, `test_underpowered_is_indeterminate`, `test_severe_requires_frozen_sesoi_or_power`, `test_duplicate_runs_count_once`, `test_refuted_k_ref_1`, `test_refuted_k_ref_2_needs_independent`, `test_inc_below_k_ref_inconclusive`, `test_refutation_priority_false_conflict_inconclusive`, `test_refuted_with_confirmations_reports_conflict`, `test_survived_single_severe`, `test_supported_needs_two_independent_one_refute_intent`, `test_disposition_mapping_with_hypotheses`, `test_evalue_product_threshold`, `test_evalue_shared_key_max_only`, `test_upcaster_claim_status_to_outcome`, `test_upcaster_studycreated_v4_defaults`, `test_legacy_claims_never_supported` (V5), `test_verdict_replay_equals_finalized` (V1), `test_report_hypotheses_payload_schema`, `test_minimal_report_hypotheses_truncation_le_10kb`. Add rule `record_claim(validity, outcome, provenance, key)` to the property test (§13) with invariants V1–V5. Update `refmodel.verdict`.

## 7. PR placement

PR 20: depends on 6, 7, 8 (and 11, if `survivors_le` is used). It touches `core/verdict.py`, `core/upcasters.py`, `graph_schema.yaml`, `core/report.py`, and refmodel. Change to DESIGN_M1:
- §3: drop "provenance ceiling" wording in favour of the §3.2 filter;
- §2 rule 1: replace with §3.5;
- §14: add the PR 20 row.
