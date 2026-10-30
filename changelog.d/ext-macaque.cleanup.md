tests/checks/test_macaque_closeout.py: removed 5 ``type: ignore`` and the only
``getattr`` with zero behavior change. ``TaskSpec.sandbox_check_limits`` is
``dict[str, FrozenCheckLimits] | None``; the four indexing/set sites now narrow
with a runtime-true ``assert ... is not None`` (file lines 152, 788, 817)
instead of five ``ignore[index]`` / ``ignore[arg-type]`` comments. The
``getattr(lim, field) == getattr(catalog_mod, const)`` loop is unrolled into five
direct assertions: every attribute is a declared ``SandboxLimits`` dataclass
field and every ``SANDBOX_*`` constant is a ``Final`` module global (verified via
CodeGraph callers: all instantiations go through the typed constructor).

Kept markers, all on intentionally wrong-typed negative tests (removal would
strip the intent marker, not fix any friction; mypy does not check untyped test
bodies under the repo config):

* 140 ``ignore[call-arg]``: omits required ``exempt_from_freeze`` on purpose
  (ValidationError expected).
* 169 ``ignore[call-arg]``: passes unknown ``surprise=1`` on purpose
  (extra="forbid" expected to refuse).
* 292 ``ignore[index]``: dead only because ``open_runtime`` returns ``Any``;
  kept per rules (ignores dead solely due to ``Any`` upstream stay).
* 490 ``ignore[arg-type]``: int ``timeout_s=1`` where float is required on
  purpose (strict float typing expected to refuse).
* 1034, 1092, 1120, 1167 ``ignore[call-arg]``: omit required arguments on
  purpose; ``TypeError`` is the asserted behavior.
* 1266, 1269 ``ignore[arg-type]``: ``**kw`` fixture feeds a ``MagicMock`` proc
  and str isolation tokens into ``_classify`` on purpose; typing the fixture
  would restructure the test for no behavioral gain.
* 1627 ``ignore[arg-type]``: str ``catalog_version`` on purpose (strict-int
  refusal expected).

Review follow-up: the ``assert rows["exec_ok"] == FrozenCheckLimits(**_row(...))``
block is restored byte-for-byte to the base layout (only the trailing
``# type: ignore[index]`` on its first line is dropped); no other formatting
touched.

Exact marker counts for this file, ``git grep -cE`` matching lines
(base = ``origin/cleanup/refactor``, after = this branch; ``grep -o``
occurrence counts in parentheses where they differ):

* standalone ``Any``: 0 → 0.
* ``getattr``: 1 → 0 (2 → 0 occurrences; both sat on the unrolled loop line).
* ``cast(``: 1 → 1 — the same byte-identical pre-existing string literal
  ``assert "cast(" not in inspect.getsource(ex)``; the file has no actual cast.
* ``type: ignore``: 16 → 11 (5 removed, 0 added).
* ``audit-ok``: 0 → 0.
