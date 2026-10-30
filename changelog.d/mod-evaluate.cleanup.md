cleanup(mod-evaluate): provenance ``meta`` typed ``Artifact``; drop 4 ``getattr``, 1 dead ignore
- ``checks/evaluate.py``: ``artifact_belongs_to_attempt`` and
  ``tool_provenance_violation`` take ``meta: Artifact | None`` (was
  ``Any | None``). Every caller (CodeGraph callers/impact: this module only,
  no test callers, no monkeypatching) passes the ``Artifact | None`` result
  of ``LocalArtifactStore.get_meta`` (the ``ArtifactStore`` protocol agrees);
  tests build real stores — no fakes, and nothing constructs the models via
  ``model_construct`` without fields.
- Four provably dead ``getattr`` reads -> direct field access:
  ``produced = meta.produced_by`` in ``artifact_belongs_to_attempt``
  (required ``ToolCallRef`` field), ``meta.provenance == "tool"`` in
  ``tool_provenance_violation``, and ``meta.net_isolation`` /
  ``meta.fs_isolation`` in ``_effect_provenance_fields`` (declared nullable
  fields read under the existing ``meta is not None`` branch). No default
  was reachable; values and branches unchanged.
- ``checks/evaluate.py`` ``evaluate_task_checks``: dropped the dead
  ``# type: ignore[arg-type]`` on the final ``evaluate_code_check(check, …)``
  call — after the ``ReviewCheck*`` isinstance branch ``check`` is already
  ``CodeCheck`` (``apply_frozen_limits`` returns ``CodeCheck``); mypy
  ``--warn-unused-ignores`` flags the marker as unused and full mypy passes
  without it.
- Left as-is (getattr, byte-identical to base): the ``call_id`` /
  ``producer`` / ``produced_by`` / ``out.claims`` ``getattr(..., default)``
  reads — direct access is behavior-identical, but it moves the
  behavior-preserving ``or ""`` / ``or []`` / ``if … else`` defaults onto a
  changed line where the audit-vs-main heuristic fires; silencing those hits
  needs a new ``audit-ok`` (forbidden) or an ``or``-form rewrite (forbidden),
  so the lines stay as in base.
- Left as-is (Any): ``_pointer_get`` / ``_cmp`` / ``_extract_ci`` consume
  ``json.loads`` output (honestly untyped; a precise annotation would need a
  new ``cast``/ignore, forbidden), and the ``values`` / ``proposals`` /
  ``attempts`` / ``effect`` dict shapes mirror the public contracts
  (``CheckResult.effect: dict[str, Any] | None``, ``AgentTaskOutput.values``);
  a TypedDict is not assignable to ``dict[str, Any]`` without a ``cast``.
  No new markers; behavior, reasons, error texts and API unchanged.

