# Core heuristics audit

Gate script: `scripts/audit_heuristics.py`.

## Marker form (hand-only)

```text
# audit-ok[<rule>]: <concrete reason about this line>
```

There is **no** auto-marker applicator. Markers are written by hand.

## Anti-template checks

- Reason required; identical reason text may appear at most **3** times
- Duplicate `audit-ok[rule]` on the same line → error
- Marker text inside string/bytes literals → error
- Orphan markers (no hit for that rule on that line) → error
- `broad_except` suppressed only by a marker on the `except` line
- Unknown rule ids → error

## CI / local

```bash
python scripts/audit_heuristics.py --vs-main
python scripts/audit_heuristics.py --list-rules
python scripts/audit_heuristics.py --list-ok
```

`--vs-main` scans only Python files touched vs `origin/main` (ACMR), and
only **changed lines** in those files (pre-existing heuristics on untouched
lines are left for follow-up issues).
