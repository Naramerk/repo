tests/mcp/test_client.py: removed every provable `Any`, `getattr`, and
`type: ignore` without behavior change. `FakeHttp.calls` is now a TypedDict
(`_RecordedRequest`) instead of `list[dict[str, Any]]`, with one added
`assert body is not None` where a recorded request body is decoded; canned
responses are typed `list[HttpStream | Exception]` (protocol-typed), which
replaces the two `type: ignore[list-item]` on custom SSE streams;
`FailCancelTransport` wraps a `MessageTransport` and no longer needs
`type: ignore[arg-type]` at `connect_memory`; the three injected `wait_for`
helpers are generic over `T` (matching the `AsyncWaitFor` protocol) instead of
`Any` in/out; two dead `type: ignore[override]` on `HangServer._run` are
dropped (confirmed unused by mypy `--warn-unused-ignores`). Left as-is, on
purpose: handler signatures `dict[str, Any] | None` / `-> Any` mirror the
shared `Handler` alias in tests/mcp/fake_server.py (out of scope for this
branch, and narrowing them would add `Any` occurrences or require casts), and
`getattr(value, "__module__", "")` in `test_mcp_package_does_not_import_core`
keeps its default because `vars()` yields objects without `__module__`
(e.g. submodule objects), so the default is reachable.
