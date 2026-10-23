"""PR19: ingest-gate import-linter coverage + dynamic-import grep."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "coscientist"


def _all_modules() -> set[str]:
    mods: set[str] = set()
    for p in PKG.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if p.name == "__init__.py":
            mod = ".".join(rel.parent.parts)
        else:
            mod = ".".join(rel.with_suffix("").parts)
        if mod and mod != "coscientist":
            mods.add(mod)
    return mods


def _contract(name: str) -> dict:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for c in data["tool"]["importlinter"]["contracts"]:
        if c.get("name") == name:
            return c
    raise AssertionError(f"missing contract {name!r}")


def test_ingest_gate_contract_sources_cover_filesystem():
    """Auto-list vs pyproject: every module except allowed importers is a source."""
    c = _contract("_ingest_gate + user_ingest only via api.studies + lifecycle")
    listed = set(c["source_modules"])
    allowed = {
        "coscientist.core.plan.lifecycle",
        "coscientist.core.execution._ingest_gate",
        "coscientist.core",  # parent overlaps forbidden — excluded from sources
        "coscientist.core.execution",  # parent of _ingest_gate / user_ingest
    }
    expected = _all_modules() - allowed
    missing = expected - listed
    extra = listed - expected
    assert not missing, f"contract missing modules: {sorted(missing)}"
    assert not extra, f"contract lists unknown modules: {sorted(extra)}"
    assert set(c["forbidden_modules"]) == {
        "coscientist.core.execution._ingest_gate",
        "coscientist.core.execution.user_ingest",
    }
    ignores = set(c.get("ignore_imports") or [])
    assert "coscientist.api.studies -> coscientist.core.execution._ingest_gate" in ignores
    assert "coscientist.core.plan.lifecycle -> coscientist.core.execution._ingest_gate" in ignores
    assert "coscientist.core.execution.artifacts -> coscientist.core.execution._ingest_gate" in ignores


def test_tools_must_not_import_api_contract():
    """tools ↛ api (symmetric with agents ↛ api; no ignore on api.runtime→lifecycle)."""
    c = _contract("tools must not import api")
    assert c["source_modules"] == ["coscientist.tools"]
    assert c["forbidden_modules"] == ["coscientist.api"]
    assert not c.get("ignore_imports")


def test_lifecycle_only_via_api_runtime():
    """Only api.runtime may import lifecycle; machine must not."""
    c = _contract("lifecycle only via api.runtime")
    listed = set(c["source_modules"])
    allowed = {
        "coscientist.core.plan.lifecycle",
        "coscientist.api.runtime",
        "coscientist.core",  # parent package
        "coscientist.core.plan",  # parent of lifecycle
    }
    expected = _all_modules() - allowed
    missing = expected - listed
    extra = listed - expected
    assert not missing, f"lifecycle contract missing: {sorted(missing)}"
    assert not extra, f"lifecycle contract unknown: {sorted(extra)}"
    assert "coscientist.core.execution.machine" in listed
    assert "coscientist.core.execution.executor" in listed
    assert "coscientist.agents" in listed
    ignores = set(c.get("ignore_imports") or [])
    assert ignores == {"coscientist.api.runtime -> coscientist.core.plan.lifecycle"}


def test_lifecycle_direct_importers_only_api_runtime():
    """Direct ``lifecycle`` imports: only api.runtime."""
    pat = re.compile(
        r"^\s*(from\s+coscientist\.core\.plan\.lifecycle\s+import|import\s+coscientist\.core\.plan\.lifecycle)\b",
        re.M,
    )
    offenders: list[str] = []
    for p in PKG.rglob("*.py"):
        mod = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        if p.name == "__init__.py":
            mod = ".".join(p.relative_to(ROOT).parent.parts)
        if mod in ("coscientist.core.plan.lifecycle", "coscientist.api.runtime"):
            continue
        text = p.read_text(encoding="utf-8")
        if pat.search(text):
            offenders.append(mod)
    assert offenders == [], f"unexpected direct lifecycle imports: {offenders}"


def test_no_dynamic_import_of_ingest_gate():
    """Grep/AST: no import_module / __import__ of _ingest_gate or user_ingest."""
    banned = (
        "coscientist.core.execution._ingest_gate",
        "coscientist.core.execution.user_ingest",
        "coscientist.core.execution._attempt_frame",
    )
    hits: list[str] = []
    for p in PKG.rglob("*.py"):
        # The gate module itself may mention its name in docs.
        if p.name in ("_ingest_gate.py", "user_ingest.py", "_attempt_frame.py"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = ""
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr
                if name not in ("import_module", "__import__"):
                    continue
                if not node.args:
                    continue
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    if any(b in arg0.value for b in banned):
                        hits.append(f"{p}:{node.lineno}:{arg0.value}")
        # String literals that look like dynamic targets next to importlib.
        text = p.read_text(encoding="utf-8")
        if "importlib" in text or "__import__" in text:
            for b in banned:
                if f'"{b}"' in text or f"'{b}'" in text:
                    # Allow comments / docstrings mentioning the name without call.
                    if re.search(
                        rf"(import_module|_import__)\([^)]*[\"']{re.escape(b)}",
                        text,
                    ):
                        hits.append(f"{p}:literal:{b}")
    assert hits == [], f"dynamic ingest-gate imports: {hits}"


def test_bypass_modules_listed_as_gate_sources():
    """Reviewer bypass paths must be contract sources (caught if they import gate)."""
    c = _contract("_ingest_gate + user_ingest only via api.studies + lifecycle")
    listed = set(c["source_modules"])
    for mod in (
        "coscientist.core.finalize.finalize",
        "coscientist.core.plan.intake",
        "coscientist.core.execution.executor",
        "coscientist.agents",
        "coscientist.core.execution.machine",
    ):
        assert mod in listed, mod


def test_user_ingest_exports_nothing():
    import coscientist.core.execution.user_ingest as ui

    assert ui.__all__ == []
    assert not hasattr(ui, "put_user")
    assert not hasattr(ui, "user_ingest_scope")
