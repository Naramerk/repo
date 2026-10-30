test(bench-drivers): pin ``count_marker_events`` DB error handling
- ``tests/test_bench_drivers.py`` only; product code untouched (byte-for-byte).
- Two new one-scenario tests pin that real DB failures propagate through the
  public ``count_marker_events`` instead of an honest-looking ``0``: a
  garbage-bytes file raises ``sqlite3.DatabaseError`` ("file is not a
  database"), and the writer's ``events`` schema minus ``entity_key`` raises
  ``sqlite3.OperationalError`` ("no such column: entity_key"). The benign
  pre-db states were already pinned: missing file → ``0``, file without the
  ``events`` table → ``0``.
- Actual behavior verified empirically before writing the tests. The claim
  "only 'no such table: events' maps to 0" is not literal: the marker query
  never runs without the table — the ``_events_table_ready`` guard returns
  ``0`` first (the poll's pre-db creation window, documented in the module).
  The production caller ``RecoveryDriver._drive`` catches nothing, so corrupt
  file / schema mismatch errors surface to the driver lane. Tests fix the
  actual behavior; no prod change made.

