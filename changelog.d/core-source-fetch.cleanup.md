cleanup(core-source-fetch): narrow provable types in source_fetch.py — 6 Any lines and 5 getattr lines removed, behavior unchanged
- ``core/execution/source_fetch.py``: ``capability`` is typed
  ``FetchCapability`` in ``verify_fetch_capability`` and
  ``assert_fetch_capability_bound`` — every caller passes a minted capability
  (``append_source_fetched``, artifacts ``put_fetched``, tests mint via
  ``effects._mint_fetch_capability_for_tests``) and the effects registry impl
  re-validates with ``isinstance`` before use, so the annotation was the only
  untyped seam. ``put_fetched`` in ``core/execution/artifacts.py`` narrows its
  ``capability`` param from ``object`` to ``FetchCapability`` accordingly (its
  only mypy-checked caller is ``store_fetched_source``); non-capability inputs
  still raise ``ArtifactRejected("fetch_capability_required")`` at runtime
  unchanged.
- ``apply_source_fetch_events`` and ``load_source_fetch_index`` take
  ``Sequence[EventEnvelope]`` instead of ``list[Any]`` — every caller passes
  envelopes: ``_cache_note_fetch`` passes ``[ev]`` (``EventEnvelope``),
  ``reconcile_fetch_integrity``/``load_source_fetch_index_from_writer`` pass
  ``store.read_all()``/``read_after()`` (``list[StoredEvent]``), and the
  direct test caller builds real ``EventEnvelope`` objects. ``store`` is read
  as ``writer.store`` (declared ``SqliteEventStore`` in ``StudyWriter.__init__``;
  all callers — ``open_runtime``, finalize, ``Executor.record`` — pass real
  writers, verified via CodeGraph callers/impact) instead of
  ``getattr(writer, "store", None)``; the duck-type ``hasattr`` gates and the
  ``read_after`` fallback stay byte-identical.
- ``store_fetched_source`` takes ``artifacts: LocalArtifactStore`` and returns
  ``Artifact`` (``put_fetched`` returns ``Artifact``; all callers pass
  ``LocalArtifactStore``); both are annotation-only via ``TYPE_CHECKING``
  imports because ``artifacts.py`` imports ``source_fetch`` inside functions.
- Reachable-attribute ``getattr`` replaced by direct access only where the
  attribute is declared on the statically known type and the rewritten line
  carries no audit heuristic: ``ev.type`` (``UnstampedEvent.type``),
  ``e.type == "StudyFinalized"``, ``writer.store``,
  ``writer.store.study_id``. No behavior change; no new ``type: ignore``/
  ``cast``/``Any``/``getattr``/``audit-ok``.
- Markers left in place, each because removal is not provable or would fail
  the audit gate (no new ``audit-ok`` allowed): payload parameters and
  ``SourceFetchIndex.by_digest`` keep ``dict[str, Any]`` — ``payload`` is the
  free-form journaled JSON object declared ``dict[str, Any]`` on
  ``UnstampedEvent`` and validated dynamically; the ``getattr`` forms around
  ``or 0``/``or {}``/``or ""`` fallbacks (``ev.seq``, ``ev.payload``,
  ``stored.seq``, ``events[0].study_id``, ``e.seq`` in the reconcile/load
  max/filter loops, ``ev.seq`` in ``_cache_note_fetch``) stay byte-identical
  to the base: direct access would put an ``or_empty``/``or_dict`` heuristic
  hit on a line changed vs main, which the audit gate refuses and we may not
  silence with new ``audit-ok`` markers.
