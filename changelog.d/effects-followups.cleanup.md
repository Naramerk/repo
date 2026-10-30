cleanup(effects-2): drop the dead ``store is None`` guard in ``_load_index_from_writer``
- ``coscientist/tools/effects.py``: ``store = writer.store`` — ``StudyWriter.store``
  is ``SqliteEventStore`` (non-Optional; assigned in ``__init__`` from the required
  constructor argument, and mypy green over the tree rules out any ``None``
  assignment). The sole caller is ``invoke_tool`` (CodeGraph), and every path that
  reaches it passes a real ``StudyWriter`` over ``SqliteEventStore``:
  ``tools/resolve.py`` / ``tools/coder/attach.py`` / ``cli/tool_execution.py``, and
  the test helpers construct ``StudyWriter(store)`` directly (no fake writers with
  ``store=None`` anywhere per CodeGraph impact).
- Behavior is truth-table identical for every input, not just typed ones:
  ``hasattr(None, "read_all")`` is ``False``, so ``not hasattr(store, "read_all")``
  keeps taking the same ``return EffectIndex()`` fallback the ``store is None``
  operand took. Only the redundant operand is deleted; the ``hasattr`` duck-typing
  guards (``read_all`` / ``read_after``) stay untouched.
- Left as-is: the two ``getattr(writer, "_pr18_effect_idx", None)`` reads
  (``_load_index_from_writer``, ``_cache_note``). The runtime default is unreachable
  — ``_pr18_effect_idx`` is declared on ``StudyWriter`` and always initialized to
  ``None`` in ``__init__`` (#391), and every caller passes a real ``StudyWriter``
  (verified via CodeGraph callers/impact, test fakes included) — but the field is
  declared ``object`` because core must not import ``tools.effects.EffectIndex``
  (import-linter contract, no ``exclude_type_checking_imports``), so a direct read
  types as ``object`` and cannot flow into ``EffectIndex | None`` without
  ``cast``/``Any``/``type: ignore`` (all forbidden; mypy experiment: "Incompatible
  types in assignment" on both lines), and the annotated assignment itself trips the
  audit's ``named_default`` heuristic once committed (Attribute-valued AnnAssign on
  a changed line), while new audit-ok markers are forbidden.
