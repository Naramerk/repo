cleanup(ext-macaque41): typed the ``_capture_probe_script`` helper in
tests/checks/test_macaque41_failclosed.py, dropping 2 ``type: ignore``
without behavior change. The helper returned a 2-tuple while annotated
``-> str`` (a live ``ignore[return-value]`` masked exactly that); it is now
``-> tuple[str, int]`` and captures via ``list[tuple[str, int]]`` instead of
a ``dict[str, str]`` (which needed ``ignore[assignment]`` for the int
timeout). The runtime types are proven, not assumed: the monkeypatched
``subprocess.run`` is only ever called by ``probe_bwrap_hard`` with
``timeout=PROBE_SUBPROCESS_TIMEOUT_S`` (``Final[int]``, verified via LeanCTX
read of ``sandbox_probe.py`` L207-212 and ``catalog.py`` L91). The single
caller ``test_probe_script_built_from_named_constants`` unpacks unchanged;
CodeGraph ``callers`` shows the helper is file-local. Unpacked-assertion
values are identical; only the never-ran failure mode differs (IndexError
instead of KeyError), both failing the test.

Kept markers, each verified not removable without behavior change or
suppressed-error risk (all were re-checked with plain mypy on the file:
Success, no new errors; ``--warn-unused-ignores`` reports 9 notes, down from
10):

- ``proc=SimpleNamespace(returncode=-9)  # type: ignore[arg-type]`` —
  ``_classify`` declares ``proc: subprocess.Popen[bytes]``; the test feeds a
  deliberate duck-typed fake, and a real ``Popen`` would spawn an OS process.
  mypy does flag this mismatch in fully-resolved contexts, so the ignore is
  load-bearing.
- 3x ``sb.limits_from_check(**base) ...`` ignores — the tests pass
  deliberately missing / extra / mistyped (``True`` for ``float``) kwargs to
  assert fail-closed ``TypeError``/``CatalogError``; mypy's ``**dict``
  unpacking checks are context-dependent, so the ignores stay.
- 2x ``sandbox_effect(**kw, timeout_kind=...)`` ignores — deliberately
  invalid ``timeout_kind`` values are the test subject; ``**kw`` unpacking
  into ``Literal``-typed params can flag in stricter contexts.
- 3x ``CodeCheck(...)`` ignores — dead only because pydantic's
  ``__init__(**data: Any)`` masks field checks without the mypy plugin
  (Any-masked ignores are kept per task rules); the inputs are deliberately
  invalid (missing / non-bool security toggles) and are what the test
  asserts.
- ``getattr(runtime, "catalog"`` at L892 is a string literal inside an
  assertion that product plan modules do NOT use ``getattr`` — not a call,
  untouchable.

No ``Any``/``cast``/``audit-ok`` exist in the file and none are added.

Counts in tests/checks/test_macaque41_failclosed.py (вхождения,
base 52e5cdd -> head, сверено с counts.sh): Any 0 -> 0, getattr 1 -> 1,
type: ignore 11 -> 9, cast 0 -> 0.
