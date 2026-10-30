cleanup(ext-studies): studies.py — concrete capability / catalog types replace three Any
- ``coscientist/api/studies.py`` annotation-only cleanup, zero behavior change:
  ``mint_study_user_put_capability()`` returns ``UserPutCapability`` and
  ``release_study_user_put_capability`` takes ``UserPutCapability | None`` —
  the exact signatures of ``_ingest_gate.mint_user_put_capability`` /
  ``release_user_put_capability`` (CodeGraph callers: only ``cli.run.run_study``
  and ``post_studies``, both passing exactly those values). ``post_studies``
  takes ``catalog: Catalog`` — every in-repo caller passes the real
  ``Catalog`` (``minimal_test_catalog()`` / the shared fixture); the only use
  is forwarding to ``open_runtime(catalog: ...)``. The type-only
  ``UserPutCapability`` import sits under ``if TYPE_CHECKING:`` — the gate
  import inside the mint function stays lazy, bodies untouched (the file was
  otherwise byte-identical to ``main``).
- Left as ``Any``, deliberately: ``CreateStudyBody.inputs`` /
  ``input_limits`` are pydantic request-body fields (OpenAPI schema surface;
  client JSON values are dynamic — ``object`` would change the pydantic core
  schema); the raw-body unions ``bytes | str | dict[str, Any] |
  CreateStudyBody`` carry arbitrary client JSON (no structural type; a
  ``dict[str, object]`` swap would be counter-driven churn — and
  ``_request_from_raw_dict`` narrows ``data.get("request")`` then indexes
  ``data["request"]``, which under ``object`` mypy cannot follow without
  restructuring the ``.get`` pattern); ``candidate: dict[str, Any]`` mirrors
  that same client data for ``model_validate``; ``_study_created_matches_body``
  mirrors ``EventEnvelope.payload: dict[str, Any]``; ``**create_kwargs`` is
  pass-through forwarding to ``create_study``; ``use_inputs`` mirrors the
  ``CreateStudyBody.inputs`` field type. The file contains no ``getattr``,
  ``cast``, ``type: ignore``, or ``audit-ok`` markers before or after.
