# Research records in Study Console

The console now groups every recorded attempt with its zero or more executor
results, including cancelled or open attempts without a result and failed
attempts preceding a successful retry. Computed outcomes, agent claims,
required checks, prediction roles and evidence references remain distinct. Source
buttons open the original CAS artifact; RFC 6901 pointers select an exact JSON
value without treating missing or malformed data as evidence.
Selection returns the original JSON source span, preserving decimal/integer
tokens exactly. A bounded parser rejects duplicate decoded object keys and
non-finite numbers; refusal leaves the original text available for inspection.
Fatal UTF-8 decoding guards structured selection; malformed bytes are available
only as an explicitly labelled replacement-character preview and original download.

Reports written after journal finalization are discoverable from validated CAS
metadata. The inventory labels these files as absent from the journal rather than
inventing registration events or graph evidence. File identity, containment and
SHA verification apply to downloads. Metadata paths, credentials and producer
arguments are excluded from the inventory response.

Graph focus retains direct neighbors so evidence stays connected to its source.
Resizing and changing focus refit the visible graph. Stream rendering is batched
per animation frame while the journal cursor remains event based. Document refresh
is explicit and does not advance that cursor.

## Verification

- CodeGraph 1.6.2 and LeanCTX 3.11.4: exit 0; own index synchronized after edits.
- Contract navigation: `callers resolve_artifact_file`, `callers run_report_lane`,
  `query ResultRecordedPayload`; anchored LeanCTX reads of the affected sources.
- Console API tests: 15 passed, including post-finalization reports, verified
  downloads and metadata directory symlink rejection.
- Browser model/results/HITL/stream tests: 16 passed, including lossy-number,
  duplicate-key, malformed UTF-8 and interrupted-attempt cases found during review.
- Ruff exit 0; mypy 215 source files exit 0; import-linter 21 contracts kept.
- Full non-slow pytest: exit 1, 3258 passed and the established 44 macOS
  sandbox failures (bubblewrap/cgroup unavailable); 14 skipped, 7 deselected,
  7 xfailed. Required Linux CI remains the merge gate.
- Browser smoke on existing `full-stack-3`: all three recorded results visible;
  check source opens the 13,136-byte clustering JSON; five post-finalization
  reports appear with explicit CAS-only provenance. Evidence focus retains five
  nodes and four links. At 390px, document scroll width remains 390px and all
  focused graph nodes stay inside the viewport.

This is read-only verification of historical artifacts. It neither reruns the
research nor establishes scientific adequacy from historical `fulfilled` status.
