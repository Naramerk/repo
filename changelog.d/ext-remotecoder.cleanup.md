cleanup(ext-remotecoder): 30 ``Any`` removed in tests/tools/coder/test_remote_coder_runtime.py.

``tmp_path: Any`` became ``tmp_path: Path`` in 24 signatures (incl. the
``_runtime`` helper): the pytest fixture is ``Path`` and every use (``/`` joins,
``RemoteSubmissionStore(root: Path)``, ``LocalArtifactStore(root: Path | str)``) stays
``Path``. Two signatures keep the base ``tmp_path: Any`` byte-for-byte — typing
them ``Path`` exceeds the hard 100-character line limit (review rejected the
102/101-character versions), and rewrapping the signature onto several lines is
reformatting rather than cleanup; the two base lines stand
(``test_legacy_status_outside_literal_is_typed_and_job_fails``,
``test_stop_receipt_budget_failure_does_not_mask_timed_out``).
``clock: Any`` became ``Clock | None = None`` in ``_runtime``, ``Clock | None``
in ``_WireHost`` and ``Clock`` in ``_jobs`` — the ``Clock`` protocol from
``core/events/clock`` is what ``RemoteWire`` and ``InMemoryJobStore`` already declare;
producers are ``FakeClock``/``SystemClock``. ``_Journal`` events are ``UnstampedEvent``:
``InMemoryJobStore._emit`` is the only producer of ``record()`` calls (jobs.py).
``_SlowToStartDownload._download(self, task_id, **kwargs: Any)`` now spells the exact
parent signature (``path``/``deadline``/``expected_sha256``/``expected_size``,
keyword-only) — ``RemoteCoderRuntime._collect_outputs`` is its only caller and passes
exactly those kwargs. ``_CancelAfterStart.submit(*args: Any, **kwargs: Any)`` now spells
``InMemoryJobStore.submit(kind, spec, attempt_id, deadline)``; the runtime call site is
positional with those four arguments. Behavior unchanged.

Left as-is: ``# type: ignore[name-defined]`` on ``class _SlowToStartDownload(runtime.__class__)``
is live (mypy reports ``Name "runtime.__class__" is not defined`` without it).
``__getattr__(self, name) -> Any`` with ``getattr(self._inner, name)`` is the deliberate
``InMemoryJobStore`` delegation of ``_CancelAfterStart`` (runtime also calls
``wait``/``complete``/``get`` through it); rewriting delegation into an explicit method
list to erase the marker would be an audit bypass. ``_request(**overrides: Any)`` and
``fields: dict[str, Any]`` build heterogeneous kwargs for the ``RemoteCoderTaskRequest``
constructor; there is no mypy-acceptable precise type for ``**``-unpacking into it.
``spec: dict[str, Any]`` mirrors the declared ``InMemoryJobStore.submit`` contract.
