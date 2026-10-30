bench/lab/driver/webdrive.py (web-lane driver) lost every ``Any``: JSON-valued
parameters and locals are now ``Mapping[str, object]`` / ``dict[str, object]``
(the honest type for ``json.loads`` output), ``build_summary`` returns the new
private ``_WebdriveSummary`` TypedDict (the fixed four-key summary shape that
``main`` reads back and mutates the ``final`` dict of), and the
``typing.Any`` import is gone. Annotations only — runtime behavior, exit
codes, printed lines, and the wire contract are unchanged (``from __future__
import annotations`` keeps annotations inert at runtime). No markers were
added and none remain in the file: there is no ``getattr``, ``cast``,
``type: ignore``, ``audit-ok``, or ``Any`` left, so nothing had to be kept
with an audit justification.
