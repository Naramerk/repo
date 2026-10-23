"""Agents must use invoke_tool; register_tool only in the loader (PR18 B2)."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COSI = ROOT / "coscientist"
TESTS = ROOT / "tests"

# Production modules allowed to *call* register_tool / define it.
_REGISTER_ALLOWED = {
    COSI / "core" / "tool_registry.py",
    COSI / "core" / "tool_loader.py",
}

# Test-only APIs — must not appear under coscientist/ (except their defining module).
_TEST_ONLY_NAMES = (
    "_reset_registry_for_tests",
    "_register_tool_for_tests",
    "_mint_fetch_capability_for_tests",
)
_TEST_ONLY_DEF_MODULES = {
    "_reset_registry_for_tests": COSI / "core" / "tool_registry.py",
    "_register_tool_for_tests": COSI / "core" / "tool_registry.py",
    "_mint_fetch_capability_for_tests": COSI / "tools" / "effects.py",
}

# Fetch mint / secret: only tools.effects may define or call mint.
_FETCH_MINT_NAMES = ("_mint_fetch_capability", "_FETCH_CAP_SECRET")
_FETCH_MINT_ALLOWED = {COSI / "tools" / "effects.py"}


def _py_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _imports_name(tree: ast.AST, *, module_suffix: str, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == module_suffix or mod.endswith("." + module_suffix):
                for alias in node.names:
                    if alias.name == name or alias.name == "*":
                        return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_suffix or alias.name.endswith("." + module_suffix):
                    return True
    return False


def _calls_name(tree: ast.AST, func_name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == func_name:
            return True
        if isinstance(f, ast.Attribute) and f.attr == func_name:
            return True
    return False


def _names_attr_or_name(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
    return False


def test_register_tool_only_in_loader_and_registry():
    """Forbid ``register_tool(...)`` outside tool_loader / tool_registry."""
    offenders: list[str] = []
    for path in _py_files(COSI):
        if path in _REGISTER_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _calls_name(tree, "register_tool"):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"register_tool called outside loader: {offenders}"


def test_test_only_registry_helpers_banned_outside_tests():
    """Test-only mint/register helpers must not be called outside tests/."""
    offenders: list[str] = []
    for path in _py_files(COSI):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _TEST_ONLY_NAMES:
            if path == _TEST_ONLY_DEF_MODULES[name]:
                continue
            if _calls_name(tree, name):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
                continue
            mod = (
                "tool_registry"
                if name in ("_reset_registry_for_tests", "_register_tool_for_tests")
                else "effects"
            )
            if _imports_name(tree, module_suffix=mod, name=name):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, f"test-only registry helpers in production: {offenders}"


def test_fetch_capability_mint_only_in_effects():
    """Only ``coscientist.tools.effects`` may mint / hold the fetch cap secret."""
    offenders: list[str] = []
    for path in _py_files(COSI):
        if path in _FETCH_MINT_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _FETCH_MINT_NAMES:
            if _calls_name(tree, name) or _names_attr_or_name(tree, name):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
            if _imports_name(tree, module_suffix="effects", name=name):
                offenders.append(f"{path.relative_to(ROOT)}: import {name}")
            if _imports_name(tree, module_suffix="source_fetch", name=name):
                offenders.append(f"{path.relative_to(ROOT)}: import {name}")
    assert not offenders, f"fetch mint leaked outside effects: {offenders}"


def test_agents_forbid_registry_and_direct_tool_call():
    """``coscientist.agents`` must not import registry/contract or call Tool.call."""
    agents = COSI / "agents"
    assert agents.is_dir(), "coscientist.agents package required for agent wiring"
    offenders: list[str] = []
    for path in _py_files(agents):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        if _imports_name(tree, module_suffix="tool_registry", name="register_tool"):
            offenders.append(f"{path.name}: imports register_tool")
        if _imports_name(tree, module_suffix="tool_registry", name="*"):
            offenders.append(f"{path.name}: imports tool_registry")
        if _imports_name(tree, module_suffix="contract", name="Tool"):
            offenders.append(f"{path.name}: imports Tool from contract")
        if _imports_name(tree, module_suffix="tools.contract", name="Tool"):
            offenders.append(f"{path.name}: imports tools.contract.Tool")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "call":
                if isinstance(f.value, ast.Name) and f.value.id == "invoke_tool":
                    continue
                offenders.append(f"{path.name}: direct .call() (use invoke_tool)")
    assert not offenders, offenders


def test_import_linter_contracts_run():
    """Actually run lint-imports (not merely grep pyproject)."""
    pytest.importorskip("importlinter")
    proc = subprocess.run(
        ["lint-imports"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "KEPT" in proc.stdout or "Contracts:" in proc.stdout
