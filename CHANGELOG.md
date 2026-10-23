# Changelog

## Unreleased

### M1 PR16
- `decomposes` acyclicity (self-loop + cycle) on accept/expand writes; `Plan.rollup` (`all|any|majority|rubric`) + `Plan.subquestions` / `QuestionSpec.origin` (`explicit` = PlanOut.subquestions; `implicit` = parent_id without subquestions listing; `emergent` = PlanExpanded origin=emergent).
- PlanValidator: named subquestions; leaf coverage (task or disposition hook); frozen rubric item `decomposition_covers`; depth ≤ `max_depth`; question count ≤ `max_tasks`.
- Parent disposition via `apply_rollup` in `dispositions()` (after PR11 loop-cap tagging). Strict majority `n//2+1` (1:1 / 2:2 → no majority). Rollup never **raises** a parent above its PR 8b claims-based verdict (may lower/keep); `expand_graph` cycle check uses the full existing decomposes tree. Report section «Дерево подзадач».
- Upcasters (rebased on #24+#26+#27 / main `d88f96b`): PlanAccepted **v10** (after loops v9); PlanExpanded **v4** (after loop fields v3). StudyCreated unchanged (v10; #23 takes v11). Open #28 stacks PlanAccepted **v11** / PlanExpanded **v5** after this PR — whichever merges second renumbers. `GraphEdgeAdded` → `graph_edges` in `_affected_tables` for #27 dirty-key projection; load-writes seed covers decomposes edges. No duplicate `register_upcaster`.
- Rollup clamp: any terminal own ``verdict`` (incl. ``rubric_not_met``); incomplete/no-majority never wipe own; cyclic decomposes localized to subtree (not study-wide wipe).

### M1 PR14 follow-up
- `append_source_fetched`: try/finally releases write permit if append fails after verify; `release_source_fetched_write_permit` raises when no handler is registered.
- Replay pairing trusts journaled `AttemptTainted.{tool,is_fetch}`. Same-name reclassified `is_fetch=False` → domain `FetchToolReclassified` (caught on fold): paired `SourceFetched` uncounted. `FetchIntegrityRaised` journaled only on open/finalize; finalize fold fallback still puts `internal_error` in the report if reconcile fails. Proven by committed old-schema fixture + natural `tick` finalize (`tests/fixtures/journals/reclassified_fetch_old`).
- `fetch_integrity` projection rows survive without `call_id` (key via tool/row_key).

### Perf / journal write SLO
- Restored honest DESIGN §3.5a measurement: 20 parallel attempts on real-disk `tmp_path`, full `StudyWriter` path (queue → `to_thread` → fence/insert/commit), `writer.latency_samples_ms`, 50ms p99, no retries/skips.
- **Root cause (why only rt_exp tailed):** `projections.apply_event` rewrote *every* row in affected tables on each append (thousands of upserts per load) on the fsync-critical path. Experimental `_project_local` (HITL fold) made the writer queue wait longer between commits, so the O(n²) upsert tax showed up as rt_exp p99 spikes (165–356ms) while rt_core stayed ~6ms. Fix: load only needed tables and upsert keys ``_put`` marks dirty (ContextVar — not `id(row)`). Local measure: dirty-keys alone → p99 ~2–3ms for both runtimes; `asyncio.sleep(0)` after append was **not** required and is omitted so in-memory projection stays seq-ordered with the durable batch.
- **Behavior:** `SourceFetched` `apply_event` now loads `fetch_calls` so journaled pairs land in the SQLite projection (previously the truncated table list left pairs invisible on the write path). Rebase on #26 keeps `AttemptTainted` → `(attempts, fetch_calls, fetch_integrity)`, `FetchIntegrityRaised`, `_row_key` for `fetch_integrity`, and `FetchToolReclassified` fold handling (no `QuestionDisposed` stub — that belongs in #28).
- Equivalence: after the 20-parallel load plus seeded AttemptTainted/SourceFetched/FetchIntegrityRaised/HitlOpened, SQLite projections match `fold_events` with non-empty `source_fetches` / `fetch_calls` / `fetch_integrity` / `hitl_requests`. Canon is strict JSON (no `default=str`).
- Removed store commit-only / shm / wal-autocheckpoint / GC test knobs (those hollowed the SLO).

### Sandbox (host CPU sample order)
- `_watch_process`: sample host-tree CPU via `/proc` **before** `poll()` (poll reaps the child and `/proc` vanishes). Keeps no-cgroup SIGXCPU classification accurate; retains the max sample while the guest is alive. Split out of PR14 so quote work stays focused.
- **M2 notes:** `_host_tree_cpu_s` returns `0.0` (not `None`) when `/proc` is readable but idle; missing `CONFIG_PROC_CHILDREN` silently drops descendants; cgroup idle path untested on CI without delegation.

### M1 PR11
- `LoopSpec` on `Plan.loops` (body templates, `scope_*`, mandatory `max_iter` ≤ `Study.max_loop_iter`, `until` predicate).
- Built-in predicates: `survivors_le`, `criterion_met`, `no_change` (scope-restricted); plugins via `register_loop_predicate` (built-ins + existing plugins non-overridable; NFKC names; `survivors_le.n` exact int `0..10000`, never `float()`).
- Invalid LoopSpec → typed **`PlanInvalid`** → **`PlanRefused` + immediate `StudyFinalized`** via **`plan_validate.apply_plan_refused`**. Fail-closed: unknown predicate / unparsable spec / unknown `scope_*`.
- Core materializes each iter as `PlanExpanded(loop=true)` at `PLAN_EXPANDED_SCHEMA_VERSION` / `TASK_CREATED_SCHEMA_VERSION`; `depends_on=["loop:<id>"]` NFKC-normalized; orphan barriers → SKIPPED refuse.
- Until-met → **`LoopCompleted`**. True cap → **`LoopCapped{reason=iteration_cap}`**. Expected eval failures → **`LoopRefused` + `LoopCapped{reason=error}`** (single append batch; no survivor recompute; never disguised as iteration_cap). Unexpected advance/recover errors → journal + **`finalize(trigger=internal_error)`** / `TerminalReason.INTERNAL_ERROR` (never leave TaskDone without StudyFinalized). Disposition `iteration_cap` only for unresolved questions after a true ≥1-iter cap (answered/outside-loop/infeasible preserved).
- Upcasters (rebased on #21 / main `517b772`): PlanAccepted **v9**, TaskCreated **v11**, PlanExpanded **v3** (v2→v3 loop fields on `added_tasks`; #21 owns v1→v2 rubric). **StudyCreated v10** (`max_loop_iter`) stays on #24 — merge order is #24 before #23 (#23 takes v11). `register_upcaster` duplicate guard + continuous chain.
- Fail-closed internals: `_refuse_loop` failures → `finalize_loop_internal_error`; `log.warning` on journal/close failures; `except LeaseLost: raise` before broad excepts; RUNNING closed (`CORE_BUG`/`CRASHED`) under `_finalizing` so nested `_append(TaskDone)` does not re-enter expand/foreach/loop hooks. Never merge from this agent.

### M1 PR14
- `quote_in_source` (K9): token-boundary + digit-edge; CJK/Thai; super/sub before NFKC; norm cache; presence≠polarity.
- Provenance: opaque `FetchCapability` id + private issued registry in `tools.effects` (not forgeable); verify looks up binds and **consumes**. `AttemptTainted` **before** any `is_fetch` call; `SourceFetched` only via permit after consume; store cannot bypass. Digests attempt-scoped (retries/downstream re-fetch). `countable=True`+`trusted=False`. `ArtifactProducer` includes `user`.
- `NORMALIZATION_VERSION` rules `3`+unidata; M1 gaps documented.

### M1 PR13
- `QuestionSpec.final_type` (`verdict` | `artifact` | `both`) and `OutputSpec.rubric` (`Rubric` / `RubricItem`).
- CodeCheck kind `rubric`: qualitative deliverables; aggregate `all_required` | `k_of_n` (required items only); digest = sha256(JCS(canonical fields, `v=1`)).
- PlanValidator: `artifact` ⇒ OutputSpec.rubric; `verdict` ⇒ ≥1 **explicit** criterion (catalog defaults do **not** count); `both` ⇒ both. **Canonical** `accept_plan` refusal: DAG / `expand_after` / shares critic / criteria / pydantic `TaskSpec`/`QuestionSpec` → `PlanRefused` + immediate `StudyFinalized` (never raw `ValueError` to callers; #24 reuses). Idem = sid + sha256(**full** detail, never truncated).
- Finalize / `rt.view()` share one gate: `study_view` passes `question_final_types` into `study_fulfilled` (never deletes them; `untested` blocks). Verdict may only **downgrade**. `rt.view().verdict` == `StudyFinalized.verdict`.
- Judge: keys = JSON array `(task_id, attempt_id, check_id, item_id, output_digest)` (fail-closed if empty); live status == journal (`PENDING` while retry remains); rebuild from ResultPending/ReviewRecorded only (not RouteReturned); corrupt pending CAS / bad retry → `JudgeCasCorrupt` + `finalize(trigger=internal_error)`.
- **Public payload:** `TerminalReason.INTERNAL_ERROR` / finalize `trigger=internal_error` (new enum value for honest study-level failure, e.g. judge CAS corrupt — not `cancel`).
- Rubric role=validity but excluded from `validity_ok` (failed rubric ≠ claim validity_failed). `both` + verdict + rubric pending → reason `rubric_pending`. Bugs in rubric eval / freeze propagate (no `except Exception → UNASSESSED`).
- Upcasters: PlanAccepted **v8**; TaskCreated **v10**; PlanExpanded **v2** — legacy unknown `final_type` → verdict + report line; current unknown → PlanRefused.
- **M1 gap:** NodeRequirement `STALE` on rubric supersede not implemented; MET of one covering task never covers another's rubric.

### M1 PR18
- Frozen closed `core.tool_registry` (loader-gated `register_tool` only; freeze at end of `tool_loader` → `MappingProxyType`; unregistered → `external_effect`); `invoke_tool` only (import-linter + agents AST); `ToolSpec.effect` advisory.
- **M2 follow-up:** AST gate misses aliases/`getattr`/indirect calls; non-agents packages may still import `tool_registry`; agents contract is meaningful once `coscientist.agents` has code.
- Exactly-once: in-doubt Intended-without-Done spans all attempts by fingerprint; completed keys keep `attempt_id`; read not indexed.
- Taint from journal `AttemptStarted.tainted_context`; per-attempt `ContextVar` for sandbox puts (foreach-safe); taint/side_effect deny → `SideEffectDenied` + report lines.
- Journal effect index + AttemptStarted taint map cached incrementally on the writer (no per-invoke ``read_all``); artifact store required before write call.
- `POST /studies` Idempotency-Key: deterministic id, `O_EXCL`, empty-file recover, fsync+rename; takeover + multi-process race tests.
- Upcasters: AttemptStarted **v7** (`tainted_context`) chained after PR12 v5 shares / v6 `budget_limit_rub` (TaskCreated v9 / PlanAccepted v7 unchanged).

### M1 PR12
- `TaskSpec.budget_share` / `deadline_share` (None = equal remainder of undistributed pool).
- Validators / plan critic: Σ `budget_share` ≤ (1−`replan_reserve`); per-task range + `Study.max_task_budget_share`; critical-path Σ `deadline_share` ≤ (1−reserve). Same critic on `accept_plan`, expansions, foreach, and HITL budget apply.
- P2 renorm (journaled on `AttemptStarted` v6): when shares present, `deadline = min(task.deadline, renormed, ed)`; without shares, `min(task.deadline, ed)` only. Budget: no `max_cost_rub` → `allocated_budget_rub=None` (no task cap); pool/share 0 → refuse at start `SKIPPED(budget)` (no attempt, no exception); else exceed only when `spent > cap`. Cap fixed at first allocation across retries; spend accumulates. Remaining cap passed as `budget_limit_rub`.
- **M1 gap (mid-attempt reserve):** there is no LLM/tool-call runner in M1, so `budget_limit_rub` is **not** enforced mid-flight. Core exposes `would_exceed_budget_limit` for M2 runners (stop before a call that would exceed). Until then the share reserve is **not** inviolable mid-attempt: post-hoc overrun is flagged on `ResultRecorded`/`TaskDone.budget_overrun` (original outcome/detail kept), retries are forbidden, and the report says the task was done with an overrun.
- Concurrent starts: pool subtracts allocated-but-unspent of open tasks so reserve stays untouchable.
- Executor debits `AgentTaskOutput.cost_rub` → `BudgetDebited(task_id=)` on **every** result path (scientific / timeout / same_digest / core_bug / catalog_error / infra) in the **same** append batch as `ResultRecorded`/`TaskDone`. Infra still does not charge an *attempt*; money spent is still debited.
- Foreach children **split** the template’s resolved share (`template_share / N`); when the template share is unset, the resolved parent default (equal remainder, capped) is split the same way. Agents cannot raise own `budget_share` (`agent_share_raise`).
- HITL `budget`: explicit shares via journaled `BudgetSharesAdjusted` (critic-validated; «реши сам»/delegate also applies shares); critic reject → `BudgetSharesRefused` + report line. `delegated_by`/`approved_by` on the HITL decision node only (not plan). Report: task «не выполнено: бюджет задачи исчерпан»; overrun «выполнено с перерасходом…»; study «не выполнено: бюджет исчерпан».
- Upcasters: TaskCreated **v9** / PlanAccepted **v7** (share defaults; PR17 sandbox stays on v8/v6); AttemptStarted v6 (`budget_limit_rub`); StudyCreated v9 (`max_task_budget_share`).

### M1 PR17
- Executable CodeCheck kinds + sandbox limits (B7, B8, K5): `exec_runs`, `tests_pass`, `metric_on_holdout`, `reproducible_rerun`.
- **Separate verifier (B1):** collect agent declared outputs (sha256); catalog scorer / task tests in a second fresh bwrap jail (data/scorer/holdout/tests RO; only results RW); read structured `reward.txt` JSON / junit — never stdout. Holdout labels from catalog (`CodeCheck.holdout`); agent holdout slots ignored. `catalog:metric` scores predictions-as-data (`python -I`). `tests_pass` is **observation-only** (`countable=false`, `trusted=false`; tamper-proof = M2). Per-check `input_allowlist` (fail-closed).
- **bwrap hard isolation (B2):** `--clearenv`, system `--ro-bind`, `--tmpfs /tmp`, `--unshare-net`/`--unshare-pid`/`--unshare-ipc`/`uts`/`cgroup-try`, `--disable-userns`, `--cap-drop ALL`, `--new-session`, `--die-with-parent`; journal/CAS never mounted. Hard only after a real probe. Cgroup: start bwrap inside leaf; `oom_detectable` only after guest host pid verified.
- **Fail-closed:** without verified hard FS isolation → `sandbox_unavailable` (no guest exec). CI installs `bubblewrap` and lifts Ubuntu 24.04 AppArmor userns restrict.
- **`trusted=true` only when both net and fs are hard** (never fake hard). Isolation + `core_evaluated` for replay from journal effect, not artifact meta.
- Infra **only** via private FD (wrapper rlimit / cgroup attach / host errors) + blocked/unavailable/verified cgroup OOM — does **not** charge the retry budget. Guest wall/CPU → host-classified `idle_timeout` / `cpu_timeout` / `wall_timeout` (`result_class=timeout`, never `k_ref`/`k_sup`); **`cpu_timeout` from host cgroup watcher**; with cgroup, `RLIMIT_CPU` is a fuse above budget; without cgroup, SIGXCPU/152 → `cpu_timeout` only when host rusage ≥ budget else NONZERO; refute-role → `refute_unresolved`; idle / no-cgroup refute / ≥2 **task-level** prediction timeouts (any template) → `untestable_by_agent` (single last-attempt timeout not escalated). **Catalog-only per-check limits** frozen on all TaskCreated paths; same-digest over jail-mounted bytes; prediction `exec_runs` via verifier jail. Cgroup `cpu.max` + `cpu.stat`; CPU budget always &lt; wall. `tests_pass` + `reproducible_rerun` observation-only. `agent_facing_*` sole agent template serialization (prompt renderer later).
- Upcasters: TaskCreated **v8**, PlanAccepted **v6** (`sandbox_check_limits` / `sandbox_limits_note`; aggregate slots always stripped); stacked on PR10 foreach/quorum versions.
- Docs: host requirements + timeout redesign. CI moves job into `ci-job` leaf before `subtree_control` (no-internal-processes); if still refused, cpu.stat tests skip (known M1 gap).

### M1 PR10
- `TaskSpec.foreach` (selected/all hypothesis|question of a source task): core materializes N `EXP-<n>` via `PlanExpanded(foreach=true)` — no planner, no expansion quota; restart/recover resumes; report lines in Russian.
- Expand at source `TaskDone` **and** at `accept_plan` / after `PlanExpanded` when `of_task` is already DONE (B1; live == replay). `validate_foreach_specs` in `validate_expansion`.
- Selector requires `source_task == of_task`; cap keeps best **rank** (missing/non-numeric rank **last**, report note; never SKIP template); journals `foreach_total`/`foreach_dropped`/`foreach_rank_notes`; kept children nat-sorted; deterministic `pl_foreach_*`; `max_foreach_children` (StudyCreated **v8**).
- Foreach errors → `ExpansionRefused` + `TaskDone SKIPPED(refused)`; `iter_dispatchable_tasks` excludes templates; `start_attempt` rejects them.
- Quorum pools: **one static pool** per fan-in task + **one pool per foreach template** (never by equal quorum value). Reject mix of ``all``+int; order-independent selection; quorum > static pool size rejected; foreach quorum > children → report «кворум > числа детей».
- Foreach template ``SUPERSEDED`` (empty) and ``REFUSED`` both count as done for ``expand_after`` → PLAN-n.
- Upcasters: TaskCreated v7, PlanAccepted v5, AttemptStarted v4, StudyCreated v8.

### M1 PR9
- `PlanExpanded` + H-g trigger (`expand_after` / `AgentTaskOutput.expansion_request` / HITL).
- Agent `expansion_request` / `expand_after` enqueue **PLAN-n → critic** (never direct `PlanExpanded`); HITL plan edit / «decide yourself» is the third trigger; `delegated_by` from `HitlDecided`.
- Agent `max_expansions` vs human `max_human_expansions`; `replan_reserve` / quotas / `max_depth`; invalid requests → `ExpansionRefused(invalid)` (never raise from record/tick); report lines `not done: expansion limit|invalid|reserve`.
- Expansion counts + `replan_used` folded from events (restart == live); recover re-queues pending expand after crash mid TaskDone→expand.
- Core assigns `EXP-<n>` on agent path; deterministic `ExpansionRefused` idem keys; shared-descendant skip rule; agent cannot supersede away countable refutation (`has_refutation`).
- Question add/supersede (`supersedes` question→question); C10 k-filter excludes claims on superseded targets.
- StudyCreated **v7** + PlanAccepted **v4** (`expand_after`); `max_depth` + `decomposes` edge.
- StudyLifecycle property rules: `expand`, `question_supersede`.
- #15 follow-ups kept: `schema_valid` missing/foreign artifact; **`core_evaluated` from effect only** (meta → producer/call_id).

### M1 PR8b
- Hypothesis verdict model (`docs/DESIGN_HYP_DELTA.md`): claim `validity`/`outcome`/`severe`/`independence_key`; stub|partial|lit|invalid excluded **before** counting.
- `independence_key` base = `experiment_id`; count-time merge by **transitive root-data** digest overlap (code vs data from **`ResultRecorded.output_kinds`** via catalog **`code_output_names`** — planner `OutputSpec.role` and agent `media_type` ignored; all-code upstream → intermediate is a root; diamond DFS uses on_stack vs done) or **structural** share-any criterion template (same target, empty roots; empty plan criteria → `catalog.defaults["experiment"]`). No TaskSpec/goal/title/plan_id hash. Method is not in the key.
- **Countable vs trusted:** evidence is **countable** when the core evaluates a catalog check on an artifact bound to the **current attempt** (`produced_by.call_id`). Countable gates **both** `refuted` (`k_ref`) and severe/`supported` symmetrically. Foreign artifacts are rejected. `effect.trusted` is a separate stronger tag — **PR 17** wires executable kinds to `put_sandbox`; trusted only when sandbox limits fully held (`net_isolation != soft`).
- **No numeric effect → indeterminate** (deliberate safe weakening vs main): missing output / missing artifact / empty json_pointer / `foreign_artifact` never refute. Report carries a **specific** reason (`output_not_submitted`, `artifact_missing`, `pointer_empty`, `foreign_artifact`) — not generic `unclear`.
- On journal read/replay: recompute `severe`/`refute_capable`/`trusted`/`countable` from artifact metadata when the store is attached, else from effect-stamped provenance facts — never journal bits. **Compat:** old `inconsistent` journals without countable provenance may recompute differently (indeterminate / not toward `k_ref`).
- `ResultRecorded` **v3** + v2→v3 upcaster for `output_kinds`. M1: caller passes `attempt_id` for call_id binding; production catalogs have empty `code_output_names` (conservative → all data).
- Study: **`supported_needed`** (default 2, ≥2); `confirmations_needed` = PR8 snapshot `k`. StudyCreated **v6**.
- `aggregate='evalue'` rejected at create (deferred until core computes p-values).
- Report: `hypothesis_report_row` in finalize; failures set dedicated `hypotheses_unavailable` (not a fake hypothesis id). Bind snapshots win on replay; finalize recomputes report rows.
- Keeps PR8: check roles, per-experiment `validity_failed` / `unknown_role`, `VerdictSnapshotsRecorded`.
- Load write test: serial/slow; absolute p99 ≤ 50ms SLO in the dedicated perf job.

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
