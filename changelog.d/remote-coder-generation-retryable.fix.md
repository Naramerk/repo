fix(cli): remote coder generation failure closes the attempt retryable, not the run
- ``coscientist/cli/coder_remote.py``: an ``LlmError``/``AgentError`` from the
  remote coder generation call (the model-authored task spec) used to raise
  ``CliRefuse`` and kill the whole study — live st_517ffaea 2026-10-31: a
  mid-generation stream break (``RemoteProtocolError``) refused a run with
  12/13 tasks DONE. The generation phase owns nothing on the backend yet, so
  the failure now closes the attempt retryable (core retry machinery, bounded
  by max_attempts), releases the task from ``coder_dispatched`` and logs the
  error; after the attempts are exhausted the task fails honestly and the
  study closes with a real verdict. Contract violations above generation
  (mixed hints, non-experiment route, missing runtime/request) still refuse
  typed.
- ``coscientist/cli/run.py``: the driver passes ``coder_dispatched`` into the
  remote dispatch and re-dispatches core-fired retry attempts — the coder
  branch previously dispatched strictly once per task key and never drove a
  retry attempt; ``executed_aids`` bookkeeping keeps the open post-submit
  attempt from being re-driven.
- Tests: unit tests for the retryable close + release (LlmError and
  AgentError); the invented-output e2e now asserts the repair path (adapter
  schema repair heals the generation, job submits, study fulfills).
