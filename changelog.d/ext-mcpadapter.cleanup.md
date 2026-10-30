tests/tools/test_mcp_adapter.py: removed every provable `Any` without
behavior change (19 → 7 occurrences). `_NoFetch.fetch` now matches the
`FileFetcher` protocol exactly (`(url: str, *, max_bytes: int,
timeout_s: float) -> bytes`; the body still always raises AssertionError,
and the only production invocation is `fetcher.fetch(uri, max_bytes=...,
timeout_s=...)`); `_StubSession.call_tool` mirrors the already-cleaned
sibling in `tests/tools/test_mcp_j1_journal.py` (`*_a: object, **_k:
object) -> CallToolReply`; annotations are strings under PEP 563, the
body still always raises the scripted exception); `_profile` takes
explicit keyword-only fields (`effect: ToolEffect`, `is_fetch: bool`,
`idempotent: bool`, `max_retries: int`) with the same defaults the old
base dict used — every call site passes keyword arguments within these
four fields; scripted outcomes are typed `list[CallToolReply |
BaseException]`, the recorded call log is a TypedDict (`_ScriptedCall`)
instead of `list[dict[str, Any]]`, and `_reply()` returns `CallToolReply`
via a module-level import (`coscientist.mcp.types` was already imported,
replacing the function-local one).

Left as-is, on purpose (each verified live, not removable without a
behavior/contract change):

- 2 `type: ignore[arg-type]` on `session=` fakes — `McpBoundTool` declares
  `session: McpSession`, a concrete class; removing the ignores would need
  subclassing `McpSession` (which requires a `MessageTransport` stub and
  real session construction) or re-typing the production signature to a
  Protocol (API change). mypy `--warn-unused-ignores` confirms both are
  live. The cleaned sibling `test_mcp_j1_journal.py` keeps the same
  ignore for the identical pattern.
- `dict[str, Any]` tool-argument types (`_ScriptedSession.call_tool` and
  the `_ScriptedCall.args` field) mirror the production contract
  `McpSession.call_tool(name, arguments: dict[str, Any], ...)`.
- `err_h` / `handler` signatures (`dict[str, Any] | None` params and
  returns) mirror the shared `Handler` alias in `tests/mcp/fake_server.py`
  (JSON-RPC payload contract; that file is out of scope for this branch).

Counters for the file (occurrences, `grep -o | wc -l`): Any 19 → 7,
getattr 0 → 0, `type: ignore` 2 → 2, `cast(` 0 → 0, audit-ok 0 → 0.
mypy on the test file: the same 6 pre-existing `"McpSession" has no
attribute "calls"` [attr-defined] errors as base (tests/ is outside the
mypy CI scope); no new errors.
