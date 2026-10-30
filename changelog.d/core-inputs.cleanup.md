cleanup(core-inputs): narrow provable types in execution/inputs.py — 10 getattr, 11 Any removed
- Graph parameters typed ``GraphStore | None`` exactly where every caller
  already passes that: ``claim_nodes_from_attempt``,
  ``_claim_structurally_ok``, ``uses_claim_graph_events``,
  ``collect_bindable_claims``. Proof (CodeGraph callers, all call sites
  read): production callers are ``Executor._commit_result``
  (``graph = self.get_graph()`` — wired in ``machine.make_executor`` as
  ``lambda: self.graph``, a ``GraphStore``) and
  ``AttemptsMixin.start_attempt`` (``getattr(self, "graph", None)`` —
  ``StudyRuntime.graph`` is a ``GraphStore``, default ``None``); every
  direct test caller constructs a real ``GraphStore(default_execution_registry())``
  or passes ``None``. This matches the types the sibling helpers already
  declare (``claims.resolve_existing_target(graph: GraphStore | None)``,
  ``executor._derive_claim_core_attrs(graph: GraphStore | None)``).
- JSON-shaped heterogeneous dicts narrowed to ``dict[str, object]`` /
  ``list[dict[str, object]]`` where bodies only read keys and coerce
  (``str``/``bool``/``type() is``/``in``): ``verdict_binding_digest.snapshot``,
  ``bind_inputs.verdict_snapshots``, ``claim_nodes_from_attempt.claims``
  (executor helpers already annotate raw claim rows ``dict[str, object]``),
  ``collect_bindable_claims.results``. Compatible with all callers
  (quorum/attempts pass ``dict[str, dict[str, Any]]`` / bare ``dict``).
- ``pinned_criterion_output(template: RequirementTemplate)`` — all callers
  (``agents.derive.derive_task_specs`` via ``Sequence[RequirementTemplate]``,
  ``lifecycle.drop_uncitable_task_criteria`` via ``Catalog.templates``,
  the shared-rule test) pass catalog templates; the mandatory ``check``
  field makes the default of ``getattr(template, "check", None)``
  unreachable → direct ``template.check``.
- ``validate_plan_criteria(tasks, catalog: Catalog | None)`` — callers pass
  ``self.catalog`` (``Catalog``) or the ``getattr``-chain in
  ``expand_validate._validate`` (``Catalog`` or ``None``); the explicit
  ``if catalog is None: raise`` stays. Reachable-attribute ``getattr``
  replaced by direct access only on statically known pydantic models with
  always-present fields: ``catalog.templates`` (mandatory on ``Catalog``),
  ``ref.template_id`` (mandatory ``str`` on ``RequirementRef`` from
  ``TaskSpec.criteria``), ``ref.params``/``ref.role``,
  ``tmpl.uses_sesoi``/``tmpl.sesoi``/``tmpl.sesoi_min``/``tmpl.sesoi_max``/
  ``tmpl.role`` (all declared on ``RequirementTemplate``). The ``or []`` /
  ``or {}`` fallbacks are kept as-is — only the dead-default ``getattr``
  layer was removed.
- Markers left in place, each because removal is not provable or is
  gate-forbidden (no new ``audit-ok`` allowed): line 8
  ``from typing import Any`` and line 297 ``derive_attrs: Callable[[dict[str,
  object], bool], dict[str, Any]] | None`` — the ``dict[str, Any]`` return is
  load-bearing: the only producer (``executor._derive_claim_core_attrs``)
  returns ``dict[str, Any]``, and typing the return ``dict[str, object]``
  would make ``core`` an ``object``-valued dict whose ``float(core[...])`` /
  ``dict(core["effect"])`` conversions fail mypy without new ``cast``
  (forbidden here). No behavior change; no new ``type: ignore``/``cast``/
  ``getattr``; marker counts in the file: ``Any`` 13 → 2 lines,
  ``getattr`` 10 → 0, ``cast``/``type: ignore``/``audit-ok`` 0 → 0.
