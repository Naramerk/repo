cleanup(mcp): stdio reap follow-ups — probe-error dedup, EINVAL comment, tests
- ``coscientist/mcp/stdio.py``: the two fatal ``McpTransportError`` raises in the
  ``_reap_process_group`` probe loop (non-EPERM errno, and EPERM still failing at
  the ``kill_grace_s`` deadline) now share one ``_probe_failed(exc)`` constructor.
  Message, type, ``retryable=False`` and ``__cause__`` are byte-identical to the
  base — covered by the existing ``match=re.escape`` tests. No behavior change,
  no new markers.
- ``tests/mcp/test_stdio_adversarial.py``: the ``_FAKE_PGID`` safety comment now
  says the probe fails with ``EINVAL`` on Linux (``os.killpg`` rejects this pgid
  outright), not ``ESRCH`` as on macOS. Added
  ``test_reap_probe_eperm_then_alive_at_deadline_times_out`` (public ``aclose``,
  fake clock, stubbed ``killpg``: an EPERM probe before the deadline, then a
  successful probe at it — still pins the base ``McpTimeoutError`` still-alive
  raise) and wrapped the alive-until-deadline ``match=`` in ``re.escape``.
