# Research-cycle core — PR stack

**Title note:** CAPABILITIES #2 (hypotheses). CAPABILITIES milestone **M4**; “M3 research-cycle” = ownership slice only.

Base: `main` @ `d649a73` (+ **D-rm #76** @ `ba9bc3c`). **≤1 open PR / agent**, ≤400 LOC/PR, **stack ≤2**, no shims; tests every PR. Never merge. Contracts before code.

**Canon:** DESIGN_HYP_DELTA on main — sole `coscientist.core.claims.verdict.hypothesis_verdict`. No second path under `hypotheses/` / refmodel oracle (#76: adapter `verdict=hypothesis_verdict`).

## Merge order (one PR at a time after stack≤2)

1. **#76** (D-rm) → `main`
2. **#51** (RC1 + D3 + D17 docs) → #76
3. **#67** (G4) → `main` (after D3 on main)
4. Then **one new PR per row below**, in ID order, each waiting for its Depends to merge (no mega-stack):
   RC3t → RC17a → RC4 → RC5 → RC6 → RC7r → RC8 → RC9c → RC10c → RC11c → RC17 → RC20 → (M4) RC12 → RC13 → RC14

Owner = GitHub issue (or open PR) that lands the change. Known now: **#51** / **#76** / **#67**; future rows → tracking issues below.

| ID | Title | Scope | Depends | Owner | DoD (file::test + green CI) |
|---|---|---|---|---|---|
| **RC1** | docs: research-cycle plan | This docs PR. | `main` | **#51** | CHANGELOG + plan review; CI green on #51 |
| **D-rm** | RM §4.1 → HYP_DELTA | Hard dep of RC8. | `main` | **#76** | RM table = code order; CI green on #76 |
| **D3** | DESIGN Need + `require_hypotheses` | §2 contracts; upcasters must not invent Need. | `main` | **#51** | DESIGN text; CI green on #51 |
| **D17** | DESIGN `max_attempts` | `Study.max_attempts` from profile; RC3t target (no default). | `main` | **#51** | DESIGN text; CI green on #51 |
| **G4** | domain Need + Plan hyps/lit | Implement D3; omit → `PlanNeedRefuse`. | D3 on main | **#67** | `tests/test_g4_plan_need.py` (parametrized Need cases); CI green on #67 |
| **RC3t** | study required; drop threshold defaults | See §RC3t + §Follow-ups. | #76, D3 | **#98** | `tests/test_rc3t_study_thresholds.py::test_missing_threshold_refuses`; CI green |
| **RC17a** | wire `Study.max_attempts` | profile→Study; missing → `max_attempts_required`. | D17, RC3t | **#99** | `tests/test_rc17a_max_attempts.py::test_missing_max_attempts_refuses`; CI green |
| **RC4** | HYP-n when `require_hypotheses` | **Decision:** if `Study.require_hypotheses` then plan **must** include ≥1 HYP-n (forced); if false, planner `Need` alone decides. | G4 | **#100** | `tests/test_rc4_hyp_n.py::test_require_hypotheses_forces_hyp_n`; CI green |
| **RC5** | hypotheses artifact validate | ≥3 hyps; one selected. | G4 | **#101** | `tests/test_rc5_hypotheses_artifact.py::test_rejects_lt_three_or_no_selected`; CI green |
| **RC6** | selected hyp referenced | ≥1 task refs selected id. | RC5 | **#102** | `tests/test_rc6_selected_ref.py::test_plan_requires_selected_hyp_ref`; CI green |
| **RC7r** | HITL reject → no PlanAccepted | Core rule only (reject ⇒ no `PlanAccepted`). | G4 | **#103** | `tests/test_rc7r_hitl_reject.py::test_reject_writes_no_plan_accepted`; CI green |
| **RC8** | question-path verdict | `needed=false`; **only** `hypothesis_verdict`; matrix below. | G4, RC3t, #76 | **#104** | `tests/test_rc8_question_verdict.py` — one test per matrix row; CI green |
| **RC9c** | HypothesisProposed | Via G7 writer. | G4, G7 **#64** | **#105** | `tests/test_rc9c_hypothesis_proposed.py::test_emits_hypothesis_proposed`; CI green |
| **RC10c** | HypothesisRefined | Via G7. | RC9c | **#106** | `tests/test_rc10c_hypothesis_refined.py::test_emits_hypothesis_refined`; CI green |
| **RC11c** | HypothesisExperimentLinked | Via G7. | RC9c | **#107** | `tests/test_rc11c_hyp_experiment_linked.py::test_emits_linked`; CI green |
| **RC17** | retries + EXP-n | `n < Study.max_attempts`. | G4, RC17a | **#108** | `tests/test_rc17_retries.py::test_no_retry_at_max_attempts`; CI green |
| **RC20** | CAP #2 suite | 1:1 CAP #2. | RC4–RC8, RC7r | **#109** | `tests/test_rc20_cap2.py` — one test per CAP #2 clause; CI green |

HITL **caller** that opens the reject gate remains **#52** (consumed; not an owner of RC7r).

## RC3t — drop defaults (target; main still has them)

**Remove defaults on symbols:** `Study.confirmations_needed` / `refutations_needed` / `refutation_priority` / `supported_needed` / `max_expansions` (`domain.Study`); claims bind when `study is None`; `hypothesis_verdict` kwargs; `rules.verdict` / `rules.priority_refuted_support_note`; `lifecycle.create_study`.

**`max_expansions` (sole quota field):** replaces profile `max_replans` (migration already on main: `domain.py` / `upcasters.py`). Main still defaults `max_expansions=2` (and `graph_schema.yaml` notes legacy `max_replans` default 2). RC3t drops the construction default → typed refuse `max_expansions_required`. No second Study field named `max_replans`.

**`refutation_priority` canon (decided now):** value is taken **only from `Study.refutation_priority`**. `hypothesis_verdict` / `rules.verdict` must not supply a kw default — callers pass Study’s field. (Today main disagrees: `hypothesis_verdict` defaults `True`, `Study` / `lifecycle.create_study` / `rules.verdict` default `False`.)

**Legacy `StudyCreated` without `refutation_priority`:** **typed refuse** `refutation_priority_required` on upcast/read — do **not** invent `False`. Rationale: `domain.Study.refutation_priority = False` and `lifecycle.create_study(..., refutation_priority=False)` are construction defaults for *new* in-process objects, not a journal migration policy; inventing would contradict D3 “upcasters must not invent”. Same refuse pattern for other missing threshold keys.

**Typed refuse names:** `study_required` (study is None at verdict bind); `confirmations_needed_required`; `refutations_needed_required`; `refutation_priority_required`; `supported_needed_required`; `max_attempts_required`; `max_expansions_required`; `require_hypotheses_required`.

## Follow-ups — legacy / soft paths still on main (owner: **#98** RC3t unless noted)

| File:line / symbol | Issue | Owner |
|---|---|---|
| `verdict.py` `hypothesis_verdict` (`k_ref`/`k_sup` = `max(1,int(...))` / `max(2,int(...))`) | clamp + coerce | **#98** |
| `verdict.py` `_timeout_kind` / `_refute_unresolved` | `str(tk)`, `getattr(..., False)`, `isinstance(eff, dict)` soft paths | **#98** |
| `verdict.py` `verdict()` compat wrapper; call site `rules.py` (`from … import verdict as hyp_verdict`) | 2-tuple shim used by `rules.verdict` | **#98** |
| `verdict.py` `hypothesis_verdict` tail (`counted` / only-invalid / else) | three `inconclusive` returns with distinct rules — keep distinct in RC8 tests; do not merge | **#104** |
| RM §4.1 gap (defaults still on main) | moved out of #76; removal owned by RC3t | **#98** |
| `verdict.py` ~995 vs ~1137 | diverging kw defaults `refutation_priority` / `supported_needed` (cayman follow-up from #76) | **#98** |
| `tests/conftest.py` ~24, ~32 | dead `refmodel` branch (cayman follow-up from #76) | **#98** |

## RC8 — matrix = `hypothesis_verdict` only (same rows as #76 / RM §4.1)

Symbols: `supported_needed`, `refute_capable` / `refute_intent_counts`, `independence_key`, `refutations_needed`, `refutation_priority`. Distinct = **`independence_key`**.

**Pre-branch:** `catalog_tied` + delegated `proposed_check` → catalog-only counts.

**Call site:** RC3t before RC8 (claims bind must not guess when `study is None`).

**Disposition** (HYP_DELTA §8): supported→`answered`, survived→`survived`, refuted→`refuted`, else `inconclusive`.

| # | Branch | Outcome | Exact expect | Test |
|---|---|---|---|---|
| 1 | no about | `untested` | `rule=="no_counted"` | `tests/test_rc8_question_verdict.py::test_branch_no_about` |
| 2 | no counted ∧ derivation review-only | `inconclusive` | `rule=="derivation_review_only"` | `…::test_branch_derivation_review_only` |
| 3 | `use_inc ≥ k_ref` ∧ `refutation_priority` | `refuted` | `rule=="k_ref"` | `…::test_branch_k_ref_priority` |
| 4 | `use_inc ≥ k_ref` ∧ ¬priority ∧ `use_severe` | `inconclusive` | `rule=="k_ref"` | `…::test_branch_k_ref_conflict` |
| 5 | `use_inc ≥ k_ref` ∧ ¬priority ∧ ¬`use_severe` | `refuted` | `rule=="k_ref"` | `…::test_branch_k_ref_no_severe` |
| 6 | `use_inc > 0` | `inconclusive` | `rule=="inc_below_k_ref"` | `…::test_branch_inc_below_k_ref` |
| 7 | unresolved ∧ idle | `untestable_by_agent` | `rule=="idle_timeout"` | `…::test_branch_idle_before_support` |
| 8a | `use_con ≥ k_sup` ∧ capable refute ∧ unresolved | `inconclusive` | `rule=="refute_unresolved"` | `…::test_branch_support_refute_unresolved` |
| 8b | `use_con ≥ k_sup` ∧ capable refute ∧ **no unresolved** | `supported` | `rule=="support_min"` | `…::test_branch_support_min` |
| 9a | `use_con ≥ 1` ∧ unresolved ∧ idle | `untestable_by_agent` | `rule=="idle_timeout"` | `…::test_branch_survived_idle` |
| 9b | `use_con ≥ 1` | `survived` | `rule=="single_severe"` | `…::test_branch_survived` |
| 10 | unresolved ∧ idle | `untestable_by_agent` | `rule=="idle_timeout"` | `…::test_branch_idle_after_con` |
| 11 | unresolved | `inconclusive` | `rule=="refute_unresolved"` | `…::test_branch_refute_unresolved` |
| 12 | counted remain | `inconclusive` | `rule=="no_counted"` | `…::test_branch_counted_no_support` |
| 13 | only invalid | `inconclusive` | `rule=="validity_failed"` | `…::test_branch_validity_failed` |
| 14 | else excluded-only | `inconclusive` | `rule=="no_counted"` | `…::test_branch_excluded_only` |

Each test calls `hypothesis_verdict(...)` and asserts `(verdict, rule)` — **no** refmodel compare. `study is None` → refuse **`study_required`** (RC3t), not a matrix row.

## M4 — RC12–14

**RC12 scope (timeout→flag)** — moved here from RM §4.1 (main-bound docs must not forward-ref research-cycle IDs). Sandbox/executor on main already set `timeout_kind` / `refute_unresolved` on check effects (`sandbox.py`, `executor.py`). **#110** owns remaining gaps only: any path that still drops timeouts from flags, or fails to surface them into `hypothesis_verdict` inputs — not re-implementing classification already on main. RM §4.1 documents the main flags and points to “owned outside this doc”.

| ID | Scope | Depends | Owner | DoD |
|---|---|---|---|---|
| **RC12** | Remaining timeout→flag gaps into `hypothesis_verdict` inputs (see scope above). | RC3t, RC8 | **#110** | `tests/test_rc12_timeout_flags.py::test_refute_unresolved_blocks_supported`; CI green |
| **RC13** | `result_class=infra` uncharged. | RC12 | **#111** | `tests/test_rc13_infra_uncharged.py::test_infra_does_not_charge_attempt`; CI green |
| **RC14** | DESIGN_M1 jail-mounted same-digest; `SAME_DIGEST_RETRY`. | RC12, RC17a | **#112** | `tests/test_rc14_same_digest.py::test_same_digest_is_retry`; CI green |

## D3 / DESIGN (this PR) + RC3t target

DESIGN §2 Study lists threshold / `max_attempts` / `max_expansions` / `require_hypotheses` as **RC3t/D17 target** (required, **no** field defaults). Sole expansion field = `max_expansions` (not `max_replans`). Main still has construction defaults until RC3t (**#98**). Upcasters: typed refuse on missing Need / flag (no invent).

## RC20 ↔ CAP #2

| CAP #2 | Covered by |
|---|---|
| HYP-n if needed | RC4 + RC20 |
| Without hyps → question verdict | RC8 + RC20 |
| ≥3 hyps; one selected | RC5 + RC20 |
| ≥1 task refs selected | RC6 + RC20 |
| HITL reject → rejected | RC7r + RC20 |

## Consumed

AgentPort → agent layer · HITL caller → **#52** · graph → G7 **#64** · report → #16 · RC15 drop.
