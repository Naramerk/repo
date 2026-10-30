bench/lab/driver/webdrive.py (web-lane driver) is now precisely typed: the
JSON-valued ``Mapping``/``dict`` annotations carry ``object`` values (the
honest type for ``json.loads`` output), and ``build_summary`` returns the new
private ``_WebdriveSummary`` TypedDict — the fixed four-key summary shape that
``main`` reads back, including the ``final`` dict mutation. Annotations only —
runtime behavior, exit codes, printed lines, and the wire contract are
unchanged (``from __future__ import annotations`` keeps annotations inert at
runtime). No audit markers remain in the file and none were added, so nothing
had to be kept with an audit justification.
