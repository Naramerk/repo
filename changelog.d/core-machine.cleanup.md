StudyRuntime (core/execution/machine.py): exact types instead of Any where
provable — make_executor(artifacts: LocalArtifactStore, on_events: OnEvent |
None, review_statuses: dict[str, ReqStatus] | None), record_budget_debit
payload/extra as dict[str, object]; direct attribute/method access instead of
dead-default getattr on self (bind-time _flush_pending_kind_stamps, view()
question_disposals/artifact_kinds/catalog/artifacts).

Kept markers (line: marker: why) — each removal would change behavior or
require a new audit-ok marker (forbidden):

- 16: Any import: needed by remaining annotations below.
- 88/89/90/98/99/127/128/129/130/135/136/139: dict[str, Any] folds: rows are
  written from local_project.py / executor.py / proposed_check.py / verdict.py
  (owned by parallel runs); a precise value type cannot be proven without
  touching those files, and object would surface mypy errors there.
- 91: asyncio.Task[Any]: no in-repo writer into _attempt_tasks (only pop /
  cancel in core; test-only writes are outside mypy scope), so Task[None]
  would be a guess.
- 95: artifacts: Any | None: tests inject a duck-typed CAS-failure store
  (_BoomArts) that is not a LocalArtifactStore.
- 201: getattr(self, "_check_proposals", {}): removing the getattr makes the
  line an `or {}` rewrite; the audit or_dict heuristic would demand a new
  audit-ok marker (forbidden by the cleanup rules).
- 206: type: ignore[attr-defined]: _runtime_attempts is not declared on
  Executor; declaring it means editing executor.py (out of scope for this
  branch).
- 231/275: getattr(t, "reason", None) ternary: honest rewrite is
  `t.reason if t is not None else None`, an IfExp with Attribute branch —
  the audit ternary_name heuristic would demand a new audit-ok (forbidden).
- 232/276: getattr(reason, "value", reason): same ternary_name constraint as
  231/275; default stays reachable for the None reason.
- 235/278: getattr(self, "_foreach_children", None) or {}: or_dict heuristic
  on a changed line would demand a new audit-ok (forbidden).
- 244: type: ignore[method-assign]: wrapping Executor._append; the attribute
  is not a declared method slot on Executor (executor.py out of scope).
- 295: getattr(arts, "get_meta", None): artifacts is Any-typed by design
  (see 95) and the injected store may lack get_meta — default is reachable.
