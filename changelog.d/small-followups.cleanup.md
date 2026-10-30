cleanup(small-followups): dead store-is-None guard in taint fold; test follow-ups
- ``coscientist/tools/effects.py`` (``_tainted_from_journal``): drop the dead
  ``store is None`` operand, mirroring #402 for ``_load_index_from_writer``.
  ``StudyWriter.store`` is ``SqliteEventStore`` (non-Optional, assigned in
  ``__init__`` from the required constructor argument); CodeGraph callers/impact
  show the only callers are ``invoke_tool`` and one direct test call, both passing
  a real ``StudyWriter``, and nothing assigns ``store = None``. Truth-table
  identical for every input: ``hasattr(None, "read_all")`` is ``False``, so the
  ``return True`` fail-closed branch is unchanged. The ``hasattr`` guards stay.
- ``tests/test_bench_drivers.py``: the marker schema-mismatch test now creates a
  minimal ``events`` table ``(study_id, type)`` instead of duplicating the full
  writer schema from ``_mk_journal``; the marker query still fails with the same
  ``no such column: entity_key`` (pinned via ``re.escape``). ``_mk_journal`` is
  untouched.
- ``tests/mcp/test_stdio_adversarial.py``: two partial ``match=`` patterns now pin
  the full error texts via ``re.escape`` — the base still-alive
  ``McpTimeoutError`` ("stdio process group still alive after SIGKILL within
  kill_grace_s") and the parametrized fatal probe ``McpTransportError``
  ("process group probe failed after SIGKILL: {exc}").
