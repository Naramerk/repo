cleanup(share-alloc): partial marker cleanup in ``core/plan/share_alloc.py`` (1 ``getattr``, 1 ``Any``).

``_raw_budget``: ``getattr(spec, "budget_share", None)`` became ``spec.budget_share``; the
parameter is typed ``TaskSpec`` and ``budget_share: float | None = None`` is a declared
pydantic field, so the default was unreachable. ``budget_debit_event`` now returns
``EventEnvelope | None`` instead of ``Any | None`` (it only ever builds an
``EventEnvelope`` or returns ``None``). Behavior unchanged.

Left as-is: the ``getattr(runtime, …)`` reads and ``runtime: Any`` parameters.
``fold_budget_shares_adjusted`` is called with ``LocalProjectMixin`` ``self`` during
journal fold, and partial duck-typed fold hosts exist (``_FoldTarget`` in
``tests/core/test_hyp_v2_registration.py``), so the ``getattr`` defaults are reachable;
typing the other ``runtime`` parameters with a Protocol over private ``StudyRuntime``
fields is left for a separate, reviewed change. ``dict[str, Any]`` payload shapes are
schemaless journal payloads shared with other modules.
