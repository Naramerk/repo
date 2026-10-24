# Core no-hardcodes cleanup — handoff

After #44. ≤400-line PR; ≤1 open; pytest+perf+`audit --vs-main`; never merge.

## Сделано (#53)
Required `schema_version`; `stamp_envelope` refuses behind/ahead; omit-sites use
`writer_schema_version(type)`. Legacy fixtures: `seed_legacy_journal` (raw SQL rows;
gate stays on). No `make_unstamped_event`.

## Обязательный следующий PR
**H2 Drop `EventEnvelope` alias** — `UnstampedEvent` only; ≤400; suite+audit green.

## Remaining (≤400)
H3 empty upcasters · H4 constants→v1 · **H5–H10 writer literals by module** (~70 prod
`schema_version=N`, hitl/executor/…) · H11 `is_code=True` · H12–H17 Study/GateConfig ·
H18–H21 PLAN-n · H22–H28 invents/except · H29 audit UnicodeDecodeError · H30 REVIEW_CHECKLIST ·
derive `PLAN_EXPANDED`/`STUDY_CREATED` consts from upcasters · ~67 test literals.
