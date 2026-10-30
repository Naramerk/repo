#!/usr/bin/env python3
"""Fail-closed changelog gate: one new fragment per PR, CHANGELOG.md untouched.

Each PR must add exactly one new (Added) file named exactly
``changelog.d/<topic>.<type>.md`` — no subdirectories; the top-level
``README.md`` and ``.gitkeep`` are scaffolding, not fragments — and must not
touch ``CHANGELOG.md``: release notes are rendered into it by ``towncrier
build`` at release time, so hand edits never belong in a feature PR.

The allowed ``<type>`` set is read from ``[[tool.towncrier.type]]`` in
``pyproject.toml`` (each entry contributes its ``directory``, or ``name``
when ``directory`` is absent). An unreadable, unparsable, or typeless
pyproject is an error, never a pass. The one legal ``CHANGELOG.md`` change is
a release diff — ``CHANGELOG.md`` modified, at least one validly named
fragment deleted, nothing added under ``changelog.d/`` and no other fragment
status — which is exactly what ``towncrier build`` produces; it passes without
adding a fragment. Every other ``CHANGELOG.md`` edit fails.

Usage::

    python scripts/check_changelog_fragment.py <base-ref>

Example: ``python scripts/check_changelog_fragment.py origin/main``.
The diff is computed as ``git diff --name-status <base-ref>...HEAD``. Git
failures (unknown ref, missing repository) and unparseable diff records are
errors, never a pass. Exit codes: 0 policy holds, 1 policy violated or
git/config error, 2 usage error.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from collections.abc import Collection, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog.d"
CHANGELOG_PATH = "CHANGELOG.md"
PYPROJECT_NAME = "pyproject.toml"
SEED_DIR_FILES = frozenset({"README.md", ".gitkeep"})
USAGE = "usage: python scripts/check_changelog_fragment.py <base-ref>  (e.g. origin/main)"

# git diff --name-status record: "<status>\t<path>", status possibly carrying
# a similarity score (R100). Anything else is refused (fail-closed).
_RECORD_RE = re.compile(r"(?P<status>[A-Z][0-9]*)\t(?P<paths>.+)")
_SINGLE_PATH_RE = re.compile(r"(?P<path>[^\t]+)")
_RENAMED_PATHS_RE = re.compile(r"(?P<old>[^\t]+)\t(?P<new>[^\t]+)")


class ChangelogGateError(Exception):
    """git/config failed or their output could not be interpreted."""


def parse_name_status(lines: Sequence[str]) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status`` records into (status, path) pairs.

    A record is exactly ``<status>\\t<path>`` or, for rename/copy,
    ``<status>\\t<old>\\t<new>``; both rename paths are kept so a rename on
    either side is judged by the same policy. Every other shape is refused.
    """
    changes: list[tuple[str, str]] = []
    for line in lines:
        record = _RECORD_RE.fullmatch(line)
        if record is None:
            raise ChangelogGateError(f"unparseable 'git diff --name-status' record: {line!r}")
        status = record.group("status")
        paths = record.group("paths")
        renamed = _RENAMED_PATHS_RE.fullmatch(paths)
        if renamed is not None:
            changes.append((status, renamed.group("old")))
            changes.append((status, renamed.group("new")))
            continue
        single = _SINGLE_PATH_RE.fullmatch(paths)
        if single is None:
            raise ChangelogGateError(f"unparseable 'git diff --name-status' record: {line!r}")
        changes.append((status, single.group("path")))
    return changes


def load_fragment_types(pyproject_path: Path) -> frozenset[str]:
    """Return the towncrier fragment types from ``[[tool.towncrier.type]]``.

    Each type entry contributes its ``directory`` (or ``name`` when
    ``directory`` is absent). An unreadable or unparsable pyproject, a missing
    ``[tool.towncrier]`` section, and an absent/empty/badly shaped type list
    all raise ``ChangelogGateError``: the gate never guesses the type set.
    """
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as read_error:
        message = f"cannot read {pyproject_path}: {read_error}"
        raise ChangelogGateError(message) from read_error
    tool = data.get("tool")
    if not isinstance(tool, dict):
        raise ChangelogGateError(f"{pyproject_path}: no [tool] section")
    towncrier = tool.get("towncrier")
    if not isinstance(towncrier, dict):
        raise ChangelogGateError(f"{pyproject_path}: no [tool.towncrier] section")
    type_entries = towncrier.get("type")
    if not isinstance(type_entries, list) or not type_entries:
        message = f"{pyproject_path}: no non-empty [[tool.towncrier.type]] list"
        raise ChangelogGateError(message)
    types: list[str] = []
    for index, entry in enumerate(type_entries):
        if not isinstance(entry, dict):
            message = f"{pyproject_path}: [[tool.towncrier.type]] entry {index} is not a table"
            raise ChangelogGateError(message)
        value = entry.get("directory")
        if value is None:
            value = entry.get("name")
        if not isinstance(value, str) or not value:
            location = f"{pyproject_path}: [[tool.towncrier.type]] entry {index}"
            raise ChangelogGateError(f"{location} needs a non-empty 'directory' (or 'name') string")
        types.append(value)
    return frozenset(types)


def _is_fragment_name(name: str, allowed_types: Collection[str]) -> bool:
    """True iff name is exactly ``<topic>.<type>.md`` with a type from the set.

    ``allowed_types`` is the non-empty set produced by ``load_fragment_types``;
    the topic is non-empty ``[a-z0-9][a-z0-9._-]*`` with no subdirectory.
    """
    types = "|".join(re.escape(fragment_type) for fragment_type in sorted(allowed_types))
    return re.fullmatch(rf"[a-z0-9][a-z0-9._-]*\.({types})\.md", name) is not None


def gate_failures(
    changes: Sequence[tuple[str, str]],
    allowed_types: Collection[str],
) -> list[str]:
    """Return one human-readable violation per problem; empty list = compliant.

    Policy: exactly one Added file whose name is exactly
    ``changelog.d/<topic>.<type>.md`` (type in ``allowed_types``, no
    subdirectories; top-level ``README.md``/``.gitkeep`` are scaffolding), no
    other diff status on fragment files, and ``CHANGELOG.md`` untouched —
    except the release diff described in the module docstring, which passes.
    """
    failures: list[str] = []
    added_fragments: list[str] = []
    deleted_fragments: list[str] = []
    changelog_statuses: list[str] = []
    for status, path in changes:
        if path == CHANGELOG_PATH:
            changelog_statuses.append(status)
            continue
        if not path.startswith(f"{FRAGMENT_DIR}/"):
            continue
        name = path[len(FRAGMENT_DIR) + 1 :]
        if "/" not in name and name in SEED_DIR_FILES:
            continue
        if not _is_fragment_name(name, allowed_types):
            allowed = ", ".join(sorted(allowed_types))
            detail = f"expected {FRAGMENT_DIR}/<topic>.<type>.md with type one of: {allowed}"
            failures.append(f"invalid fragment name: {path}; {detail}")
            continue
        if status == "A":
            added_fragments.append(path)
        elif status == "D":
            deleted_fragments.append(path)
        else:
            failures.append(f"fragments are add-only: {path} has status {status}")
    is_release = (
        bool(changelog_statuses)
        and all(status == "M" for status in changelog_statuses)
        and not added_fragments
        and not failures
        and bool(deleted_fragments)
    )
    if is_release:
        return []
    for status in changelog_statuses:
        failures.append(f"CHANGELOG.md was edited ({status}); edit {FRAGMENT_DIR}/ instead")
    if len(added_fragments) != 1:
        failures.append(f"expected exactly one added fragment, found {len(added_fragments)}")
    return failures


def changed_vs(base_ref: str, repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """Return (status, path) pairs for ``git diff --name-status base...HEAD``."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-status",
            f"{base_ref}...HEAD",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        message = f"git diff --name-status {base_ref}...HEAD failed: {proc.stderr.strip()}"
        raise ChangelogGateError(message)
    return parse_name_status(proc.stdout.splitlines())


def main(argv: Sequence[str], repo_root: Path = REPO_ROOT) -> int:
    """CLI entry: 0 = policy holds, 1 = violated or git/config error, 2 = usage."""
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    allowed_types = load_fragment_types(repo_root / PYPROJECT_NAME)
    failures = gate_failures(changed_vs(argv[0], repo_root), allowed_types)
    for failure in failures:
        print(f"changelog gate: {failure}", file=sys.stderr)
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ChangelogGateError as gate_error:
        print(f"changelog gate error: {gate_error}", file=sys.stderr)
        sys.exit(1)
