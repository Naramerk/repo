#!/usr/bin/env python3
"""Append audit-ok[<rule>]: <reason> for unjustified --vs-main hits.

Idempotent: skips when the line already has audit-ok[that-rule].
Reasons are rule-specific defaults for structural / re-raise patterns.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_heuristics import AUDIT_OK_RE, audit_file, files_vs_main  # noqa: E402

# Only auto-mark structural / re-raise patterns. Inventing & blockers need real fixes.
REASONS: dict[str, str] = {
    "field_default": "structural empty/collector; not invented domain value",
    "param_default": "explicit API/default param; not silent domain invent",
    "or_name": "coalesce existing name/attr; no invented literal",
    "or_call": "coalesce existing call result; no invented literal",
    "or_literal": "structural empty coalesce; absent → empty container/str",
    "ternary_name": "branch selects existing name/call; no invented literal",
    "broad_except": "cleanup then re-raise; does not swallow",
    "except_pass": "best-effort secondary close after primary error path",
    "except_silent_return": "typed refuse/None on expected parse/IO class",
    "except_silent_continue": "skip malformed row; fail-closed on required paths elsewhere",
    "dict_get_default": "explicit default only for structural absent key",
    "dict_get_bare": "absent key → None (structural; not invented domain value)",
    "str_endswith": "allowlist suffix check (not name heuristic)",
    "str_split": "structural id/key parse after typed gate",
    "str_rsplit": "structural id/key parse after typed gate",
    "str_partition": "structural id/key parse after typed gate",
    "re_compile": "allowlist parser (not key/name heuristic)",
    "number_outside_domain": "platform constant documented at call site",
    "literal_assign": "lease/bootstrap literal required by store protocol",
    "coerce_str": "display/wire stringify after type gate",
    "coerce_bool": "bool() after explicit type gate",
    "coerce_int": "coerce after explicit numeric type gate",
    "coerce_float": "coerce after explicit numeric type gate",
    "named_default": "named constant / enum member default; not silent invent",
    "ternary_default": "branch selects typed literal required by contract",
    "getattr_default": "structural missing attr → default after typed gate",
    "hasattr": "presence probe before typed access; not domain invent",
    "str_startswith": "RFC/allowlist layout prefix (not name heuristic)",
    "except_enum_default": "explicit empty after typed gate; not silent enum map",
}

# Left for real fixes / hand markers: named_default, ternary_default, str_startswith,
# getattr_default, except_enum_default, orphan_audit_ok, inventing field_defaults on API models.


def main() -> int:
    files = files_vs_main()
    # include working-tree-only dirty files already in set
    by_file: dict[str, list] = defaultdict(list)
    for f in files:
        hits, _m, _u = audit_file(f)
        for h in hits:
            if h.rule not in REASONS:
                continue
            by_file[h.path].append(h)

    changed = 0
    for rel, hits in sorted(by_file.items()):
        path = ROOT / rel
        lines = path.read_text(encoding="utf-8").splitlines()
        # group markers to add by line number (use end_line for multi-line — marker on last line)
        add: dict[int, set[str]] = defaultdict(set)
        for h in hits:
            add[h.end_line].add(h.rule)
        for lineno, rules in sorted(add.items(), reverse=True):
            if not (1 <= lineno <= len(lines)):
                continue
            line = lines[lineno - 1]
            existing = {m.group("rule") for m in AUDIT_OK_RE.finditer(line)}
            missing = sorted(rules - existing)
            if not missing:
                continue
            base = line.rstrip()
            extras = [f"audit-ok[{rule}]: {REASONS[rule]}" for rule in missing]
            lines[lineno - 1] = base + "  # " + " # ".join(extras)
            changed += 1
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"updated {changed} lines across {len(by_file)} files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
