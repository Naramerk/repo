#!/usr/bin/env python3
"""Fail-closed heuristics audit (CORE_HEURISTICS_AUDIT.md + review expansions).

Implements grep/AST rules that invent defaults, coerce external data, or fail
open. A hit on a line is suppressed only when that line contains:

    audit-ok: <reason>

Exit codes: 0 = clean, 1 = unjustified hits, 2 = usage/IO error.

Usage:
  PYTHONPATH=. python scripts/audit_heuristics.py PATH [PATH ...]
  PYTHONPATH=. python scripts/audit_heuristics.py --vs-main
  PYTHONPATH=. python scripts/audit_heuristics.py --vs-main --by-rule
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
AUDIT_OK_RE = re.compile(r"audit-ok\s*:", re.IGNORECASE)

# Domain-ish defaults that invent study/policy numbers (rule: signature/field defaults).
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
        120,
        180,
        600,
        1800,
    }
)


def _is_empty_container(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return not node.elts
    return False


def _is_invented_literal(node: ast.AST) -> bool:
    """Literal used as a silent default (not None — None often means optional)."""
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None:
            return False
        if isinstance(v, bool):
            return True
        if isinstance(v, (int, float)):
            return True
        if isinstance(v, str):
            return True
        return False
    return _is_empty_container(node)


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
    return type(node).__name__


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    detail: str
    text: str


def _line_has_audit_ok(lines: list[str], lineno: int) -> bool:
    if lineno < 1 or lineno > len(lines):
        return False
    return AUDIT_OK_RE.search(lines[lineno - 1]) is not None


class HeuristicVisitor(ast.NodeVisitor):
    """AST rules aligned with CORE_HEURISTICS_AUDIT.md §grep + user expansions."""

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.lines = source.splitlines()
        self.hits: list[Hit] = []
        self._except_depth = 0

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        lineno = getattr(node, "lineno", 1) or 1
        if _line_has_audit_ok(self.lines, lineno):
            return
        text = self.lines[lineno - 1].strip() if 1 <= lineno <= len(self.lines) else ""
        self.hits.append(
            Hit(path=self.path, line=lineno, rule=rule, detail=detail, text=text[:200])
        )

    # 4. or with invented literal (not `x or y` both names)
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            for v in node.values[1:]:
                if _is_invented_literal(v):
                    self._add(v, "or_literal", f"or {_literal_repr(v)}")
        self.generic_visit(node)

    # ternary inventing a default on either branch
    def visit_IfExp(self, node: ast.IfExp) -> None:
        for side in (node.body, node.orelse):
            if _is_invented_literal(side):
                self._add(node, "ternary_default", f"ternary invents {_literal_repr(side)}")
                break
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)

        # 4. getattr with default
        if name == "getattr" and len(node.args) >= 3:
            self._add(node, "getattr_default", "getattr(..., default)")
        # hasattr
        if name == "hasattr":
            self._add(node, "hasattr", "hasattr(...)")
        # 1. setdefault
        if name == "setdefault":
            self._add(node, "setdefault", "setdefault(...)")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
            self._add(node, "setdefault", ".setdefault(...)")

        # 3. .get(key, default) with invented default or enum-member fallback (rule 8)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and len(node.args) >= 2:
            default = node.args[1]
            if _is_invented_literal(default):
                self._add(node, "dict_get_default", f".get(..., {_literal_repr(default)})")
            elif isinstance(default, ast.Attribute):
                self._add(node, "dict_get_enum_fallback", ".get(..., Enum.member)")

        # 5. clamps — CORE_HEURISTICS allows max(0.0, …) for remaining time/budget;
        # flag only positive domain floors (max(1,…), max(2,…), min(1,…), etc.).
        if name in ("max", "min"):
            for a in node.args:
                if (
                    isinstance(a, ast.Constant)
                    and isinstance(a.value, (int, float))
                    and a.value not in (0, 0.0)
                ):
                    self._add(node, "clamp", f"{name}(..., {a.value!r}, ...)")
                    break

        # coercion bool/str/int/float on non-literals (external-ish)
        if name in ("bool", "int", "float") and node.args:
            arg0 = node.args[0]
            if not isinstance(arg0, ast.Constant):
                self._add(node, f"coerce_{name}", f"{name}(...) on non-literal")
        # str() is ubiquitous for serialization — only flag str(x or …) via BoolOp child;
        # flag str() when wrapping Subscript (row["x"]) or .get Call
        if name == "str" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, (ast.Subscript, ast.Attribute)):
                self._add(node, "coerce_str", "str(...) on subscript/attribute")
            elif isinstance(arg0, ast.Call) and _call_name(arg0) in ("get", "getattr"):
                self._add(node, "coerce_str", "str(...get/getattr...)")

        # 6 / expansion: startswith/endswith/rsplit/split/partition on dynamic values
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

        # 10. re.compile
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id == "re" and node.func.attr == "compile":
                self._add(node, "re_compile", "re.compile(...)")
        if isinstance(node.func, ast.Name) and node.func.id == "compile":
            # skip builtin compile
            pass

        # len(checks|criteria) == 1 handled in Compare

        self.generic_visit(node)

    # 9. len(checks|criteria) == 1
    def visit_Compare(self, node: ast.Compare) -> None:
        if (
            isinstance(node.left, ast.Call)
            and _call_name(node.left) == "len"
            and node.left.args
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == 1
        ):
            arg = node.left.args[0]
            aname = None
            if isinstance(arg, ast.Name):
                aname = arg.id
            elif isinstance(arg, ast.Attribute):
                aname = arg.attr
            if aname in ("checks", "criteria"):
                self._add(node, "len_one_infer", f"len({aname}) == 1")
        self.generic_visit(node)

    # 7. broad except + silent return/assign inside except KeyError|ValueError
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        t = node.type
        broad = False
        soft = False
        if t is None:
            broad = True
            self._add(node, "broad_except", "except:")
        elif isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"):
            broad = True
            self._add(node, "broad_except", f"except {t.id}")
        elif isinstance(t, ast.Name) and t.id in ("KeyError", "ValueError", "TypeError"):
            soft = True
        elif isinstance(t, ast.Tuple):
            names = [elt.id for elt in t.elts if isinstance(elt, ast.Name)]
            if any(n in ("Exception", "BaseException") for n in names):
                broad = True
                self._add(node, "broad_except", f"except ({', '.join(names)})")
            if any(n in ("KeyError", "ValueError", "TypeError") for n in names):
                soft = True

        if soft or broad:
            for stmt in node.body:
                if isinstance(stmt, ast.Return):
                    self._add(stmt, "except_silent_return", "return inside except")
                elif isinstance(stmt, ast.Continue):
                    self._add(stmt, "except_silent_continue", "continue inside except")
                elif isinstance(stmt, ast.Assign):
                    # except ...: x = "inconclusive"
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                        stmt.value.value, str
                    ):
                        self._add(
                            stmt,
                            "except_enum_default",
                            f"assign {stmt.value.value!r} in except",
                        )
        self._except_depth += 1
        self.generic_visit(node)
        self._except_depth -= 1

    # assert in production
    def visit_Assert(self, node: ast.Assert) -> None:
        self._add(node, "assert", "assert in production module")
        self.generic_visit(node)

    # silent continue outside except still flagged when skipping after soft checks —
    # keep only continue immediately after soft dict/get patterns via parent? Skip
    # blanket continue to avoid noise; except_silent_continue covers the review case.

    # signature defaults that invent domain numbers / empty containers / bools / strs
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
            for arg, default in zip(args.args[-len(defaults) :], defaults, strict=True):
                self._flag_param_default(arg.arg, default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if default is not None:
                self._flag_param_default(arg.arg, default)

    def _flag_param_default(self, name: str, default: ast.AST) -> None:
        if isinstance(default, ast.Constant):
            v = default.value
            if v is None:
                return
            if isinstance(v, bool) or isinstance(v, str) or _is_empty_container(default):
                self._add(default, "param_default", f"{name}={v!r}")
                return
            if isinstance(v, (int, float)) and v in _DOMAIN_NUMBERS:
                self._add(default, "param_default", f"{name}={v!r}")
                return
        if _is_empty_container(default):
            self._add(default, "param_default", f"{name}={_literal_repr(default)}")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            self.generic_visit(node)
            return
        if _is_invented_literal(node.value):
            # class field defaults (Study.alpha = 0.05 etc.)
            self._add(
                node, "field_default", f"annotated field = {_literal_repr(node.value)}"
            )
        elif isinstance(node.value, ast.Call):
            fname = _call_name(node.value)
            if fname == "Field":
                for kw in node.value.keywords:
                    # Structural empty default_factory (list/dict/set) ≡ «поля не было»
                    # per CORE_HEURISTICS_AUDIT — not an invented semantic default.
                    if kw.arg == "default_factory":
                        if isinstance(kw.value, ast.Name) and kw.value.id in (
                            "list",
                            "dict",
                            "set",
                            "tuple",
                        ):
                            continue
                        self._add(node, "field_default", "Field(default_factory=...)")
                    elif kw.arg == "default" and _is_invented_literal(kw.value):
                        self._add(node, "field_default", "Field(default=...)")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        # 2 / 7: schema_version=N, epoch=0, alpha=0.05 kwargs
        if node.arg in ("schema_version", "epoch", "alpha", "evalue_alpha"):
            if isinstance(node.value, ast.Constant):
                self._add(
                    node,
                    "literal_kwarg",
                    f"{node.arg}={node.value.value!r}",
                )
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
                        node,
                        "literal_assign",
                        f"{t.id}={node.value.value!r}",
                    )
        self.generic_visit(node)


_RE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("schema_version_literal", re.compile(r"schema_version\s*=\s*\d")),
    ("epoch_literal", re.compile(r"\bepoch\s*=\s*0\b")),
]


def audit_file(path: Path) -> list[Hit]:
    source = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [
            Hit(
                path=rel,
                line=e.lineno or 1,
                rule="syntax_error",
                detail=str(e),
                text="",
            )
        ]
    v = HeuristicVisitor(rel, source)
    v.visit(tree)
    hits = list(v.hits)
    lines = source.splitlines()
    existing = {(h.line, h.rule) for h in hits}
    for rule, pat in _RE_RULES:
        for i, line in enumerate(lines, start=1):
            if _line_has_audit_ok(lines, i):
                continue
            if not pat.search(line):
                continue
            key = (i, rule)
            if key in existing:
                continue
            # skip if AST already raised literal_kwarg/assign on same line
            if any(h.line == i and h.rule.startswith("literal_") for h in hits):
                continue
            hits.append(
                Hit(
                    path=rel,
                    line=i,
                    rule=rule,
                    detail=line.strip()[:120],
                    text=line.strip()[:200],
                )
            )
    hits.sort(key=lambda h: (h.path, h.line, h.rule))
    return hits


def files_vs_main() -> list[Path]:
    r = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD", "--", "coscientist/**/*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        # fallback without pathspec magic
        r = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--vs-main", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-rule", action="store_true")
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
        all_hits.extend(audit_file(f))

    if args.json:
        print(json.dumps([asdict(h) for h in all_hits], indent=2, ensure_ascii=False))
    else:
        for h in all_hits:
            print(f"{h.path}:{h.line}: [{h.rule}] {h.detail}")
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

    return 1 if all_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
