fix(cli): pace the run-loop tick — idle passes no longer burn the max_ticks budget
- ``coscientist/cli/run.py``: a dispatch pass with nothing to dispatch (retry
  backoff, HITL wait, coder job in flight) returns in milliseconds; the run
  loop then spun unpaced, consuming the whole ``max_ticks`` budget in seconds
  and refusing the run (``max_ticks exhausted before phase=closed``) with no
  journal event — observed live 2026-10-31 on an otherwise healthy study that
  died mid-EXP at 12/14 tasks. One tick now takes at least
  ``_TICK_IDLE_POLL_S`` (1.0s of real wall time, measured with
  ``time.monotonic``, not the injectable study ``Clock`` — pacing bounds the
  driver's own spin, not study time). Active iterations are unaffected: a
  pass blocked on a stage call already exceeds the floor. With the tick a
  poll again, the preset ``max_ticks`` budget tracks waited time (operator
  presets raised 1200 → 3600 to match the 3600s study deadline).
- ``coscientist/api/run.py``: the web-lane run boundary now logs the typed
  refusal detail (``CliRefuse``) and full traceback (unexpected failures) via
  module logger. Previously the detail lived only in the in-memory run
  record and ``console_status`` masked it as the generic ``run_refused`` —
  the server log had nothing to diagnose from.
