#!/usr/bin/env python3
"""Fail-closed heuristics audit (CORE_HEURISTICS_AUDIT.md + review expansions).

Suppression marker (exact form; non-empty reason required)::

    audit-ok[<rule>]: <reason>

A marker suppresses **only** that rule on the covered line range of a hit.
Markers that match no hit in the file are reported as errors (orphan markers).
Multi-line AST nodes are covered by any marker on any line of the node span.

Exit: 0 clean, 1 unjustified hits / orphan markers, 2 usage/IO.

Usage:
  PYTHONPATH=. python scripts/audit_heuristics.py PATH [PATH ...]
  PYTHONPATH=. python scripts/audit_heuristics.py --vs-main
  PYTHONPATH=. python scripts/audit_heuristics.py --vs-main --by-rule
  PYTHONPATH=. python scripts/audit_heuristics.py --vs-main --count-ok
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# audit-ok[rule]: reason  — rule id is [A-Za-z0-9_]+, reason non-empty.
# Multiple markers may appear on one line; reason stops before the next marker.
AUDIT_OK_RE = re.compile(
    r"audit-ok\[(?P<rule>[A-Za-z0-9_]+)\]\s*:\s*(?P<reason>\S.*?)(?=\s*#\s*audit-ok\[|\s*$)",
    re.IGNORECASE,
)
# Legacy / malformed markers (caught as orphan / format errors)
AUDIT_OK_ANY_RE = re.compile(r"audit-ok", re.IGNORECASE)

# Domain numbers used for param_default / field_default of numeric literals.
# Literals **outside** this set are also flagged (named_number_outside_domain).
_DOMAIN_NUMBERS = frozenset(
    {
        0,
        1,
        2,
        3,
        8,
        10,
        30,
        32,
        64,
        0.05,
        0.1,
        100,
        120,
        180,
        500,
        600,
        1800,
        1_000_000,
    }
)


def _is_empty_container(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    return False


def _literal_repr(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.List):
        return "[]"
    if isinstance(node, ast.Dict):
        return "{}"
    if isinstance(node, ast.Tuple):
        return "()"
    if isinstance(node, ast.Set):
        return "set()"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return f"{_call_name(node) or '?'}(...)"
    return type(node).__name__


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _node_span(node: ast.AST) -> tuple[int, int]:
    lo = getattr(node, "lineno", 1) or 1
    hi = getattr(node, "end_lineno", None) or lo
    return lo, hi


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    end_line: int
    rule: str
    detail: str
    text: str


@dataclass(frozen=True)
class Marker:
    path: str
    line: int
    rule: str
    reason: str
    raw: str


def _collect_markers(path: str, lines: list[str]) -> tuple[list[Marker], list[Hit]]:
    """Parse audit-ok markers; malformed → format hits."""
    markers: list[Marker] = []
    bad: list[Hit] = []
    for i, line in enumerate(lines, start=1):
        if not AUDIT_OK_ANY_RE.search(line):
            continue
        found = list(AUDIT_OK_RE.finditer(line))
        if not found:
            bad.append(
                Hit(
                    path=path,
                    line=i,
                    end_line=i,
                    rule="audit_ok_format",
                    detail="audit-ok must be audit-ok[<rule>]: <non-empty reason>",
                    text=line.strip()[:200],
                )
            )
            continue
        for m in found:
            reason = m.group("reason").strip()
            if not reason:
                bad.append(
                    Hit(
                        path=path,
                        line=i,
                        end_line=i,
                        rule="audit_ok_format",
                        detail="audit-ok reason must be non-empty",
                        text=line.strip()[:200],
                    )
                )
                continue
            markers.append(
                Marker(
                    path=path,
                    line=i,
                    rule=m.group("rule"),
                    reason=reason,
                    raw=line.strip()[:200],
                )
            )
    return markers, bad


def _suppressed(markers: list[Marker], rule: str, lo: int, hi: int) -> Marker | None:
    for mk in markers:
        if mk.rule == rule and lo <= mk.line <= hi:
            return mk
    return None


class HeuristicVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source: str, markers: list[Marker]) -> None:
        self.path = path
        self.lines = source.splitlines()
        self.markers = markers
        self.hits: list[Hit] = []
        self._used_markers: set[tuple[int, str]] = set()

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        lo, hi = _node_span(node)
        mk = _suppressed(self.markers, rule, lo, hi)
        if mk is not None:
            self._used_markers.add((mk.line, mk.rule))
            return
        text = self.lines[lo - 1].strip() if 1 <= lo <= len(self.lines) else ""
        self.hits.append(
            Hit(
                path=self.path,
                line=lo,
                end_line=hi,
                rule=rule,
                detail=detail,
                text=text[:200],
            )
        )

    # --- or with literal OR Name|Call (RHS of `or`) ---
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            for v in node.values[1:]:
                if isinstance(v, ast.Constant) and v.value is not None:
                    self._add(v, "or_literal", f"or {_literal_repr(v)}")
                elif _is_empty_container(v):
                    self._add(v, "or_literal", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.Name):
                    self._add(v, "or_name", f"or {v.id}")
                elif isinstance(v, ast.Call):
                    self._add(v, "or_call", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.Attribute):
                    self._add(v, "or_name", f"or {_literal_repr(v)}")
        self.generic_visit(node)

    # ternary inventing literal OR name/call on either branch
    def visit_IfExp(self, node: ast.IfExp) -> None:
        for side in (node.body, node.orelse):
            if isinstance(side, ast.Constant) and side.value is not None:
                self._add(node, "ternary_default", f"ternary invents {_literal_repr(side)}")
                break
            if _is_empty_container(side):
                self._add(node, "ternary_default", f"ternary invents {_literal_repr(side)}")
                break
            if isinstance(side, (ast.Name, ast.Call, ast.Attribute)):
                self._add(node, "ternary_name", f"ternary uses {_literal_repr(side)}")
                break
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)

        if name == "getattr" and len(node.args) >= 3:
            self._add(node, "getattr_default", "getattr(..., default)")
        if name == "hasattr":
            self._add(node, "hasattr", "hasattr(...)")
        if name == "setdefault" or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault"
        ):
            self._add(node, "setdefault", "setdefault(...)")

        # .get(key) without default AND .get(key, default)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if len(node.args) == 1 and not any(k.arg == "default" for k in node.keywords):
                self._add(node, "dict_get_bare", ".get(k) without default")
            elif len(node.args) >= 2:
                default = node.args[1]
                if isinstance(default, ast.Constant) or _is_empty_container(default):
                    self._add(
                        node, "dict_get_default", f".get(..., {_literal_repr(default)})"
                    )
                elif isinstance(default, (ast.Name, ast.Attribute, ast.Call)):
                    self._add(
                        node, "dict_get_default", f".get(..., {_literal_repr(default)})"
                    )
            for kw in node.keywords:
                if kw.arg == "default":
                    self._add(node, "dict_get_default", ".get(..., default=...)")

        if name in ("max", "min"):
            for a in node.args:
                if (
                    isinstance(a, ast.Constant)
                    and isinstance(a.value, (int, float))
                    and a.value not in (0, 0.0)
                ):
                    self._add(node, "clamp", f"{name}(..., {a.value!r}, ...)")
                    break

        if name in ("bool", "int", "float") and node.args:
            if not isinstance(node.args[0], ast.Constant):
                self._add(node, f"coerce_{name}", f"{name}(...) on non-literal")
        if name == "str" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, (ast.Subscript, ast.Attribute)):
                self._add(node, "coerce_str", "str(...) on subscript/attribute")
            elif isinstance(arg0, ast.Call) and _call_name(arg0) in ("get", "getattr"):
                self._add(node, "coerce_str", "str(...get/getattr...)")

        # rule 6: startswith/endswith/lower + split family
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "startswith",
            "endswith",
            "rsplit",
            "split",
            "partition",
            "rpartition",
            "lower",
        ):
            self._add(node, f"str_{node.func.attr}", f".{node.func.attr}(...)")

        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr == "compile"
        ):
            self._add(node, "re_compile", "re.compile(...)")

        # Field(...) positional literal default
        if name == "Field" and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) or _is_empty_container(a0):
                self._add(node, "field_positional", f"Field({_literal_repr(a0)}, ...)")
            elif isinstance(a0, ast.Name):
                self._add(node, "field_positional", f"Field({a0.id}, ...)")

        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # rule 6-ish: `"x" in detail` / `"x" in reason`
        if (
            len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            right = node.comparators[0]
            rname = None
            if isinstance(right, ast.Name):
                rname = right.id
            elif isinstance(right, ast.Attribute):
                rname = right.attr
            if rname in ("detail", "reason", "mt", "route"):
                self._add(node, "str_in_detail", f"{node.left.value!r} in {rname}")
        # len(checks|criteria) == 1
        if (
            isinstance(node.left, ast.Call)
            and _call_name(node.left) == "len"
            and node.left.args
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == 1
        ):
            arg = node.left.args[0]
            aname = arg.id if isinstance(arg, ast.Name) else (
                arg.attr if isinstance(arg, ast.Attribute) else None
            )
            if aname in ("checks", "criteria"):
                self._add(node, "len_one_infer", f"len({aname}) == 1")
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        t = node.type
        if t is None:
            self._add(node, "broad_except", "except:")
        elif isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"):
            self._add(node, "broad_except", f"except {t.id}")
        elif isinstance(t, ast.Tuple):
            names = [elt.id for elt in t.elts if isinstance(elt, ast.Name)]
            if any(n in ("Exception", "BaseException") for n in names):
                self._add(node, "broad_except", f"except ({', '.join(names)})")

        # except: pass / except X: pass
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self._add(node.body[0], "except_pass", "except ...: pass")

        soft = False
        if isinstance(t, ast.Name) and t.id in ("KeyError", "ValueError", "TypeError", "AttributeError"):
            soft = True
        elif isinstance(t, ast.Tuple):
            names = [elt.id for elt in t.elts if isinstance(elt, ast.Name)]
            if any(n in ("KeyError", "ValueError", "TypeError", "AttributeError") for n in names):
                soft = True
        if soft or t is None or (
            isinstance(t, ast.Name) and t.id in ("Exception", "BaseException")
        ):
            for stmt in node.body:
                if isinstance(stmt, ast.Return):
                    self._add(stmt, "except_silent_return", "return inside except")
                elif isinstance(stmt, ast.Continue):
                    self._add(stmt, "except_silent_continue", "continue inside except")
                elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    if isinstance(stmt.value.value, str):
                        self._add(
                            stmt,
                            "except_enum_default",
                            f"assign {stmt.value.value!r} in except",
                        )
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._add(node, "assert", "assert in production module")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_defaults(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_defaults(node)
        self.generic_visit(node)

    def _visit_defaults(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        defaults = list(args.defaults)
        if defaults:
            for arg, default in zip(
                args.args[-len(defaults) :], defaults, strict=True
            ):
                self._flag_default(arg.arg, default, kind="param_default")
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if default is not None:
                self._flag_default(arg.arg, default, kind="param_default")

    def _flag_default(self, name: str, default: ast.AST, *, kind: str) -> None:
        # Named constant defaults (_DEFAULT_*, DEFAULT_*)
        if isinstance(default, ast.Name) and (
            default.id.startswith("_DEFAULT_") or default.id.startswith("DEFAULT_")
        ):
            self._add(default, "named_default", f"{name}={default.id}")
            return
        if isinstance(default, ast.Attribute) and (
            default.attr.startswith("_DEFAULT_") or default.attr.startswith("DEFAULT_")
        ):
            self._add(default, "named_default", f"{name}={default.attr}")
            return
        if isinstance(default, ast.Constant):
            v = default.value
            if v is None:
                return
            if isinstance(v, bool) or isinstance(v, str):
                self._add(default, kind, f"{name}={v!r}")
                return
            if isinstance(v, (int, float)):
                if v in _DOMAIN_NUMBERS:
                    self._add(default, kind, f"{name}={v!r}")
                else:
                    self._add(
                        default,
                        "number_outside_domain",
                        f"{name}={v!r} not in _DOMAIN_NUMBERS",
                    )
                return
        if _is_empty_container(default):
            self._add(default, kind, f"{name}={_literal_repr(default)}")
        elif isinstance(default, (ast.Name, ast.Call, ast.Attribute)):
            # any other named/call default (not None)
            self._add(default, "named_default", f"{name}={_literal_repr(default)}")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            self.generic_visit(node)
            return
        val = node.value
        if isinstance(val, ast.Name) and (
            val.id.startswith("_DEFAULT_") or val.id.startswith("DEFAULT_")
        ):
            self._add(node, "named_default", f"field = {val.id}")
        elif isinstance(val, ast.Constant) and val.value is not None:
            v = val.value
            if isinstance(v, (bool, str)) or _is_empty_container(val):
                self._add(node, "field_default", f"annotated field = {_literal_repr(val)}")
            elif isinstance(v, (int, float)):
                if v in _DOMAIN_NUMBERS:
                    self._add(node, "field_default", f"annotated field = {v!r}")
                else:
                    self._add(
                        node,
                        "number_outside_domain",
                        f"annotated field = {v!r} not in _DOMAIN_NUMBERS",
                    )
        elif _is_empty_container(val):
            self._add(node, "field_default", f"annotated field = {_literal_repr(val)}")
        elif isinstance(val, ast.Call) and _call_name(val) == "Field":
            for kw in val.keywords:
                if kw.arg == "default_factory":
                    if isinstance(kw.value, ast.Name) and kw.value.id in (
                        "list",
                        "dict",
                        "set",
                        "tuple",
                    ):
                        pass  # structural empty — still flag as named if not list/dict?
                        # Monkey: Field(default_factory=list) was previously OK; keep skip
                    else:
                        self._add(node, "field_default", "Field(default_factory=...)")
                elif kw.arg == "default":
                    self._flag_default("<field>", kw.value, kind="field_default")
            # positional already handled in visit_Call
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg in ("schema_version", "epoch", "alpha", "evalue_alpha"):
            if isinstance(node.value, ast.Constant):
                self._add(
                    node, "literal_kwarg", f"{node.arg}={node.value.value!r}"
                )
            elif isinstance(node.value, ast.Name) and (
                node.value.id.startswith("_DEFAULT_")
                or node.value.id.startswith("DEFAULT_")
            ):
                self._add(node, "named_default", f"{node.arg}={node.value.id}")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in (
                "schema_version",
                "epoch",
                "alpha",
                "evalue_alpha",
            ):
                if isinstance(node.value, ast.Constant):
                    self._add(
                        node, "literal_assign", f"{t.id}={node.value.value!r}"
                    )
        self.generic_visit(node)


_RE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("schema_version_literal", re.compile(r"schema_version\s*=\s*\d")),
    ("epoch_literal", re.compile(r"\bepoch\s*=\s*0\b")),
]


def audit_file(path: Path) -> tuple[list[Hit], list[Marker], set[tuple[int, str]]]:
    source = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    lines = source.splitlines()
    markers, format_hits = _collect_markers(rel, lines)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return (
            [
                Hit(
                    path=rel,
                    line=e.lineno or 1,
                    end_line=e.lineno or 1,
                    rule="syntax_error",
                    detail=str(e),
                    text="",
                )
            ]
            + format_hits,
            markers,
            set(),
        )
    v = HeuristicVisitor(rel, source, markers)
    v.visit(tree)
    hits = list(v.hits) + format_hits
    for rule, pat in _RE_RULES:
        for i, line in enumerate(lines, start=1):
            if not pat.search(line):
                continue
            if _suppressed(markers, rule, i, i):
                # mark used
                mk = _suppressed(markers, rule, i, i)
                if mk:
                    v._used_markers.add((mk.line, mk.rule))
                continue
            if any(h.line == i and h.rule.startswith("literal_") for h in hits):
                continue
            hits.append(
                Hit(
                    path=rel,
                    line=i,
                    end_line=i,
                    rule=rule,
                    detail=line.strip()[:120],
                    text=line.strip()[:200],
                )
            )
    used = v._used_markers
    for mk in markers:
        if (mk.line, mk.rule) in used:
            continue
        hits.append(
            Hit(
                path=rel,
                line=mk.line,
                end_line=mk.line,
                rule="orphan_audit_ok",
                detail=f"unused marker audit-ok[{mk.rule}]: {mk.reason}",
                text=mk.raw,
            )
        )
    hits.sort(key=lambda h: (h.path, h.line, h.rule))
    return hits, markers, used


def files_vs_main() -> list[Path]:
    r = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[Path] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("coscientist/") or not line.endswith(".py"):
            continue
        p = ROOT / line
        if p.is_file():
            out.append(p)
    return out


def count_audit_ok(files: list[Path]) -> Counter[str]:
    c: Counter[str] = Counter()
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            m = AUDIT_OK_RE.search(line)
            if m:
                c[m.group("rule")] += 1
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--vs-main", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-rule", action="store_true")
    ap.add_argument(
        "--count-ok",
        action="store_true",
        help="print audit-ok[rule] counts (stderr)",
    )
    args = ap.parse_args(argv)

    files: list[Path] = []
    if args.vs_main:
        files.extend(files_vs_main())
    for p in args.paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
        elif p.is_file():
            files.append(p)
    if not files:
        print("no files to scan", file=sys.stderr)
        return 2

    seen: set[Path] = set()
    uniq: list[Path] = []
    for f in files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(f)

    all_hits: list[Hit] = []
    for f in uniq:
        hits, _markers, _used = audit_file(f)
        all_hits.extend(hits)

    if args.json:
        print(json.dumps([asdict(h) for h in all_hits], indent=2, ensure_ascii=False))
    else:
        for h in all_hits:
            span = f"{h.line}" if h.line == h.end_line else f"{h.line}-{h.end_line}"
            print(f"{h.path}:{span}: [{h.rule}] {h.detail}")
        print(
            f"\n{len(all_hits)} unjustified hit(s) in {len(uniq)} file(s)",
            file=sys.stderr,
        )
    if args.by_rule:
        c = Counter(h.rule for h in all_hits)
        print("--- by rule ---", file=sys.stderr)
        for k, n in c.most_common():
            print(f"{n:5d}  {k}", file=sys.stderr)
        fc = Counter(h.path for h in all_hits)
        print("--- by file ---", file=sys.stderr)
        for k, n in fc.most_common():
            print(f"{n:5d}  {k}", file=sys.stderr)
    if args.count_ok:
        ok = count_audit_ok(uniq)
        print("--- audit-ok by rule ---", file=sys.stderr)
        for k, n in ok.most_common():
            print(f"{n:5d}  {k}", file=sys.stderr)
        print(f"total audit-ok markers: {sum(ok.values())}", file=sys.stderr)

    return 1 if all_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
