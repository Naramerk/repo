# Core no-hardcodes cleanup — handoff

≤400-line PR; ≤1 open; pytest+perf+`audit --vs-main`; never merge.

## #53 done
Required `schema_version`; stamp refuses behind/ahead; `writer_schema_version(type)`.
`seed_legacy_journal` (raw SQL; gate on). No `make_unstamped_event`.

## Next (mandatory)
**H2 Drop `EventEnvelope` alias** — UnstampedEvent only.

## Remaining
H3–H4 · **H5–H10 writer literals** · H11 is_code · H12–H17 Study/GateConfig ·
H18–H21 PLAN-n · H22–H28 invents · H29 audit UnicodeDecodeError · H30 REVIEW_CHECKLIST.
