"""Agents must use invoke_tool; register_tool only in the loader (PR18 B2)."""

from __future__ import annotations

import ast
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

# Test-only APIs — must not appear under coscientist/.
_TEST_ONLY_NAMES = ("_reset_registry_for_tests", "_register_tool_for_tests")


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
    """``_reset_registry_for_tests`` / ``_register_tool_for_tests`` only in tests/."""
    offenders: list[str] = []
    for path in _py_files(COSI):
        # Definitions live in tool_registry; everything else under coscientist is banned.
        if path == COSI / "core" / "tool_registry.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _TEST_ONLY_NAMES:
            if _calls_name(tree, name) or _imports_name(
                tree, module_suffix="tool_registry", name=name
            ):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, f"test-only registry helpers in production: {offenders}"


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


def test_import_linter_agents_contract():
    """lint-imports must include the agents invoke_tool contract."""
    pytest.importorskip("importlinter")
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "agents must use invoke_tool" in text
    assert "coscientist.agents" in text
    assert "coscientist.core.tool_registry" in text
