cleanup(claims): drop 8 of 12 ``getattr`` and 15 ``Any`` in ``dispositions.py``
- ``coscientist/core/claims/dispositions.py`` only (plus this entry). Behavior,
  API, error texts and OpenAPI surface unchanged: annotations only plus
  guaranteed-field attribute access (``Task.reason/attempts``,
  ``TaskSpec.expected_outputs``, ``RuntimeClaim.severe``,
  ``Catalog.templates``, ``Artifact.name/size`` — all declared pydantic /
  dataclass fields; no ``Task`` fakes exist per CodeGraph callers/impact —
  tests build real ``Task(...)`` and the production callers pass
  ``runtime.tasks``).
- ``getattr`` 12 → 4: every removed default was unreachable (attributes are
  declared required/defaulted model fields). The None-guard reorder in
  ``_grounded_receipt_refs`` keeps the exact old semantics (``getattr`` on a
  ``None`` meta never fired its default after the ``meta is not None`` guard).
  The four remaining ``getattr`` are the runtime-address union loop in
  ``_dispositions_inner`` (``spec``/``addresses``/``task_key``/``task_id``):
  removal is type-safe too, but the resulting changed-vs-main lines trip the
  audit heuristics (``or_dict``/``or_empty``/``set_default``), new audit-ok
  markers are forbidden, and rewriting the ``or``-chains to dodge the
  heuristics is forbidden as well — so that block stays byte-for-byte as in
  base (the file is byte-identical to main ``5909328`` there).
- ``Any`` 41 → 24 lines: ``catalog`` → ``Catalog | None`` (production callers
  pass ``runtime.catalog: Catalog``; tests build ``Catalog(...)``),
  ``artifact_lookup`` → ``Callable[[str], Artifact | None] | None`` (sole
  provenance is ``ArtifactStore.get_meta`` per ``events/ports.py``; verdict.py
  ``_Meta`` test fakes never reach this module), ``spec`` → ``TaskSpec``,
  ``templates`` → ``dict[str, RequirementTemplate]``, ``checks``/
  ``artifact_ids`` → ``list[object]`` (isinstance-narrowed at use),
  ``loop_capped_questions`` → ``Mapping[str, object] | None`` (values are
  runtime-narrowed via ``isinstance(p, dict)``).
- Left as-is (all remaining 24 ``Any`` lines), no honest removal without
  behavior-adjacent code: disposition row dicts (``dict[str, Any]`` — dynamic
  row schema: conditional keys, ``dict.pop``/``update``, public
  ``stamp_honest_terminal_verdict``); ``results``/``plans`` payload mappings —
  the shared claims-module convention (``claims.py``/``verdict.py``/
  ``rubric.py`` keep the same shape; those files are out of this task's
  bounds); ``question_disposals`` — values are used via ``.get`` without an
  isinstance guard, typing them as ``object`` would force a new runtime branch.
- No new ``type: ignore``/``cast``/``setattr``/``audit-ok``; no test changes
  needed (no public contract moved). Pre-existing ``ruff format`` deviations on
  untouched lines are byte-identical to the base commit (CI enforces
  ``ruff check``, not ``format``).
