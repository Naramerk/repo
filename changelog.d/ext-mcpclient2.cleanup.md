tests/mcp/test_client.py: exact JSON value types instead of Any in the
in-process fake RPC handlers. The eleven local `handler` callbacks passed to
`FakeStdioServer` now read `params: dict[str, object] | None`; the six
result-only handlers return `dict[str, object] | None`, and the five that
inject raw JSON-RPC messages return the private alias `_RpcResult`
(`dict[str, object] | list[dict[str, object]] | None`), mirroring the result
half of `tests.mcp.fake_server.Handler`. The pagination fixture map is
explicitly `dict[object, dict[str, object]]` (cursor is an arbitrary decoded
JSON value; indexing `pages[cursor]` still raises KeyError on a miss exactly
as before). Annotations only: no runtime code path, error text, default, or
log changed; no new markers.

Call-chain checks (CodeGraph 1.6.2 / LeanCTX 3.11.4, run from the worktree):
`codegraph init -y`, `codegraph sync`, `codegraph callers FakeStdioServer`
(43 call sites, all under tests/; fake_server.py itself is unchanged, so
other consumers — tests/mcp/test_stdio_adversarial.py,
tests/tools/test_mcp_adapter.py and the rest — keep the `Handler` alias with
`Any` untouched), `codegraph callers default_handler` (46 call sites; its
`dict[str, Any] | None` parameter accepts the wider `dict[str, object] | None`
arguments without friction). Handler bodies were read with
`lean-ctx read tests/mcp/test_client.py -m lines:N-M` and
`lean-ctx read tests/mcp/fake_server.py -m signatures|lines:N-M`.

Counters (occurrences / вхождения, grep -o | wc -l, tests/mcp/test_client.py):
`Any` 23 → 0, `getattr` 1 → 1, `cast(` 0 → 0, `type: ignore` 0 → 0,
`audit-ok` 0 → 0. The `Any` name is gone from the typing import.

Kept markers (line: marker: why):

- 1247: getattr(value, "__module__", "") in
  test_mcp_package_does_not_import_core: `value` comes from
  `vars(mod).items()` over three module namespaces, so it is an arbitrary
  attribute value; module objects themselves and C-extension objects have no
  `__module__`, so the "" default is genuinely reachable. Direct attribute
  access would raise AttributeError where base returns "" — a behavior
  change — so the line stays byte-identical to base.

Verification: ruff check clean. mypy 1.19.1 on the file, default and
`--check-untyped-defs`: no new errors (line-insensitive); head drops one base
error (`Incompatible return value type` at the pagination handler), all other
errors are base = head. The file's tests pass.
