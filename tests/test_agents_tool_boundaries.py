"""Agents must use invoke_tool; register_tool only in the loader (PR18 B2)."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COSI = ROOT / "coscientist"
TESTS = ROOT / "tests"

# Production modules allowed to *call* register_tool / define it.
_REGISTER_ALLOWED = {
    COSI / "core" / "execution" / "tool_registry.py",
    COSI / "core" / "execution" / "tool_loader.py",
}

# Test-only APIs — must not appear under coscientist/ (except their defining module).
_TEST_ONLY_NAMES = (
    "_reset_registry_for_tests",
    "_register_tool_for_tests",
    "_mint_fetch_capability_for_tests",
)
_TEST_ONLY_DEF_MODULES = {
    "_reset_registry_for_tests": COSI / "core" / "execution" / "tool_registry.py",
    "_register_tool_for_tests": COSI / "core" / "execution" / "tool_registry.py",
    "_mint_fetch_capability_for_tests": COSI / "tools" / "effects.py",
}

# Fetch mint / registry / write-permit: only tools.effects may define these.
_FETCH_MINT_NAMES = (
    "_mint_fetch_capability",
    "_ISSUED_FETCH_CAPS",
    "_IssuedFetchCap",
    "_SOURCE_FETCHED_WRITE_PERMITS",
    "_take_source_fetched_write_permit",
    "_release_source_fetched_write_permit",
    # Removed bool-bypass APIs — must not reappear under coscientist/.
    "_source_fetched_permit_scope",
    "source_fetched_permitted",
    "_allow_source_fetched",
    "allow_source_fetched",
)
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
            mod = "tool_registry" if name in ("_reset_registry_for_tests", "_register_tool_for_tests") else "effects"
            if _imports_name(tree, module_suffix=mod, name=name):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, f"test-only registry helpers in production: {offenders}"


# Bool / scope bypass names must not appear anywhere under coscientist/.
_FETCH_BYPASS_BANNED = (
    "_source_fetched_permit_scope",
    "source_fetched_permitted",
    "_allow_source_fetched",
    "allow_source_fetched",
)


def test_fetch_capability_mint_only_in_effects():
    """Only ``coscientist.tools.effects`` may mint / hold the fetch registry."""
    offenders: list[str] = []
    registry_names = (
        "_mint_fetch_capability",
        "_ISSUED_FETCH_CAPS",
        "_IssuedFetchCap",
        "_SOURCE_FETCHED_WRITE_PERMITS",
        "_take_source_fetched_write_permit",
        "_release_source_fetched_write_permit",
    )
    for path in _py_files(COSI):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _FETCH_BYPASS_BANNED:
            if _calls_name(tree, name) or _names_attr_or_name(tree, name):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
        if path in _FETCH_MINT_ALLOWED:
            continue
        for name in registry_names:
            if _calls_name(tree, name) or _names_attr_or_name(tree, name):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
            if _imports_name(tree, module_suffix="effects", name=name):
                offenders.append(f"{path.relative_to(ROOT)}: import {name}")
            if _imports_name(tree, module_suffix="source_fetch", name=name):
                offenders.append(f"{path.relative_to(ROOT)}: import {name}")
    assert not offenders, f"fetch mint/bypass leaked: {offenders}"


def test_agents_forbid_registry_and_direct_tool_call():
    """``coscientist.agents`` must not import registry / Tool / star-contract or ``Tool.call``.

    ``CallCtx`` comes from ``core.execution.call_ctx`` (not ``tools.contract``).
    The only allowed ``.call`` form is ``self._router.call(...)`` (adapter field —
    not a local name heuristic).
    """
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
        if _imports_name(tree, module_suffix="tools.contract", name="*"):
            offenders.append(f"{path.name}: imports tools.contract *")
        if _imports_name(tree, module_suffix="contract", name="*"):
            offenders.append(f"{path.name}: imports contract *")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "call":
                # Strict: only self._router.call(...)
                base = f.value
                if (
                    isinstance(base, ast.Attribute)
                    and base.attr == "_router"
                    and isinstance(base.value, ast.Name)
                    and base.value.id == "self"
                ):
                    continue
                offenders.append(f"{path.name}: direct .call() (use self._router.call / invoke_tool)")
    assert not offenders, offenders


def test_import_linter_contracts_run():
    """Actually run lint-imports (not merely grep pyproject; no importorskip)."""
    proc = subprocess.run(
        ["lint-imports"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"lint-imports failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    assert "0 broken" in proc.stdout
