cleanup(ext-sandboxlimits): 1 dead ``type: ignore`` removed in tests/checks/test_sandbox_limits.py.

The file contains no ``Any``/``getattr``/``cast``/``audit-ok``; its 14 ``type: ignore``
comments sit on deliberately mis-typed calls in negative fail-closed tests. The one in
``test_limits_from_check_refuses_int_for_float_budgets`` (``timeout_s=1``) is removed:
mypy accepts ``int`` where ``float`` is expected (PEP 484 numeric promotion), so the
comment suppresses nothing under any mypy version or strictness — verified with the CI
pin mypy 1.19.1 and the gates venv mypy 2.4.0, each on the base file
(``--warn-unused-ignores`` flags it unused) and on a stripped copy (no error appears).
Runtime intent is unchanged: the int is still refused with
``sandbox_limits_bad_type:timeout_s:int``.

Left as-is — 13 ``type: ignore``, each load-bearing or masked-marker:

- ``FrozenCheckLimits(**fields)`` in the ``_row`` helper: live under gates mypy 2.4.0 —
  ``_row`` is fully annotated so its body is checked, and ``dict[str, object]`` unpacking
  into the typed init is a real arg-type error (3 errors without the comment). The helper
  exists to inject invalid values, so no precise type can express that unpacking.
- ``RequirementRef(..., exempt_from_freeze=...)`` (2×): dead only because pydantic's
  ``__init__(**data: Any)`` masks unknown kwargs at type level (the Any-downstream case);
  the runtime rejection comes from ``extra=forbid`` and is exactly what the test asserts.
- The other 10 (``limits_from_check``/``apply_frozen_limits`` calls): flagged unused only
  because the enclosing test functions are unannotated, so mypy does not check their
  bodies; the callees are precisely typed with no ``Any``. With bodies checked (gates
  mypy 2.4.0; e.g. tests/test_pr20_proposed_check.py already annotates its tests) each
  becomes live, suppressing an intentional type violation that the test verifies at
  runtime. Deleting inert markers that would resurface as unfixable errors (new
  ``type: ignore`` is forbidden by the cleanup rules) would corner future test typing,
  so they stand.

Counts in this file (base -> head, вхождения, ``grep -o | wc -l``): Any 0 -> 0,
getattr 0 -> 0, type: ignore 14 -> 13, cast 0 -> 0, audit-ok 0 -> 0.
Behavior unchanged: the only diff is one removed comment.
