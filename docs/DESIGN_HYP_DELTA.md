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
| claim | `independence_key` | str | base `sha256(JCS({experiment_id}))`; count-time merge by root-data overlap / shared templates |
| claim | `data_digests` | list(str) | **root** data content digests (§5) |
| claim | `criterion_template_ids` | list(str) | structural independence (§5); no goal/title/plan_id |
| claim | `effect{est,ci_low?,ci_high?,kind,producer,trusted}` | nullable | from **CheckResult.effect** (core evaluation), never agent claim attrs |
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
| `ResultRecorded` | v2 | + `output_kinds` (name → code\|data from plan OutputSpec) | old: absent → DATA / legacy meta |
| `StudyFinalized` | v3 | `hypotheses[]` via `hypothesis_report_row`; optional `hypotheses_unavailable` | old: `[]` / null |

Legacy claims are never severe → never `supported` (V5). On replay/read, **`severe` / `refute_capable` / `effect.trusted` are recomputed** (or reset to False); stored `True` is never trusted. `effect.trusted` is recomputed from core-stamped `effect.producer` (`core`\|`sandbox` → true; `agent` → false).

**Snapshots vs finalize:** bind prefers persisted `VerdictSnapshotsRecorded` (no softer recomputation on replay). Finalize **recomputes** `hypothesis_report_row` / dispositions from the graph at finalize time and journals them on `StudyFinalized.hypothesis_verdicts`.

## 3. `classify` / severe

1. Validity FAILED or R17 violated → invalid, outcome null, severe false.
2. Prediction checks: any FAILED → inconsistent **only if** the metric evidence is trusted (`effect.producer` ∈ {core, sandbox}); untrusted fail → **indeterminate** (never a counting refute). All PASSED (≥1) → consistent; else indeterminate.
3. Null prediction (`predicts_null` from **catalog template**, frozen on criterion) is consistent only if **numerical TOST** holds: real CI (`ci_low < ci_high`) from the metric artifact strictly inside (−sesoi, +sesoi). **Never invent CI from a point estimate** (`ci_low = ci_high = est` is not TOST).
4. `severe` = consistent ∧ frozen ∧ catalog `uses_sesoi` ∧ check `used_sesoi` ∧ **trusted metric producer** ∧ (null → TOST interval; else point exceeds sesoi in `predicted_direction`). **No `min_power` path.** Untrusted effects are never severe and never refute-capable (null or non-null).
5. Exact equality (`value_matches`) is a point check: never sets `used_sesoi`, never TOST.

### Metric trust (symmetric)

| Writer | Path | `Artifact.producer` | `effect.trusted` | Severe / refute / k_ref |
|---|---|---|---|---|
| Agent | `LocalArtifactStore.put` via `AgentTaskOutput.outputs` | **agent** | **false** | **no** |
| Core | `put_core` (CAS / core-owned writes) | **core** | **true** | yes |
| Sandbox | `put_sandbox` (execution of catalog/check code) | **sandbox** | **true** | yes |

The agent cannot set `trusted` or `producer`. Core stamps both from the write path. The **same** trust rule applies to both directions: a claim counts toward `refuted` (priority / `k_ref`) only if its evidence is trusted, exactly like severe / `supported`. An untrusted failing metric is indeterminate / excluded, never a refutation.

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
- **Code vs data:** decided from core-owned **`ResultRecorded.output_kinds`**, filled from the producing task's plan **`OutputSpec`** (`role` preferred, else OutputSpec `media_type`). Agent-supplied `Artifact.media_type` is **ignored** for this decision. No OutputSpec entry → **DATA**. Legacy journals without `output_kinds` fall back to artifact meta. **Never** use `InputBinding.input` / slot names.
- **Root digests:** DFS walk `InputBinding` → producer `ResultRecorded.inputs` (depth guard; `on_stack` vs `done` so diamonds are not cycles; `get_meta` / producer-index failures → conservative notes, never raise). Roots are artifacts with no upstream producer, **or** whose upstream inputs are all code (generation/simulation — the intermediate is itself a root). Two experiments are one source if their **root-data digest sets overlap**.
- **Structural merge (no distinct root data):** same target ∧ **any shared** criterion template id ∧ both empty root-data sets → one source. Exact template-set equality is rejected (adding an auxiliary template must not mint a new source). **Do not** hash whole TaskSpec; **never** use goal, title, or plan_id.
- **Catalog defaults:** when `TaskSpec.criteria` is empty, both check evaluation and `criterion_template_ids` use `catalog.defaults["experiment"]` (same template id list). There is no separate “primary template.”

### Residual risk

- Intermediate producers that omit `ResultRecorded.inputs` / `output_map` break the provenance walk — journal shape is a core invariant.
- Empty-root experiments that share **no** criterion template remain distinct sources (bind distinct root data to force a merge across template families).

## 6. `verdict(h)`

- No claims → `untested`.
- `inc` / `con` distinct by independence **source** (§5).
- `|inc| ≥ k_ref` (**trusted** inconsistent only): refuted if priority; else conflict → inconclusive if con non-empty.
- `|con| ≥ supported_needed` ∧ ≥1 **capable** refute intent → `supported`.
- `|con| ≥ 1` → `survived`.
- Else inconclusive; `validity_failed` when only invalid remain.

**Capable refute intent:** intent=refute ∧ frozen ∧ catalog `uses_sesoi` ∧ prediction-role check that `used_sesoi` ∧ **trusted** metric producer (null → trusted CI). Planner label alone is not enough. Stored `refute_capable=True` is never trusted on read.

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
- **V4** refuted ⇒ ≥ k_ref distinct counted **trusted** inconsistent; validity alone never refutes; untrusted fail never refutes.
- **V5** Legacy claims never give supported (absent/`severe=False` on read; forged `severe=True` recomputed).

## 10. Report `hypotheses[]`

Built by `hypothesis_report_row` / `hypothesis_row` in finalize → `minimal_report.hypotheses` and `StudyFinalized.hypothesis_verdicts`.

If `report_hypothesis_verdicts` fails, the report records a dedicated **`hypotheses_unavailable`** field (exception class name) with empty `hypotheses[]` — **not** a fake hypothesis id string.

PR8 priority-refuted note wording restored: `refuted; n supporting results were stub/partial`.

## 11. Tests

Unit + property (`record_claim` V1–V5) + executor e2e:

- Independence: **slot-rename** (bind data as `script`), OutputSpec kinds, goal reword, intermediate step, all-code-input intermediate as root, **media_type spoof** (`text/x-python` on a data OutputSpec stays a root), **diamond** provenance (not a cycle).
- Structural: shared-template merge (extra template does not split); empty criteria → catalog defaults in `criterion_template_ids`.
- TOST / non-null: real CI; untrusted agent metrics → not severe / not refute-capable / not k_ref.
- Sandbox / core metrics: can reach `supported` (`supported_needed` ≥ 2 independent) and `refuted`.
- `value_matches` never sets `used_sesoi`.
- Forged old journal `severe=True` / `refute_capable=True` / `effect.trusted=True` recomputed on replay from producer.
- Provenance walk: no crash on `get_meta` failure / cycles; `_producer_index` safe.
