Test-only sync: the interactive report-gate cassette tests
(`tests/api/test_hitl_stage_gates.py`) replayed the pre-`render_tz_early`
call order, so after the ТЗ moved to right after plan acceptance the
cassette's science body was consumed by the early ТЗ render and the
executor route got it back — schema fail, cassette exhausted, wait_for
timeout. Both report-gate cassettes now carry the ТЗ body in its real
slot (after plan approval) and drop it from the draft/repair report
renders, which skip the ТЗ model call when the early render CASed.
No production behavior changed; gate assertions untouched.
