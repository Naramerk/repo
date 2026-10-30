core/execution/attempts.py: exact types and direct attribute access instead of
Any/getattr where the mixin environment is guaranteed. AttemptsMixin serves only
StudyRuntime (machine.py), whose __init__ unconditionally creates every runtime
attribute; mixin annotations now mirror LocalProjectMixin exactly
(study: Study | None, clock: Clock, graph: GraphStore, results/plans/
verdict_snapshots: dict[str, dict[str, Any]], _foreach_children: dict[str,
list[str]]); limitations: list[Limitation] (all producers already return
list[Limitation]); done_payload: dict[str, str]; _attempt_role_from_reviews
takes runtime: object; direct self.<attr> reads replace 8 getattr occurrences;
self.study.user_input_ids direct read (study is None or Study.model_validate
only; pydantic default_factory field; start_attempt already dereferences
self.study.study_id right after the same assert).

Marker occurrences (grep -o | wc -l) in the file, base → head: Any 8 → 6,
getattr 13 → 5, type: ignore 2 → 2, cast( 0 → 0, audit-ok 0 → 0.

Left markers, line: marker: why —
- L96 `type: ignore[arg-type]`: loop_is_complete declares
  `runtime: StudyRuntime` (core/plan/loop.py — outside this task's bounds);
  here self is AttemptsMixin, so the ignore is still required.
- L563 `type: ignore[return-value]`: dead only because attempts rows are
  dict[str, dict[str, Any]] — the membership test against VALID_PROVENANCE
  cannot narrow a dict value; removing it would surface once Any is gone.
- L37 `getattr(runtime, "_proposed_check_reviews", None)`: the attribute is
  created lazily by core/plan/proposed_check.py:70 (absent on runtimes that
  never enter the proposed_check flow), so the default is reachable; runtime
  stays duck-typed.
- L336–339 getattr (artifacts/executor/get_meta): self.artifacts is
  Any | None and executor is Executor | None per machine.py; the meta store is
  duck-typed, getattr-with-default must stay.
