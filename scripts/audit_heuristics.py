#!/usr/bin/env python3
"""Fail-closed heuristics audit — hand markers only, rule registry.

Marker form (exact):
    audit-ok[<rule>]: <reason>

Rules (anti-template):
  - reason required and non-empty
  - same reason text may appear at most 3 times across scanned files
  - duplicate audit-ok[rule] on the same line → error
  - markers inside string / bytes literals → error
  - orphan markers (no hit on that line for that rule) → error
  - broad_except suppressed only when marker is on the except line
  - unknown rule id → error

Scan modes:
  --vs-main   only files touched vs origin/main; hits only on changed lines
  paths...    explicit paths (whole-file scan)
  (default)   coscientist/ + scripts/ (whole-file)

No auto-marker applicator exists; markers are hand-only.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- Rule registry (stable ids) -------------------------------------------------
RULES: dict[str, str] = {
    "broad_except": "except Exception / BaseException / bare except",
    "field_default": "dataclass/Pydantic field default literal",
    "named_default": "field default Name / Attribute / Enum.X",
    "or_dict": "BoolOp Or with Dict/Set/List/Tuple display RHS",
    "or_fstring": "BoolOp Or with JoinedStr RHS",
    "or_slice": "BoolOp Or with Subscript RHS",
    "or_binop": "BoolOp Or with BinOp RHS",
    "or_empty": "BoolOp Or with empty-collection / empty-string RHS",
    "dict_get_default": "dict.get(k, default)",
    "dict_pop_default": ".pop(k, default)",
    "next_default": "next(it, default)",
    "contextlib_suppress": "contextlib.suppress(...)",
    "except_silent_return": "return inside except body",
    "except_silent_continue": "continue inside except body",
    "except_silent_pass": "pass inside except body",
    "except_enum_default": "Name/Attribute assign default inside except",
    "ternary_name": "IfExp with Name/Attribute branches",
    "str_split": "str.split heuristics",
    "str_rsplit": "str.rsplit heuristics",
    "str_partition": "str.partition heuristics",
    "number_outside_domain": "numeric literal outside domain.py",
    "set_default": "setdefault(...) display default",
}

META_RULES: dict[str, str] = {
    "duplicate_audit_ok": "duplicate audit-ok[rule] on same line",
    "marker_in_literal": "audit-ok marker inside string/bytes literal",
    "orphan_marker": "audit-ok with no matching hit on that line",
    "repeated_reason": "same audit-ok reason text used more than 3 times",
    "unknown_rule": "audit-ok rule id not in registry",
    "bad_marker_form": "audit-ok present but not audit-ok[rule]: reason",
}

_MARKER_RE = re.compile(r"audit-ok\[([a-z0-9_]+)\]:\s*(\S.*)$")
_MARKER_ANY_RE = re.compile(r"audit-ok\[")
_MAX_REASON_REPEAT = 3


@dataclass(frozen=True)
class Hit:
    path: Path
    line: int
    col: int
    rule: str
    detail: str


@dataclass(frozen=True)
class Marker:
    path: Path
    line: int
    rule: str
    reason: str


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _literal_repr(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Dict):
        return "{...}" if node.keys else "{}"
    if isinstance(node, ast.List):
        return "[...]" if node.elts else "[]"
    if isinstance(node, ast.Tuple):
        return "(...)" if node.elts else "()"
    if isinstance(node, ast.Set):
        return "{...}" if node.elts else "set()"
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    if isinstance(node, ast.BinOp):
        return "BinOp"
    if isinstance(node, ast.Subscript):
        return "slice"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return ast.unparse(node) if hasattr(ast, "unparse") else "attr"
    return type(node).__name__


def _is_display_empty(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value in ("", None, 0, 0.0, False):
        return True
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)) and not (
        getattr(node, "keys", None) or getattr(node, "elts", None)
    ):
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
        return True
    return False


class HeuristicVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.hits: list[Hit] = []
        self._except_stack: list[ast.ExceptHandler] = []

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        line = getattr(node, "lineno", 1) or 1
        col = getattr(node, "col_offset", 0) or 0
        self.hits.append(Hit(self.path, line, col, rule, detail))

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        t = node.type
        broad = False
        if t is None:
            broad = True
            detail = "bare except"
        elif isinstance(t, ast.Name) and t.id in {"Exception", "BaseException"}:
            broad = True
            detail = f"except {t.id}"
        elif isinstance(t, ast.Tuple) and any(
            isinstance(x, ast.Name) and x.id in {"Exception", "BaseException"}
            for x in t.elts
        ):
            broad = True
            detail = "except (... Exception/BaseException ...)"
        if broad:
            self._add(node, "broad_except", detail)
        self._except_stack.append(node)
        self.generic_visit(node)
        self._except_stack.pop()

    def visit_Return(self, node: ast.Return) -> None:
        if self._except_stack:
            self._add(node, "except_silent_return", "return in except")
        self.generic_visit(node)

    def visit_Continue(self, node: ast.Continue) -> None:
        if self._except_stack:
            self._add(node, "except_silent_continue", "continue in except")
        self.generic_visit(node)

    def visit_Pass(self, node: ast.Pass) -> None:
        if self._except_stack:
            self._add(node, "except_silent_pass", "pass in except")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._except_stack:
            v = node.value
            if isinstance(v, (ast.Name, ast.Attribute, ast.Constant)):
                self._add(node, "except_enum_default", f"assign {_literal_repr(v)} in except")
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            for v in node.values[1:]:
                if isinstance(v, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                    self._add(v, "or_dict", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.JoinedStr):
                    self._add(v, "or_fstring", "or f-string")
                elif isinstance(v, ast.Subscript):
                    self._add(v, "or_slice", "or slice")
                elif isinstance(v, ast.BinOp):
                    self._add(v, "or_binop", "or BinOp")
                elif _is_display_empty(v) or (
                    isinstance(v, ast.Constant) and isinstance(v.value, str)
                ):
                    self._add(v, "or_empty", f"or {_literal_repr(v)}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and len(node.args) >= 2:
            self._add(node, "dict_get_default", f".get(..., {_literal_repr(node.args[1])})")
        if isinstance(func, ast.Attribute) and func.attr == "pop" and len(node.args) >= 2:
            self._add(node, "dict_pop_default", f".pop(..., {_literal_repr(node.args[1])})")
        if isinstance(func, ast.Attribute) and func.attr == "setdefault" and len(node.args) >= 2:
            self._add(node, "set_default", f"setdefault(..., {_literal_repr(node.args[1])})")
        if isinstance(func, ast.Name) and func.id == "next" and len(node.args) >= 2:
            self._add(node, "next_default", "next(it, default)")
        if isinstance(func, ast.Attribute) and func.attr == "suppress":
            self._add(node, "contextlib_suppress", "contextlib.suppress(...)")
        elif isinstance(func, ast.Name) and func.id == "suppress":
            self._add(node, "contextlib_suppress", "suppress(...)")
        if isinstance(func, ast.Attribute) and func.attr in {"split", "rsplit", "partition"}:
            rule = {
                "split": "str_split",
                "rsplit": "str_rsplit",
                "partition": "str_partition",
            }[func.attr]
            self._add(node, rule, f".{func.attr}(...)")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._field_default(node, node.value)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        for branch in (node.body, node.orelse):
            if isinstance(branch, (ast.Name, ast.Attribute)):
                self._add(node, "ternary_name", f"ternary {_literal_repr(branch)}")
                break
        self.generic_visit(node)

    def _field_default(self, node: ast.AST, value: ast.AST) -> None:
        # Skip function bodies; only treat module/class-level annotations as fields.
        if isinstance(value, ast.Constant):
            self._add(node, "field_default", f"default {_literal_repr(value)}")
            if isinstance(value.value, (int, float)) and self.path.name != "domain.py":
                self._add(node, "number_outside_domain", f"number {_literal_repr(value)}")
        elif isinstance(value, (ast.Name, ast.Attribute)):
            self._add(node, "named_default", f"default {_literal_repr(value)}")


def _parse_markers(path: Path, source: str) -> tuple[list[Marker], list[Hit]]:
    markers: list[Marker] = []
    meta: list[Hit] = []
    for i, line in enumerate(source.splitlines(), 1):
        if "audit-ok" not in line:
            continue
        # code portion before stripping comments is whole line; markers are comments.
        found = list(_MARKER_RE.finditer(line))
        if _MARKER_ANY_RE.search(line) and not found:
            # likely malformed
            if re.search(r"audit-ok\b", line):
                meta.append(
                    Hit(path, i, 0, "bad_marker_form", "audit-ok must be audit-ok[<rule>]: <reason>")
                )
            continue
        seen_rules: set[str] = set()
        for m in found:
            rule, reason = m.group(1), m.group(2).strip()
            if rule not in RULES:
                meta.append(
                    Hit(path, i, m.start(), "unknown_rule", f"unknown rule id {rule!r}")
                )
            if rule in seen_rules:
                meta.append(
                    Hit(path, i, m.start(), "duplicate_audit_ok", f"duplicate audit-ok[{rule}]")
                )
            seen_rules.add(rule)
            if not reason:
                meta.append(Hit(path, i, m.start(), "bad_marker_form", "empty reason"))
                continue
            markers.append(Marker(path, i, rule, reason))
    return markers, meta


def _markers_in_literals(path: Path, tree: ast.AST) -> list[Hit]:
    hits: list[Hit] = []

    class Lit(ast.NodeVisitor):
        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, (str, bytes)):
                text = node.value.decode() if isinstance(node.value, bytes) else node.value
                if "audit-ok[" in text:
                    hits.append(
                        Hit(
                            path,
                            getattr(node, "lineno", 1),
                            getattr(node, "col_offset", 0),
                            "marker_in_literal",
                            "audit-ok marker inside literal",
                        )
                    )

    Lit().visit(tree)
    return hits


def audit_file(path: Path) -> tuple[list[Hit], list[Marker]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [Hit(path, e.lineno or 1, 0, "bad_marker_form", f"syntax error: {e}")], []
    vis = HeuristicVisitor(path, source)
    vis.visit(tree)
    markers, meta = _parse_markers(path, source)
    meta.extend(_markers_in_literals(path, tree))

    # Suppress hits covered by markers on the same line (broad_except: except line only).
    by_line_rule: dict[tuple[int, str], list[Marker]] = defaultdict(list)
    for mk in markers:
        by_line_rule[(mk.line, mk.rule)].append(mk)

    remaining: list[Hit] = []
    used: set[tuple[int, str, str]] = set()
    for h in vis.hits:
        key = (h.line, h.rule)
        ms = by_line_rule.get(key)
        if ms:
            used.add((ms[0].line, ms[0].rule, ms[0].reason))
            continue
        # broad_except: only same-line except marker suppresses (already same line).
        remaining.append(h)

    for mk in markers:
        if (mk.line, mk.rule, mk.reason) not in used:
            # allow marker if any hit on that line matched rule (already handled)
            if not any(h.line == mk.line and h.rule == mk.rule for h in vis.hits):
                meta.append(
                    Hit(
                        mk.path,
                        mk.line,
                        0,
                        "orphan_marker",
                        f"unused marker audit-ok[{mk.rule}]: {mk.reason}",
                    )
                )

    remaining.extend(meta)
    return remaining, markers


def iter_targets(paths: list[Path] | None, vs_main: bool) -> list[Path]:
    if paths:
        out: list[Path] = []
        for p in paths:
            if p.is_dir():
                out.extend(sorted(p.rglob("*.py")))
            elif p.suffix == ".py":
                out.append(p)
        return [p for p in out if ".py" in p.suffix and "__pycache__" not in p.parts]
    if vs_main:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "origin/main...HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        names = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().endswith(".py")]
        return [ROOT / n for n in names if (ROOT / n).is_file()]
    out = []
    for base in (ROOT / "coscientist", ROOT / "scripts"):
        if base.is_dir():
            out.extend(sorted(base.rglob("*.py")))
    return [p for p in out if "__pycache__" not in p.parts]


def changed_lines_vs_main(path: Path) -> set[int] | None:
    """Return 1-based new-file line numbers touched vs origin/main, or None (= all).

    New files (no origin/main counterpart) return None so the whole file is audited.
    """
    rel = _rel(path)
    proc = subprocess.run(
        ["git", "diff", "-U0", "--diff-filter=ACMR", "origin/main...HEAD", "--", rel],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    # Brand-new file: diff has only additions; still parse hunks. If git has no
    # path on main, treat as full-file audit.
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"origin/main:{rel}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    lines: set[int] = set()
    for raw in proc.stdout.splitlines():
        if not raw.startswith("@@"):
            continue
        # @@ -a,b +c,d @@
        m = re.search(r"\+(\d+)(?:,(\d+))?", raw)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2) or "1")
        if count == 0:
            continue
        lines.update(range(start, start + count))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument(
        "--vs-main",
        action="store_true",
        help="only files touched vs origin/main; only changed lines",
    )
    ap.add_argument("--list-rules", action="store_true")
    ap.add_argument("--list-ok", action="store_true", help="print markers")
    args = ap.parse_args(argv)

    if args.list_rules:
        for k, v in sorted(RULES.items()):
            print(f"{k}: {v}")
        return 0

    targets = iter_targets(list(args.paths) if args.paths else None, args.vs_main)
    all_hits: list[Hit] = []
    all_markers: list[Marker] = []
    for path in targets:
        hits, markers = audit_file(path)
        if args.vs_main:
            changed = changed_lines_vs_main(path)
            if changed is not None:
                hits = [h for h in hits if h.line in changed]
                markers = [m for m in markers if m.line in changed]
        all_hits.extend(hits)
        all_markers.extend(markers)

    reason_counts = Counter(m.reason for m in all_markers)
    for reason, n in sorted(reason_counts.items(), key=lambda x: (-x[1], x[0])):
        if n > _MAX_REASON_REPEAT:
            sample = next(m for m in all_markers if m.reason == reason)
            all_hits.append(
                Hit(
                    sample.path,
                    sample.line,
                    0,
                    "repeated_reason",
                    f"reason repeated {n} times (max {_MAX_REASON_REPEAT}): {reason!r}",
                )
            )

    if args.list_ok:
        for m in sorted(all_markers, key=lambda x: (_rel(x.path), x.line)):
            print(f"{_rel(m.path)}:{m.line}: [{m.rule}] {m.reason}")

    # Drop self-hits on audit script docstring examples for unknown/orphan noise when
    # the only issue is documentation — keep real meta failures.
    filtered: list[Hit] = []
    for h in all_hits:
        if h.path.name == "audit_heuristics.py" and h.rule in {
            "bad_marker_form",
            "orphan_marker",
            "unknown_rule",
            "marker_in_literal",
        }:
            continue
        filtered.append(h)

    by_rule = Counter(h.rule for h in filtered)
    files = {h.path for h in filtered}
    print(
        f"{len(filtered)} unjustified hit(s) in {len(files)} file(s); "
        f"{len(all_markers)} audit-ok marker(s)"
    )
    if filtered:
        for h in sorted(filtered, key=lambda x: (_rel(x.path), x.line, x.rule)):
            print(f"  {_rel(h.path)}:{h.line}: [{h.rule}] {h.detail}")
        if by_rule:
            print("--- hits by rule ---")
            for r, n in by_rule.most_common():
                print(f"  {n:4d}  {r}")
        return 1

    if all_markers:
        print("--- audit-ok by rule ---")
        for r, n in Counter(m.rule for m in all_markers).most_common():
            print(f"  {n:4d}  {r}")
        print(f"total audit-ok markers: {len(all_markers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
