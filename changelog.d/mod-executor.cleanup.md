cleanup(executor): narrow provable types in executor.py — 21 getattr lines and 30 Any removed, behavior unchanged
- ``core/execution/executor.py``: ``checks`` is typed end-to-end as
  ``list[CheckResult]`` — it is produced only by
  ``evaluate_task_checks`` (annotated ``→ list[CheckResult]``) and never
  leaves that type, so the module-level helpers (``_normalize_to_template_id``,
  ``_criterion_key_for_claim``, ``_criterion_status_for_claim``,
  ``_required_validity_failed``, ``_no_effect_reason_from_checks``,
  ``_role_for_claim_key``, ``_effect_from_checks``,
  ``_derive_claim_core_attrs``, ``_commit_timeout``, ``_commit_result``),
  ``_check_role``/``_is_rubric_check`` and the ``elevated`` buffer take
  ``CheckResult``/``RequirementRef`` instead of ``Any``. CodeGraph callers
  confirm every caller, including the two direct test callers of
  ``_derive_claim_core_attrs`` (real ``Task``/``CheckResult`` or
  ``SimpleNamespace(spec=TaskSpec…)`` with ``checks=[]``).
- ``Executor.__init__`` callbacks narrowed to the types production callers
  already pass (``machine.make_executor``): ``clock: Clock`` (protocol,
  structural for ``SystemClock``/``FakeClock``), ``on_events:
  Callable[[EventEnvelope], None]``, ``get_graph: Callable[[], GraphStore |
  None]``, ``get_results``/``get_proposals: Callable[[], dict[str,
  dict[str, object]]]``, ``get_artifact_kinds: Callable[[], dict[str, str]]``,
  ``on_budget_debit: Callable[..., Awaitable[None]]``. ``graph``/``catalog``/
  ``artifact_store``/``bindings``/``task`` params of the derive/commit paths
  narrowed to ``GraphStore | None``/``Catalog | None``/
  ``LocalArtifactStore | None``/``list[object] | None``/``Task``.
- Reachable-attribute ``getattr`` replaced by direct access where the
  attribute is declared on the statically known type and the default is
  unreachable: ``CheckResult`` fields (``effect``/``detail``/``status.value``/
  ``role``), ``Task`` fields (``attempts``/``deadline``/``spec``/
  ``kind.value``), ``GraphNode.kind``, ``Catalog.defaults``,
  ``OutputSpec.name``, ``self.get_artifact_kinds`` (declared in
  ``__init__``). The shadowing of loop variable ``n`` (``str`` from
  ``OutputSpec.name``) by the question-node loop is resolved by renaming the
  latter to ``qn`` (rename only). No behavior change; no new
  ``type: ignore``/``cast``/``Any``/``getattr`` lines.
- Markers left in place, each because removal would change behavior or the
  audit-heuristic line verbatim (none is removable without a shim):
  ``_check_field`` keeps ``getattr(c, name, None)`` and ``object`` typing —
  it is the generic dict|attr accessor for two input shapes;
  ``getattr(task, "spec"|"method"|"criteria"|"task_id", …)`` stay because
  ``task`` is honestly ``Any`` (tests feed ``SimpleNamespace`` partials, and
  the ``task_id`` read is guarded by an explicit ``try/except``);
  ``getattr(self, "_runtime_attempts", None)`` stays — the attribute is
  injected externally (``machine.py``/tests attach it with
  ``type: ignore[attr-defined]``), so it is not a declared ``Executor``
  attribute; ``str(getattr(r, "template_id", None) or r or "")`` stays —
  the ``or r`` fallback is reachable for a falsy ``template_id=""``
  (``RequirementRef`` accepts it), so the default is not dead; payload
  dicts keep ``dict[str, Any]`` (honest JSON-shaped values); both
  ``type: ignore[arg-type]`` stay — ``claim_outcome`` is ``str | None``
  against the ``ClaimOutcome`` literal and ``kind`` is ``str | None``
  against the ``ArtifactKind`` literal; narrowing either needs a ``cast``,
  which is forbidden here.
