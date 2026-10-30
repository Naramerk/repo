tests/mcp/test_final_review.py: removed every provable `Any` and dead
`type: ignore` without behavior change (counts per the CI counter method,
lines containing the marker: `Any` 8 -> 2, `type: ignore` 9 -> 1, `getattr`
0 -> 0, `cast(` 0 -> 0, `audit-ok` 0 -> 0). The injected `wait_for` /
`passthrough_wait_for` helpers are now generic over `T`
(`Awaitable[T] -> T`, matching the `AsyncWaitFor` protocol, same shape as
existing helpers in tests/cli and tests/api) instead of `Any` in/out; the
hang branch uses `asyncio.Future[T]()`, which drops the dead
`type: ignore[return-value]`; the three `FakeStdioServer._run` overrides
drop dead `type: ignore[override]` (parent signature is identical,
confirmed by mypy `--warn-unused-ignores`); the four `fetcher=...
# type: ignore[arg-type]` in fetch_files tests are dropped — the local
fakes structurally satisfy the one-method `FileFetcher` protocol exactly;
`hanging_reap` (monkeypatched over `coscientist.mcp.stdio.
_reap_process_group`) drops `*_a: Any, **_k: Any` for the patched
function's precise signature (`proc`, `pgid`, the three grace/poll floats,
`clock: Clock`, `sleep: AsyncSleep`, `wait_for: AsyncWaitFor` — the exact
kwarg names of the single call site in `StdioTransport.aclose`); the
protocol-negotiation `handler` returns the provable
`dict[str, Any] | None` (its body returns a dict literal or
`default_handler(...)`) instead of `Any`. Left as-is, on purpose:
`# type: ignore[dict-item]` on `content_digest({"bad": {1, 2, 3}})` — dead
today only because the production signature is
`content_digest(result: dict[str, Any])` (untouchable API contract), and
the ignore documents the intentionally non-JSON-able set that must hit the
RFC 8785 failure path; and the remaining `Any` occurrences are the
`dict[str, Any]` JSON-payload contract mirrored from the shared `Handler`
alias in tests/mcp/fake_server.py plus its import. Annotations only — the
file has `from __future__ import annotations`, and the single runtime-shape
change (`asyncio.Future()` -> `asyncio.Future[T]()`) constructs the
identical never-resolving future.
