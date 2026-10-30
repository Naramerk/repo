core/execution/attempts.py: exact types and direct attribute access instead of
Any/getattr where the mixin environment is guaranteed. AttemptsMixin runs only
on StudyRuntime (machine.py), whose __init__ unconditionally creates every
runtime attribute and whose callers are typed StudyRuntime (cli/run.py,
recover.py, plan/foreach.py, plan/loop.py; test fakes delegate to real
runtimes). Mixin annotations now mirror LocalProjectMixin: study:
Study | None, clock: Clock, graph: GraphStore, results/verdict_snapshots:
dict[str, dict[str, object]], _foreach_children: dict[str, list[str]];
limitations: list[Limitation] (all producers already return list[Limitation]);
done_payload: dict[str, str]; _attempt_role_from_reviews takes runtime:
object; direct self.<attr> reads replace seven getattr occurrences;
self.study.user_input_ids read directly (study is None or
Study.model_validate only, pydantic default_factory field, and start_attempt
already dereferences self.study.study_id right after the same assert). The
now-dead `or {}` / `or []` fallbacks on those guaranteed dict/list reads are
dropped: every consumer only reads (collect_bindable_claims,
producer_attempt_failed, filtered_claims_by_target, runtime_claim_from_node
never mutate the passed mapping), so dropping the unreachable default is
observationally identical, including dict identity for the empty case.

Marker occurrences (grep -o | wc -l) in the file, base → head: Any 8 → 3,
getattr 13 → 6, type: ignore 2 → 2, cast( 0 → 0, audit-ok 0 → 0.

Left markers, line: marker: why —
- 104 `type: ignore[arg-type]`: loop_is_complete declares
  `runtime: StudyRuntime` (core/plan/loop.py — outside this task's bounds);
  here self is AttemptsMixin, so the ignore is still required.
- 571 `type: ignore[return-value]`: dead only because attempts rows are
  dict[str, dict[str, Any]] — the membership test against VALID_PROVENANCE
  cannot refine a dict value; removing it would surface once Any is gone.
- 41 `getattr(runtime, "_proposed_check_reviews", None)`: the attribute is
  created lazily by core/plan/proposed_check.py (absent on runtimes that
  never enter the proposed_check flow), so the default is reachable; runtime
  stays duck-typed.
- 253 `getattr(self, "plans", None)`: kept byte-identical to base — plans
  holds validated plan blobs consumed via .get() iteration, so the precise
  annotation would have to carry Any in a newly added line, and the
  object-typed alternative would require restructuring the loop (out of
  bounds).
- 344–347 getattr (artifacts/executor/get_meta): self.artifacts is
  Any | None and executor is Executor | None per machine.py; the meta store
  is duck-typed, getattr-with-default must stay.
