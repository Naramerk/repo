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
| claim | `effect{est,ci_low?,ci_high?,kind,producer,trusted,countable,core_evaluated,produced_by_call_id}` | nullable | from **CheckResult.effect** (core evaluation), never agent claim attrs |
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
| `ResultRecorded` | v2 → **v3** | + `output_kinds` (name → code\|data from catalog `code_output_names`) | v2→v3: `output_kinds` default `{}` |
| `StudyFinalized` | v3 | `hypotheses[]` via `hypothesis_report_row`; optional `hypotheses_unavailable` | old: `[]` / null |

Legacy claims are never severe → never `supported` (V5). On replay/read, **`severe` / `refute_capable` / `effect.trusted` / `effect.countable` are recomputed** from provenance facts (`producer`, `core_evaluated`, `produced_by_call_id`); journal bits are never trusted. **Note:** old journals that stored `outcome=inconsistent` without countable provenance may recompute to indeterminate / not count toward `k_ref`.

**Snapshots vs finalize:** bind prefers persisted `VerdictSnapshotsRecorded` (no softer recomputation on replay). Finalize **recomputes** `hypothesis_report_row` / dispositions from the graph at finalize time and journals them on `StudyFinalized.hypothesis_verdicts`.

## 3. `classify` / severe / countability

1. Validity FAILED or R17 violated → invalid, outcome null, severe false.
2. Prediction checks: any FAILED → inconsistent **only if** evidence is **countable**; non-countable fail → **indeterminate** (never a counting refute). All PASSED (≥1) → consistent; else indeterminate.
3. Null prediction (`predicts_null` from **catalog template**, frozen on criterion) is consistent only if **numerical TOST** holds: real CI (`ci_low < ci_high`) from the metric artifact strictly inside (−sesoi, +sesoi). **Never invent CI from a point estimate**.
4. `severe` = consistent ∧ frozen ∧ catalog `uses_sesoi` ∧ check `used_sesoi` ∧ **countable** ∧ (null → TOST interval; else point exceeds sesoi in `predicted_direction`). **No `min_power` path.**
5. Exact equality (`value_matches`) is a point check: never sets `used_sesoi`, never TOST.

### Countable vs trusted (M1)

| Concept | Meaning | Gates refute / support? |
|---|---|---|
| **countable** | Core evaluated a catalog check on an artifact whose `produced_by.call_id` belongs to the **current attempt** | **Yes** (symmetric both directions) |
| **trusted** | `Artifact.producer` ∈ {core, sandbox} | **No in M1** — recorded/reported only |

Agent `LocalArtifactStore.put` + core `evaluate_code_check` → countable (production path). Foreign artifacts (other attempt's `call_id`) are rejected at eval (`foreign_artifact`) and never countable.

**Foreign-artifact is a mistake guard, not anti-malice:** the agent adapter (DESIGN `agents/adapter.py` / `make_tool_ref`) sets `produced_by.call_id` on put. Agents do not choose call_id. The eval check catches wrong handles and cross-attempt references (wiring mistakes), not a malicious agent forging call_id.

### No numeric effect → indeterminate (safe weakening vs main)

A prediction check that yields **no numeric effect** is **indeterminate** (never a counting refute). This is a deliberate safe weakening versus main, where a FAILED prediction always became `not_met` / refute. Covered cases and **specific** report reasons (never a generic `unclear`):

| Case | CheckResult.detail / exclusion_reason |
|---|---|
| Output slot not submitted | `output_not_submitted` |
| Artifact id missing from store | `artifact_missing` |
| Empty / missing json_pointer value | `pointer_empty` |
| Artifact `produced_by.call_id` ≠ current attempt | `foreign_artifact` |

Study disposition / `StudyFinalized.question_dispositions[].reason` and `excluded_claims[].reason` carry that code. Claim `outcome=indeterminate`; severity and `k_ref` counting skip it.

### Sandbox producer wired (M1 PR 17)

Executable catalog CodeCheck kinds (`exec_runs`, `tests_pass`, `metric_on_holdout`, `reproducible_rerun`) run in a **core-owned** local sandbox (`coscientist.checks.sandbox`) and store logs via `put_sandbox` (`Artifact.producer=sandbox`). Agent `put` + non-executable catalog eval still yields `trusted=false`.

- **`net_isolation: hard | soft | none`** is recorded on every `SandboxResult` and on sandbox `Artifact` meta (and stamped into `effect`).
  - `allow_network=True` → `none` (network permitted by catalog) with FS still hard when the probe passes.
  - `allow_network=False` + working bwrap netns → `hard`.
  - Probe fail / no bubblewrap → `soft` **fail-closed**: no guest exec; reason `sandbox_unavailable:isolation` (recorded/reported; study still closes).
- **`trusted=true` only when both `net_isolation` and `fs_isolation` are `hard`**. `allow_network=True` ⇒ not trusted. There is no silent downgrade to trusted.
- **trusted** remains a severity **tag** for reporting only — it does **not** gate `refuted` / `supported` (countable does).
- Sandbox infra **only** via the private FD channel (wrapper rlimit failure, cgroup attach failure, bwrap/launcher host errors) plus blocked / unavailable / verified cgroup OOM → `result_class=infra` (does **not** charge the retry budget). Guest wall or tree-wide CPU budget exhaustion is host-classified from `cgroup cpu.stat` + wall: `idle_timeout` (cpu/wall below threshold → suspected sabotage → hypothesis `untestable_by_agent`) / `cpu_timeout` / `wall_timeout` → `result_class=timeout`, **never** countable toward `k_ref`/`k_sup`. **`cpu_timeout` from host cgroup watcher** (`kill_cause==cpu`); with live cgroup, `RLIMIT_CPU` is a fuse **above** the cgroup budget (watcher wins the race). Without cgroup, guest `SIGXCPU`/152 → `cpu_timeout` only when host-measured child rusage CPU ≥ budget, else scientific `NONZERO`. Refute-role timeout → `refute_unresolved` (hard-blocks verdict `supported` regardless of `k_sup`). Without `cpu.stat`, refute-role timeouts are never plain benign `wall_timeout` — elevate to `idle_timeout` / untestable. Structural rule: ≥2 prediction/refute-role timeouts **on the task** (any template_id; alternation does not evade) → `untestable_by_agent`; a single timeout (even on the last charged attempt) is not escalated. Support-role timeout contributes no `k_sup`. **Limits come only from the catalog per check** (agent `sandbox_timeout_s` / `sandbox_memory_mb` / agent `sandbox_check_limits` stripped + journaled); frozen per-check at every `TaskCreated` path (`accept_plan` / `expand` / `foreach` / `expand_trigger`) via `freeze_task_sandbox_limits`; retries cannot raise them (replan + critic only). Every guest-timeout attempt is charged; same-digest retry uses a digest over **jail-mounted** input bytes; exhausted timeout budget → task closes; `StudyFinalized.contributing_reasons` includes Russian lines «не проверено в пределах бюджета» / idle variant (matched by `TaskReason`, not detail substring). Refute/prediction `exec_runs` go through the **verifier jail** (catalog command, holdout, agent artifacts RO). Output overflow and exit codes remain scientific. Unexpected core bugs → `result_class=core_bug`. `StudyFinalized` still holds.
- **Host requirements:** `bubblewrap`; unprivileged user namespaces (on Ubuntu 24.04 runners set `kernel.apparmor_restrict_unprivileged_userns=0`); cgroup v2 memory+cpu delegation (`cpu.max` + `cpu.stat` tree budget always &lt; wall; else note `OOM not detectable`). Start bwrap **inside** the cgroup leaf; `oom_detectable` only when a guest host pid is verified in the leaf. CI moves the job into a dedicated leaf (`…/coscientist-ci/ci-job`) **before** enabling `subtree_control` on the parent (cgroup v2 “no internal processes”), then nests sandbox leaves under the job; if the runner still refuses, cpu.stat idle/OOM probes skip — **known M1 gap** (fail-closed rules still apply; refute without cgroup → untestable).
- Holdout labels come from the **catalog** (`CodeCheck.holdout` + `holdout_digest`); agent holdout slots are ignored. Metric verifier treats predictions as JSON data (`python -I`; no `/inputs` on `sys.path`). `tests_pass` and `reproducible_rerun` are **observation-only** (`countable=false`; no agent `est` toward `k_sup`; tamper-proof = M2). **Observation only:** `tests_pass` denylists agent filenames that shadow stdlib / pytest hooks (`sitecustomize.py`, `os.py`, …) but this is defense-in-depth, not a complete import sandbox — agents can still ship modules that influence the test process; hence observation-only until M2. Per-check `input_allowlist` (fail-closed when empty). Agent-facing views never serialize full `CodeCheck` (holdout/scorer/tests): **`agent_facing_template_view` / `agent_facing_catalog_view` are the only agent-facing template serialization** (prompt renderer arrives later and must use them).
- **Seccomp filters: deferred to M2.**
- Enforced limits: time, memory (`RLIMIT_AS` + optional cgroup), output size, network policy as above.
- `reward.txt` is strict JSON `{"schema":"coscientist.reward.v1","value": <number>}` — never `pass`/`fail` strings.

### M1 limits (call_id + catalog kinds)

- **Attempt binding:** the executor/eval caller passes `attempt_id`; countable / foreign checks compare `Artifact.produced_by.call_id` to that id. There is no separate ambient “current attempt” resolver. The **adapter** assigns `call_id` at put time (`f"{attempt_id}:{seq}"`); the foreign-artifact check therefore guards against mistaken cross-attempt handles, not a malicious agent.
- **`code_output_names`:** independence `output_kinds` come from the catalog list. **No production catalog ships a non-empty list yet** (test catalog does). Empty list ⇒ every output name is **data** (conservative: more root digests, harder accidental independence splits).
- **Replay countable:** when an artifact store is attached, replay recomputes `trusted`/`countable`/`core_evaluated` from live `get_meta` (prefer meta over journal effect bits). Journal-only replay (no store) uses effect-stamped provenance facts written at eval from that same meta — never journal `trusted`/`countable` bits.

## 4. Counting filter

```
counts(c) = existing_counts(c)
            and provenance == real
            and validity == valid
            and outcome in {consistent, inconsistent}
            and not literature
```

Excluded: stub, partial, invalid (`validity_failed`/`unknown_role`), indeterminate, lit, r17, superseded.
Agent-asserted results **not** evaluated by a core catalog check remain excluded (non-countable → indeterminate).
Only-excluded bags → `inconclusive` (not `untested`).

## 5. Independence sources

- Base key: `sha256(JCS({experiment_id}))` (task id; never attempt id). **Method is not in the key.**
- **Code vs data:** decided from core-owned **`ResultRecorded.output_kinds`**, filled from catalog **`code_output_names`** for the producing task's output names. Names in that list → code; else → **data**. Planner `OutputSpec.role` / media_type and agent `Artifact.media_type` are **ignored**. Legacy journals without `output_kinds` fall back to artifact meta. **Never** use `InputBinding.input` / slot names.
- **Root digests:** DFS walk `InputBinding` → producer `ResultRecorded.inputs` (depth guard; `on_stack` vs `done` so diamonds are not cycles; `get_meta` / producer-index failures → conservative notes, never raise). Roots are artifacts with no upstream producer, **or** whose upstream inputs are all code. Two experiments are one source if their **root-data digest sets overlap**.
- **Structural merge (no distinct root data):** same target ∧ **any shared** criterion template id ∧ both empty root-data sets → one source. Exact template-set equality is rejected. **Do not** hash whole TaskSpec; **never** use goal, title, or plan_id.
- **Catalog defaults:** when `TaskSpec.criteria` is empty, both check evaluation and `criterion_template_ids` use `catalog.defaults["experiment"]`. There is no separate “primary template.”

### Residual risk

- Intermediate producers that omit `ResultRecorded.inputs` / `output_map` break the provenance walk — journal shape is a core invariant.
- Empty-root experiments that share **no** criterion template remain distinct sources.
- Catalog `code_output_names` must list true code outputs; a mis-listed data name is treated as code (not a root). Planner cannot override via `OutputSpec.role`.

## 6. `verdict(h)`

- No claims → `untested`.
- `inc` / `con` distinct by independence **source** (§5).
- `|inc| ≥ k_ref` (**countable** inconsistent only): refuted if priority; else conflict → inconclusive if con non-empty.
- `|con| ≥ supported_needed` ∧ ≥1 **capable** refute intent → `supported`.
- `|con| ≥ 1` → `survived`.
- Else inconclusive; `validity_failed` when only invalid remain.

**Capable refute intent:** intent=refute ∧ frozen ∧ catalog `uses_sesoi` ∧ prediction-role check that `used_sesoi` ∧ **countable** evidence (null → countable CI). Stored `refute_capable=True` is never trusted on read.

## 7. E-value — deferred

`aggregate='evalue'` rejected at StudyCreated with a clear error. DESIGN note: revisit when the core computes p-values.

## 8. Question disposition

- Without hypotheses: supported→answered, survived→survived, refuted→refuted, else inconclusive.
- With hypotheses: answered if ≥1 supported; survived if ≥1 survived and none supported; refuted if all refuted; else inconclusive.
- Implemented via `disposition_for_question` in dispositions/finalize.

## 9. Invariants

- **V1** Deterministic; bind replay equals persisted `VerdictSnapshotsRecorded`.
- **V2** Removing stub/partial/invalid/lit changes no scientific verdict.
- **V3** supported ⇒ ≥ supported_needed distinct core sources, all severe (countable SESOI), ≥1 capable refute.
- **V4** refuted ⇒ ≥ k_ref distinct counted **countable** inconsistent; validity alone never refutes; non-countable fail never refutes.
- **V5** Legacy claims never give supported (absent/`severe=False` on read; forged `severe=True` recomputed).

## 10. Report `hypotheses[]`

Built by `hypothesis_report_row` / `hypothesis_row` in finalize → `minimal_report.hypotheses` and `StudyFinalized.hypothesis_verdicts`.

If `report_hypothesis_verdicts` fails, the report records a dedicated **`hypotheses_unavailable`** field (exception class name) with empty `hypotheses[]` — **not** a fake hypothesis id string.

PR8 priority-refuted note wording restored: `refuted; n supporting results were stub/partial`.

## 11. Tests

Unit + property (`record_claim` V1–V5) + executor e2e:

- Independence: slot-rename, catalog kinds (planner `role=code` ignored), goal reword, intermediate step, all-code upstream, media_type spoof, diamond DFS.
- Structural: share-any templates; empty criteria → catalog defaults.
- Countable: agent `put` + core eval → not_met refutes; two independent SESOI mets → live `supported`; foreign / missing-output finalize with specific reason; executable kinds → `put_sandbox` stamps `trusted=true` without changing the countable gate.
- TOST / non-null SESOI; `value_matches` never `used_sesoi`.
- Forged journal `severe`/`refute_capable`/`trusted`/`countable` recomputed on replay.
- Provenance walk safe; ResultRecorded v2→v3 upcaster for `output_kinds`.
