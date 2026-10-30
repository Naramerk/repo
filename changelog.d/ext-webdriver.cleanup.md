bench web driver (`bench/drivers/web.py`) typed: every mechanically removable
`Any` replaced with an exact structural type; annotation-only diff under
`from __future__ import annotations` — behavior, error texts and call API
(module passed in, lazy import + typed refuse on missing dep) unchanged.

- The lazily-imported transport modules `post_run` / `collect_events` /
  `get_run_terminal` receive are now typed by private Protocols instead of
  `Any`: `_HttpxTransport` / `_HttpClient` / `_HttpResponse` for the `httpx`
  module — mypy checks the real module against the protocol at the `_drive`
  call sites (httpx is the repo `mcp` extra) — and `_WebsocketsTransport` /
  `_WsConnect` / `_WsConnection` for the `websockets` module (launcher
  runtime dep, not a repo dependency, so mypy never resolves it; the
  protocol pins only the documented usage shape: `connect(uri, *,
  open_timeout, close_timeout)` as an async context manager whose stream
  yields `recv() -> str | bytes`, plus the `WebSocketException` type).
- JSON-shaped values (WS events, run-status dicts, the POST body) narrowed
  from `dict[str, Any]` / `Mapping[str, Any]` value types to `object` across
  `run_body`, `post_run`, `collect_events`, `get_run_terminal`,
  `is_final_event`, `verdict_of`, `observed_outcome`, `write_summary` and
  `WebDriver._closed_outcome` — every consumer either narrows with
  `isinstance` or only serializes, so `object` is the honest JSON value type.
- `Any` kept (the only remaining markers in the file, both pre-existing):
  the `typing` import, and `evidence_from(observed: Mapping[str, Any])` —
  the rows feed the strict pydantic `EvidenceOutcomeCount` constructor
  (`outcome: str`, `count: int`, `strict=True`, `extra="forbid"`) straight
  from unvalidated journal JSON (`observe_quality` returns
  `dict[str, Any]`); typing the parameter `object` would need a new `cast`
  or runtime re-validation, and re-typing `observe_quality` in
  `bench/drivers/journal.py` is outside this task's file scope.

Counts in this file (base -> head): Any 20 -> 2, getattr 0 -> 0, type: ignore 0 -> 0,
cast 0 -> 0.
