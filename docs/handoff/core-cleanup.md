# Core no-hardcodes cleanup — handoff

After #44 (`2ac0d5b`). ≤400-line PR; ≤1 open; pytest+perf(p99≤50)+`audit --vs-main`; never merge.

## Область
Deferred #44/#41 hardcodes: upcasters, Study/GateConfig defaults, or/_DEFAULT_*, silent except,
id prefixes, PLAN-n, EventEnvelope alias, NodeRequirement.is_code=True, audit UnicodeDecodeError,
REVIEW_CHECKLIST.md.

## Состояние / Сделано
Schema v1 on main. **#53**: required schema_version; stamp refuses behind/ahead; omit-sites use
writer_schema_version(type); no make_unstamped_event (review). Legacy seeds: append_legacy /
permit_legacy_schema_versions / writer_append_*_legacy.

## Следующий PR
**H2 Drop EventEnvelope alias** — UnstampedEvent only; ≤400 lines; suite+audit green.

## Контракты / Проверки / Риски
Config/plan or typed refuse; no invents/clamps/literal writer versions/id startswith; re-raise
LeaseLost; test per fix. Checks: ruff; audit --vs-main; pytest -m 'not perf'; perf gate on.
Risks: upcaster wipe; writer literal mismatch; executor/lifecycle/expand/evaluate/hitl conflicts.

## Вопросы
Empty upcasters vs structural chains? Study defaults profile vs explicit? PLAN-n plan_role vs allocator?

## Remaining PRs (≤400)
1 H2 alias · 2 H3 empty upcasters · 3 H4 constants→v1 · 4 H5–H10 writer literals by module ·
5 H11 is_code=True · 6 H12–H17 studies/lifecycle/GateConfig · 7 H18–H21 PLAN-n/rsplit ·
8 H22–H28 invents/except/noqa · 9 H29 audit UnicodeDecodeError · 10 H30 REVIEW_CHECKLIST.md
