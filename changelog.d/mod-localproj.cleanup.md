cleanup(local-project): drop reachable-getattr on envelope seq and narrow provable Any in local_project.py
- ``core/execution/local_project.py``: four ``getattr(ev, "seq", None)``
  (duplicate-``StudyFinalized`` taint log/row, ``AttemptStarted`` and
  ``ResultRecorded`` ``_seq`` guards) became plain ``ev.seq``.
  ``EventEnvelope`` is ``UnstampedEvent``, a frozen pydantic model with
  ``extra="forbid"`` and a declared ``seq: int | None = None`` field, so the
  attribute is always materialized and the ``getattr`` default is
  unreachable; ``ev.seq`` is already used directly in the same fold paths.
  No behavior change.
- Same file: provable ``Any`` narrowed to ``object`` — the local
  ``upd: dict[str, object]`` / ``raw_dl: object`` in ``AttemptStarted``
  (values pass through unchanged to ``Task.model_copy(update=…)``),
  ``_invalidate_verdict_snapshots_for_target(target: object)`` (body only
  does ``is None`` + ``str()``; the two in-file callers pass
  ``Any``/``str | None``), and
  ``_stamp_hypothesis_verdict_attrs(rows: Mapping[str, Mapping[str, object]])``
  (only ``row.get("verdict")``, enum membership and assignment into
  ``dict(node.attrs)``). Annotation-only; mypy 1.19.1 passes with no new
  ignores.
- Left as-is, honestly: (1) the 31 ``getattr(self, …)`` sites — the mixin's
  class-level annotations are declarations, not initializations, and partial
  duck-typed hosts exist (``_FoldTarget`` in
  ``tests/core/test_hyp_v2_registration.py`` initializes only ``graph``,
  ``verdict_snapshots``, ``hypothesis_verdicts``), so the defaults are
  reachable and the ``isinstance`` lazy-init branches are the documented
  fold-before-bind resilience (removal would add ``AttributeError`` paths);
  (2) ``getattr(t, "task_key", "")`` — ``added_tasks`` payload elements may
  be dicts or attribute objects by design; (3) the 15 class-level
  ``dict[str, …Any]``/``list[dict[str, Any]]`` annotations plus
  ``payload: dict[str, Any]`` in ``_fold_finalized_graph`` — schemaless
  journal payloads shared across fold modules (loop/hitl/dispose/
  proposed_check); tightening them would cascade mypy errors into
  out-of-scope modules, and the ``_fold_finalized_graph`` signature line is
  109 chars on base, so editing it would either break the ≤100-char rule for
  touched lines or add new ruff-format drift. Marker counts for the file:
  ``getattr`` 35→31, ``Any`` 21→16, ``cast``/``type: ignore``/``audit-ok``
  0→0. No new raises, no API/CLI changes.
