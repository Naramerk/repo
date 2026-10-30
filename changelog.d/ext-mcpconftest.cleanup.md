cleanup(ext-mcpconftest): precise types in tests/mcp/conftest.py

tests/mcp/conftest.py: typed the injected-primitive helpers with the production
protocols they already satisfy — `system_sleep`/`fake_sleep` return
`AsyncSleep`, `system_wait_for`/`fake_wait_for` return `AsyncWaitFor` (inner
defs generic over `Awaitable[T] -> T`, same pattern as tests/mcp/
test_final_review.py in #425), `make_config` `servers` is replaced with
`tuple[McpServerConfig, ...] | None`, `make_client` `clock`/`sleep`/`wait_for`
are replaced with `Clock | None` / `AsyncSleep | None` / `AsyncWaitFor | None`.
All call sites (tests/mcp, tests/tools, tests/agents, tests/cli) verified to
pass Stdio/StreamableHttp server configs and Clock/AsyncSleep/AsyncWaitFor-
conforming objects. Annotations only; behavior unchanged.

Marker counts in tests/mcp/conftest.py (вхождения, `grep -o | wc -l`,
before -> after): Any 14 -> 2, getattr 0 -> 0, type: ignore 0 -> 0,
cast( 0 -> 0, audit-ok 0 -> 0.

Left (no audit-ok added):
- `**kwargs: Any` in `make_client` (1 Any occurrence; the other remaining Any
  occurrence is the `from typing import Any` import feeding it): kwargs are
  forwarded to `McpClient(**kwargs)`. A narrower type is impossible without
  enumerating the target parameters, which would move the unexpected-keyword
  `TypeError` from `McpClient.__init__` binding to the `make_client` call
  binding — an error-message/API change.
