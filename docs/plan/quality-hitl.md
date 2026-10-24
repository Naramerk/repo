# Plan: #7 quality checks + #18 HITL (small PRs)

Base: `main` @ `2ac0d5b`. One agent = one small PR (~≤300 LOC). Never merge.
This PR (#1) delivers only the first #7 slice: attachment helpers (CodeCheck+ReviewCheck required; UNASSESSED≠FAILED).

## Done in this PR

1. **quality: CheckAttachment helpers (R23)** — pure `coscientist/checks/attachment.py`: require ≥1 code + ≥1 review on a check list; list required-unassessed ids; refuse UNASSESSED↔FAILED coercion. Tests for every function.

## Remaining (do not start here)

2. **quality: wire require_code_and_review into Executor.record** — every `ResultRecorded`/`ResultPending` calls attachment require; CatalogError on miss.  
   Acceptance: any record path without both check kinds refuses typed; with both, payload keeps both.
3. **quality: ReviewScheduled on every record exit** — all record exits that leave review UNASSESSED/PENDING emit `ReviewScheduled` (idempotent).  
   Acceptance: tests cover success, pending, catalog_error, timeout exits.
4. **quality: Result.unassessed fold uses attachment helper** — projection/`Result.unassessed` = `unassessed_required_review_ids` only (never FAILED ids).  
   Acceptance: FAILED review not in `unassessed`; UNASSESSED required is.
5. **quality: review_deadline → UNASSESSED** — expired review wait journals UNASSESSED (not FAILED); outcome via `compute_outcome`.  
   Acceptance: FakeClock past review_deadline → status UNASSESSED; outcome not FAILED solely from that.
6. **quality: catalog defaults require code+review pair** — `load_catalog` refuses when experiment defaults lack both kinds.  
   Acceptance: missing pair → CatalogError at load; minimal fixture has both.
7. **quality: compute_outcome matrix tests only** — B2b MET+UNASSESSED→SUCCESS; only-review UNASSESSED→PARTIAL; FAILED stays FAILED.  
   Acceptance: table-driven tests; no production change unless bug.
8. **quality: #7 acceptance suite** — `test_cap7_*` 1:1 with CAPABILITIES #7.  
   Acceptance: every #7 criterion has one named test green.
9. **HITL: GatePolicyOutcome map** — pure resolve of timeout/headless outcome from `HitlPolicy`/`GateConfig`; typed refuse if missing.  
   Acceptance: plan_approval/result_acceptance/skip_task each have config-sourced outcome; no guessed default in resolve path.
10. **HITL: CriticDecide Protocol + fake** — narrow Protocol for headless/delegate; tests with fake only.  
    Acceptance: Protocol callable; fake returns typed decision; no LLM import.
11. **HITL: plan_approval open+deadline** — `HitlOpened` deadline from GateConfig capped by task/study.  
    Acceptance: deadline equals min(now+timeout_s, task, study); kind unchanged.
12. **HITL: result_acceptance open+deadline** — same for `result_acceptance`.  
    Acceptance: journal state open with config timeout; payload_ref set.
13. **HITL: skip_task open+deadline** — same for `skip_task`; no P7 on open.  
    Acceptance: open only; task not SKIPPED until human accept.
14. **HITL: timeout plan_approval → on_timeout policy** — delegate→critic / fail→FAILED(human_timeout)+deps.  
    Acceptance: both GateConfig paths covered by tests.
15. **HITL: timeout result_acceptance → accept_computed** — close gate; keep computed outcome.  
    Acceptance: after deadline, gate closed; result outcome unchanged by timeout.
16. **HITL: timeout skip_task → deny** — close gate; task not skipped.  
    Acceptance: task still RUNNING/not SKIPPED after timeout.
17. **HITL: headless open never waits** — headless/auto immediate policy; no open gate left.  
    Acceptance: after `open_hitl`, `hitl[rid].open is False` in headless.
18. **HITL: headless plan_approval R2 e2e** — study progresses without human.  
    Acceptance: `HitlDecided(approved_by=critic)`; no hang past deadline.
19. **HITL: headless result_acceptance + skip_task** — accept_computed / deny without hang.  
    Acceptance: both gates resolve on open or first tick in headless.
20. **HITL: answer_hitl three gates** — accept/reject/delegate; skip_task accept→P7.  
    Acceptance: human accept skip_task → DONE(SKIPPED, human); delegate uses CriticDecide.
21. **HITL: HitlDeadlineExtended bounds** — recover grace ≤ task/study deadline.  
    Acceptance: extended deadline never exceeds task or study effective_deadline.
22. **HITL: tick resolves due gates before finalize** — experimental tick order.  
    Acceptance: open past-deadline HITL closed before StudyFinalized on same tick.
23. **HITL: #18 acceptance suite** — `test_cap18_*` 1:1 with CAPABILITIES #18.  
    Acceptance: every #18 criterion has one named test green.
24. **CHANGELOG: #7+#18** — entries after suites green.  
    Acceptance: CHANGELOG lists slices; no behavior change.
