cleanup(executor): narrow provable types in executor.py — 6 getattr lines and 27 Any removed, behavior unchanged
- ``core/execution/executor.py``: ``checks`` is typed end-to-end as
  ``list[CheckResult]`` — it is produced only by
  ``evaluate_task_checks`` (annotated ``→ list[CheckResult]``) and never
  changes type (``c.model_copy`` keeps it), so the module-level helpers
  (``_normalize_to_template_id``, ``_criterion_key_for_claim``,
  ``_criterion_status_for_claim``, ``_required_validity_failed``,
  ``_no_effect_reason_from_checks``, ``_role_for_claim_key``,
  ``_effect_from_checks``, ``_derive_claim_core_attrs``,
  ``_commit_timeout``, ``_commit_result``), ``_check_role``/
  ``_is_rubric_check`` and the ``elevated`` buffer take
  ``CheckResult``/``RequirementRef`` instead of ``Any``. CodeGraph callers
  confirm every caller, including the direct test callers of
  ``_derive_claim_core_attrs`` (real ``Task``/``CheckResult`` or
  ``SimpleNamespace(spec=TaskSpec…)`` with ``checks=[]``).
- ``Executor.__init__`` callbacks narrowed to the types production callers
  already pass (``machine.make_executor``): ``clock: Clock`` (protocol,
  structural for ``SystemClock``/``FakeClock``), ``on_events:
  Callable[[EventEnvelope], None]``, ``get_results``/``get_proposals:
  Callable[[], dict[str, dict[str, object]]]``, ``get_artifact_kinds:
  Callable[[], dict[str, str]]``, ``on_budget_debit: Callable[...,
  Awaitable[None]]``. The derive/commit paths take ``Catalog | None``,
  ``LocalArtifactStore | None``, ``list[object] | None``, ``Task`` and
  ``GraphStore | None`` (graph helpers), and the three re-wrapped helper
  signatures type the claim payload as ``raw: dict[str, object]`` (bodies
  only do ``raw.get``/``str()``/``in`` checks). ``_check_field`` takes and
  returns ``object`` — it is the generic dict|attr accessor.
- Reachable-attribute ``getattr`` replaced by direct access only where the
  attribute is declared on the statically known type, the default is
  unreachable AND the rewritten line triggers no audit heuristic:
  ``GraphNode.kind``, ``CheckResult.effect``/``status.value`` in the
  unapproved-check and prediction-status loops, ``task.kind.value``,
  ``self.get_artifact_kinds`` (declared in ``__init__``),
  ``task.deadline``. No behavior change; no new ``type: ignore``/``cast``/
  ``Any``/``getattr`` lines.
- Markers left in place, each because removal is not provable or is
  gate-forbidden (no new ``audit-ok`` allowed): ``_check_field`` keeps
  ``getattr(c, name, None)`` — generic accessor over two input shapes;
  ``getattr(task, "spec"|"method"|"criteria"|"task_id", …)`` stay —
  ``task`` is honestly ``Any`` (tests feed ``SimpleNamespace`` partials,
  and the ``task_id`` read sits in an explicit ``try/except``);
  ``getattr(self, "_runtime_attempts", None)`` stays — the attribute is
  injected externally (``machine.py``/tests attach it with
  ``type: ignore[attr-defined]``); ``str(getattr(r, "template_id",
  None) or r or "")`` stays — the ``or r`` fallback is reachable for a
  falsy ``template_id=""`` (``RequirementRef`` accepts it). The
  ``getattr`` forms around ``or {}``/``or []``/``or ""``/``.get(..., [])``
  fallbacks (``CheckResult.detail``/``effect`` reads, ``catalog.defaults``,
  ``task.attempts``/``spec.expected_outputs``/``ospec.name`` loops) stay
  byte-identical to the base: unmasking the attribute would put an
  ``or_*``/``dict_get_default`` heuristic hit on a line changed vs main,
  which the audit gate refuses and we may not silence with new
  ``audit-ok`` markers; likewise ``get_graph`` stays ``Any`` — typing it
  forces a rename inside the question-node loop, which would drag the
  ``ref_id or node_id.split(...)`` line into the audited set. Payload and
  result dicts keep ``dict[str, Any]`` (honest JSON-shaped values); both
  ``type: ignore[arg-type]`` stay — ``claim_outcome`` is ``str | None``
  against the ``ClaimOutcome`` literal and ``kind`` is ``str | None``
  against the ``ArtifactKind`` literal; narrowing either needs a
  ``cast``, which is forbidden here.
