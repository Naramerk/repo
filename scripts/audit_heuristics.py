#!/usr/bin/env python3
"""Fail-closed heuristics audit — hand markers only, rule registry.

Marker form (exact)::

    audit-ok[<rule>]: <reason>

Rules:
- Marker suppresses **only** that rule on the covered span.
- ``broad_except`` is suppressed only when the marker is on the ``except`` line.
- Reason text may appear at most 3 times across the scanned set (else error).
- Duplicate markers (same rule on same line) → error.
- Markers inside string/bytes literals → error.
- Orphan (unused) markers → error.
- Malformed ``audit-ok`` → error.

Exit: 0 clean, 1 hits, 2 usage.
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

AUDIT_OK_RE = re.compile(
    r"audit-ok\[(?P<rule>[A-Za-z0-9_]+)\]\s*:\s*(?P<reason>\S.+?)(?=\s*#\s*audit-ok\[|\s*$)"
)
AUDIT_OK_ANY_RE = re.compile(r"audit-ok", re.IGNORECASE)

# --- Rule registry (stable ids) -------------------------------------------------
RULES: dict[str, str] = {
    "or_literal": "BoolOp Or with Constant/empty container RHS",
    "or_name": "BoolOp Or with Name/Attribute RHS",
    "or_call": "BoolOp Or with Call RHS",
    "or_dict": "BoolOp Or with Dict/Set/List/Tuple display RHS",
    "or_fstring": "BoolOp Or with JoinedStr RHS",
    "or_slice": "BoolOp Or with Subscript RHS",
    "or_binop": "BoolOp Or with BinOp RHS",
    "ternary_default": "IfExp invents Constant/empty",
    "ternary_name": "IfExp branch is Name/Call/Attribute",
    "getattr_default": "getattr with default",
    "hasattr": "hasattr(...)",
    "setdefault": "setdefault(...)",
    "dict_get_bare": ".get(k) without default",
    "dict_get_default": ".get(k, default)",
    "dict_pop_default": ".pop(k, default)",
    "next_default": "next(it, default)",
    "contextlib_suppress": "contextlib.suppress(...)",
    "clamp": "max/min with non-zero numeric literal",
    "coerce_bool": "bool(non-literal)",
    "coerce_int": "int(non-literal)",
    "coerce_float": "float(non-literal)",
    "coerce_str": "str(subscript/attribute/get)",
    "str_startswith": ".startswith(...)",
    "str_endswith": ".endswith(...)",
    "str_split": ".split(...)",
    "str_rsplit": ".rsplit(...)",
    "str_partition": ".partition/.rpartition(...)",
    "str_lower": ".lower(...)",
    "str_in_detail": "'x' in detail|reason|mt|route",
    "re_compile": "re.compile(...)",
    "len_one_infer": "len(checks|criteria) == 1",
    "broad_except": "except Exception / bare except",
    "except_pass": "except body is pass",
    "except_silent_return": "return inside except",
    "except_silent_continue": "continue inside except",
    "except_enum_default": "assign str literal in except",
    "assert": "assert in production module",
    "param_default": "function param inventing literal/domain number",
    "field_default": "annotated field inventing literal",
    "named_default": "param/field default is Name (_DEFAULT_*/DEFAULT_*/Enum.X)",
    "field_positional": "Field(<positional literal>)",
    "number_outside_domain": "numeric default outside _DOMAIN_NUMBERS",
    "literal_kwarg": "schema_version|epoch|alpha= literal kwarg",
    "literal_assign": "schema_version|epoch|alpha = literal",
    "schema_version_literal": "schema_version = <digit> (regex)",
    "epoch_literal": "epoch = 0 (regex)",
    "audit_ok_format": "malformed audit-ok marker",
    "orphan_audit_ok": "unused audit-ok marker",
    "duplicate_audit_ok": "duplicate audit-ok[rule] on same line",
    "audit_ok_in_literal": "audit-ok text inside string literal",
    "repeated_reason": "same audit-ok reason text used >3 times",
    "syntax_error": "file failed to parse",
}

_DOMAIN_NUMBERS = frozenset(
    {0, 0.0, 1, 1.0, 2, 3, 8, 10, 30, 32, 64, 0.05, 0.1, 100, 120, 180, 500, 600, 1800, 1_000_000, 1000000}
)

MAX_REASON_REPEATS = 3


def _is_empty_container(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    return False


def _literal_repr(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_literal_repr(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_call_name(node) or '?'}(...)"
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    if isinstance(node, ast.Dict):
        return "{...}" if node.keys else "{}"
    if isinstance(node, ast.List):
        return "[...]" if node.elts else "[]"
    if isinstance(node, ast.BinOp):
        return "BinOp"
    if isinstance(node, ast.Subscript):
        return "Subscript"
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
    markers: list[Marker] = []
    bad: list[Hit] = []
    seen_line_rule: set[tuple[int, str]] = set()
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
            rule = m.group("rule")
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
            if rule not in RULES:
                bad.append(
                    Hit(
                        path=path,
                        line=i,
                        end_line=i,
                        rule="audit_ok_format",
                        detail=f"unknown rule id {rule!r} (not in registry)",
                        text=line.strip()[:200],
                    )
                )
                continue
            key = (i, rule)
            if key in seen_line_rule:
                bad.append(
                    Hit(
                        path=path,
                        line=i,
                        end_line=i,
                        rule="duplicate_audit_ok",
                        detail=f"duplicate audit-ok[{rule}] on line",
                        text=line.strip()[:200],
                    )
                )
                continue
            seen_line_rule.add(key)
            markers.append(
                Marker(path=path, line=i, rule=rule, reason=reason, raw=line.strip()[:200])
            )
    return markers, bad


def _markers_in_literals(path: str, tree: ast.AST, source: str) -> list[Hit]:
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if AUDIT_OK_ANY_RE.search(node.value):
                lo, hi = _node_span(node)
                hits.append(
                    Hit(
                        path=path,
                        line=lo,
                        end_line=hi,
                        rule="audit_ok_in_literal",
                        detail="audit-ok text inside string literal",
                        text=node.value[:80],
                    )
                )
    return hits


class HeuristicVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source: str, markers: list[Marker]) -> None:
        self.path = path
        self.lines = source.splitlines()
        self.markers = markers
        self.hits: list[Hit] = []
        self._used: set[tuple[int, str]] = set()

    def _suppressed(self, rule: str, lo: int, hi: int, *, except_line_only: bool = False) -> bool:
        for mk in self.markers:
            if mk.rule != rule:
                continue
            if except_line_only:
                if mk.line == lo:
                    self._used.add((mk.line, mk.rule))
                    return True
                continue
            if lo <= mk.line <= hi:
                self._used.add((mk.line, mk.rule))
                return True
        return False

    def _add(
        self,
        node: ast.AST,
        rule: str,
        detail: str,
        *,
        except_line_only: bool = False,
    ) -> None:
        lo, hi = _node_span(node)
        if self._suppressed(rule, lo, hi, except_line_only=except_line_only):
            return
        text = self.lines[lo - 1].strip() if 1 <= lo <= len(self.lines) else ""
        self.hits.append(
            Hit(path=self.path, line=lo, end_line=hi, rule=rule, detail=detail, text=text[:200])
        )

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            for v in node.values[1:]:
                if isinstance(v, ast.Constant) and v.value is not None:
                    self._add(v, "or_literal", f"or {_literal_repr(v)}")
                elif _is_empty_container(v):
                    self._add(v, "or_literal", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.Name):
                    self._add(v, "or_name", f"or {v.id}")
                elif isinstance(v, ast.Attribute):
                    self._add(v, "or_name", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.Call):
                    self._add(v, "or_call", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.Dict) or (
                    isinstance(v, (ast.List, ast.Set, ast.Tuple)) and v.elts
                ):
                    self._add(v, "or_dict", f"or {_literal_repr(v)}")
                elif isinstance(v, ast.JoinedStr):
                    self._add(v, "or_fstring", "or f-string")
                elif isinstance(v, ast.Subscript):
                    self._add(v, "or_slice", "or subscript/slice")
                elif isinstance(v, ast.BinOp):
                    self._add(v, "or_binop", "or BinOp")
        self.generic_visit(node)

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

        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if len(node.args) == 1 and not any(k.arg == "default" for k in node.keywords):
                self._add(node, "dict_get_bare", ".get(k) without default")
            elif len(node.args) >= 2:
                self._add(node, "dict_get_default", f".get(..., {_literal_repr(node.args[1])})")

        if isinstance(node.func, ast.Attribute) and node.func.attr == "pop" and len(node.args) >= 2:
            self._add(node, "dict_pop_default", f".pop(..., {_literal_repr(node.args[1])})")

        if name == "next" and len(node.args) >= 2:
            self._add(node, "next_default", "next(it, default)")

        # contextlib.suppress
        if isinstance(node.func, ast.Attribute) and node.func.attr == "suppress":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "contextlib":
                self._add(node, "contextlib_suppress", "contextlib.suppress(...)")
        if name == "suppress":
            self._add(node, "contextlib_suppress", "suppress(...)")

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

        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "startswith",
            "endswith",
            "rsplit",
            "split",
            "partition",
            "rpartition",
            "lower",
        ):
            attr = node.func.attr
            rule = "str_partition" if attr in ("partition", "rpartition") else f"str_{attr}"
            self._add(node, rule, f".{attr}(...)")

        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr == "compile"
        ):
            self._add(node, "re_compile", "re.compile(...)")

        if name == "Field" and node.args:
            a0 = node.args[0]
            if isinstance(a0, (ast.Constant, ast.Name)) or _is_empty_container(a0):
                self._add(node, "field_positional", f"Field({_literal_repr(a0)}, ...)")

        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if (
            len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            right = node.comparators[0]
            rname = right.id if isinstance(right, ast.Name) else (
                right.attr if isinstance(right, ast.Attribute) else None
            )
            if rname in ("detail", "reason", "mt", "route"):
                self._add(node, "str_in_detail", f"{node.left.value!r} in {rname}")
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
        broad = False
        if t is None:
            broad = True
            self._add(node, "broad_except", "except:", except_line_only=True)
        elif isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"):
            broad = True
            self._add(node, "broad_except", f"except {t.id}", except_line_only=True)
        elif isinstance(t, ast.Tuple):
            names = [elt.id for elt in t.elts if isinstance(elt, ast.Name)]
            if any(n in ("Exception", "BaseException") for n in names):
                broad = True
                self._add(
                    node,
                    "broad_except",
                    f"except ({', '.join(names)})",
                    except_line_only=True,
                )

        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self._add(node.body[0], "except_pass", "except ...: pass")

        for stmt in node.body:
            if isinstance(stmt, ast.Return):
                self._add(stmt, "except_silent_return", "return inside except")
            elif isinstance(stmt, ast.Continue):
                self._add(stmt, "except_silent_continue", "continue inside except")
            elif isinstance(stmt, ast.Pass) and not (
                len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            ):
                self._add(stmt, "except_pass", "pass inside except")
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
            for arg, default in zip(args.args[-len(defaults) :], defaults):
                self._flag_default(arg.arg, default, kind="param_default")
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None:
                self._flag_default(arg.arg, default, kind="param_default")

    def _flag_default(self, name: str, default: ast.AST, *, kind: str) -> None:
        if isinstance(default, ast.Name):
            self._add(default, "named_default", f"{name}={default.id}")
            return
        if isinstance(default, ast.Attribute):
            # Enum.X or module.DEFAULT_*
            self._add(default, "named_default", f"{name}={_literal_repr(default)}")
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

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            self.generic_visit(node)
            return
        val = node.value
        if isinstance(val, (ast.Name, ast.Attribute)):
            self._add(node, "named_default", f"field = {_literal_repr(val)}")
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
                        continue
                    self._add(node, "field_default", "Field(default_factory=...)")
                elif kw.arg == "default":
                    self._flag_default("<field>", kw.value, kind="field_default")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg in ("schema_version", "epoch", "alpha", "evalue_alpha"):
            if isinstance(node.value, ast.Constant):
                self._add(node, "literal_kwarg", f"{node.arg}={node.value.value!r}")
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
                    self._add(node, "literal_assign", f"{t.id}={node.value.value!r}")
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
    hits = format_hits + _markers_in_literals(rel, tree, source)
    v = HeuristicVisitor(rel, source, markers)
    v.visit(tree)
    hits.extend(v.hits)
    for rule, pat in _RE_RULES:
        for i, line in enumerate(lines, start=1):
            if not pat.search(line):
                continue
            if any(mk.rule == rule and mk.line == i for mk in markers):
                for mk in markers:
                    if mk.rule == rule and mk.line == i:
                        v._used.add((mk.line, mk.rule))
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
    used = v._used
    for mk in markers:
        if (mk.line, mk.rule) not in used:
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


def _repeated_reason_hits(markers: list[Marker]) -> list[Hit]:
    counts: Counter[str] = Counter(m.reason for m in markers)
    hits: list[Hit] = []
    for reason, n in counts.items():
        if n <= MAX_REASON_REPEATS:
            continue
        # flag every marker with that reason
        for m in markers:
            if m.reason == reason:
                hits.append(
                    Hit(
                        path=m.path,
                        line=m.line,
                        end_line=m.line,
                        rule="repeated_reason",
                        detail=f"reason repeated {n} times (max {MAX_REASON_REPEATS}): {reason[:80]}",
                        text=m.raw,
                    )
                )
    return hits


def count_audit_ok(files: list[Path]) -> Counter[str]:
    c: Counter[str] = Counter()
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            for m in AUDIT_OK_RE.finditer(line):
                c[m.group("rule")] += 1
    return c


def list_markers(files: list[Path]) -> list[Marker]:
    out: list[Marker] = []
    for f in files:
        rel = str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f)
        markers, _ = _collect_markers(rel, f.read_text(encoding="utf-8").splitlines())
        out.extend(markers)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--vs-main", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-rule", action="store_true")
    ap.add_argument("--count-ok", action="store_true")
    ap.add_argument("--list-ok", action="store_true", help="print each marker path:line:rule:reason")
    ap.add_argument("--list-rules", action="store_true", help="print rule registry")
    args = ap.parse_args(argv)

    if args.list_rules:
        for k, v in sorted(RULES.items()):
            print(f"{k}: {v}")
        return 0

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
    all_markers: list[Marker] = []
    for f in uniq:
        hits, markers, _used = audit_file(f)
        all_hits.extend(hits)
        all_markers.extend(markers)
    all_hits.extend(_repeated_reason_hits(all_markers))
    all_hits.sort(key=lambda h: (h.path, h.line, h.rule))

    if args.list_ok:
        for m in sorted(all_markers, key=lambda x: (x.path, x.line, x.rule)):
            print(f"{m.path}:{m.line}: [{m.rule}] {m.reason}")

    if args.json:
        print(json.dumps([asdict(h) for h in all_hits], indent=2, ensure_ascii=False))
    else:
        for h in all_hits:
            span = f"{h.line}" if h.line == h.end_line else f"{h.line}-{h.end_line}"
            print(f"{h.path}:{span}: [{h.rule}] {h.detail}")
        print(
            f"\n{len(all_hits)} unjustified hit(s) in {len(uniq)} file(s); "
            f"{len(all_markers)} audit-ok marker(s)",
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
