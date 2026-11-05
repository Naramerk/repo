fix(cli): coder re-dispatch drives the open retry attempt, never starts over it
- ``coscientist/cli/coder_remote.py``: after a retryable generation failure
  the core's retry machinery starts the next attempt itself; the driver's
  re-dispatch must DRIVE that open attempt, but ``_require_attempt`` called
  ``start_attempt`` unconditionally → ``RuntimeError: already has open
  attempt`` killed the run as failed (live st_1ea67a08 2026-10-31: the
  retryable close worked, attempt #2 started, the re-dispatch crashed over
  it). The dispatch now drives the task's open attempt when one exists and
  starts one only for the first dispatch.
- Follows fdedda89 (retryable generation close + re-dispatch) — this closes
  the loop it opened.
