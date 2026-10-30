#!/usr/bin/env python3
"""Fail-closed changelog gate: one new fragment per PR, CHANGELOG.md untouched.

Each PR must add exactly one new (Added) file named exactly
``changelog.d/<topic>.<type>.md`` — no subdirectories; the top-level
``README.md`` and ``.gitkeep`` are add-only scaffolding, not fragments — and
must not touch ``CHANGELOG.md`` at all: every diff record on it is a
violation, whatever the status (a mode-only change already reports ``M``).

Every added file under ``changelog.d/`` — fragment or scaffolding — must be
a plain regular file (mode ``100644``); symlinks, executables, and
submodules are refused. The added fragment must carry non-whitespace
content, read from the git object store at ``HEAD``, never from the
worktree.

The allowed ``<type>`` set is read from ``[[tool.towncrier.type]]`` in
``pyproject.toml`` (each entry contributes its ``directory``, or ``name``
when ``directory`` is absent). An unreadable, unparsable, typeless, or
duplicate-valued pyproject is an error, never a pass. Rendering
``CHANGELOG.md`` via ``towncrier build`` is a manual maintainer operation
outside this gate: either committed directly, or landed through a PR whose
gate is expectedly red and merged at the maintainer's discretion.

Usage::

    python scripts/check_changelog_fragment.py <base-ref>

Example: ``python scripts/check_changelog_fragment.py origin/main``.
The diff is computed as ``git diff --raw -z --no-abbrev <base-ref>...HEAD``.
Git failures (unknown ref, missing repository) and unparseable diff records
are errors, never a pass. Exit codes: 0 policy holds, 1 policy violated or
git/config error, 2 usage error.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog.d"
CHANGELOG_PATH = "CHANGELOG.md"
PYPROJECT_NAME = "pyproject.toml"
SEED_DIR_FILES = frozenset({"README.md", ".gitkeep"})
USAGE = "usage: python scripts/check_changelog_fragment.py <base-ref>  (e.g. origin/main)"

# Every added file under changelog.d/ must be a plain regular file, never a
# symlink (120000), an executable (100755), or a submodule (160000).
ADDED_FILE_MODE = "100644"

# git diff --raw --no-abbrev record header: ":<old-mode> <new-mode>
# <old-sha> <new-sha> <status>", status possibly carrying a similarity score
# (R100). The header carries no path: with -z every field group is
# NUL-separated, so the path(s) follow as their own NUL-terminated chunk(s).
# Everything else is refused (fail-closed).
_RAW_RECORD_RE = re.compile(
    r":(?P<old_mode>[0-7]{6}) (?P<new_mode>[0-7]{6}) "
    r"(?P<old_sha>[0-9a-f]{40,64}) (?P<new_sha>[0-9a-f]{40,64}) "
    r"(?P<status>[A-Z][0-9]*)"
)


class ChangelogGateError(Exception):
    """git/config failed or their output could not be interpreted."""


class DiffRecord(NamedTuple):
    """One changed path: status letter(s), path, and the mode at HEAD.

    ``mode`` is the new mode of ``path`` (``000000`` for deletions; for a
    rename/copy the old path carries the old mode and the new path the new).
    """

    status: str
    path: str
    mode: str


def parse_raw_diff(output: str) -> list[DiffRecord]:
    """Parse ``git diff --raw -z --no-abbrev`` output into diff records.

    With ``-z`` every field group is NUL-separated: a header
    ``:<old-mode> <new-mode> <old-sha> <new-sha> <status>`` followed by its
    path (rename and copy records carry a second NUL-terminated path, and
    both paths are kept so a rename on either side is judged by the same
    policy). Every other shape — including output whose records are not
    NUL-terminated — is refused.
    """
    chunks = output.split("\0")
    if chunks[-1] != "":
        message = "unparseable 'git diff --raw' output: records are not NUL-terminated"
        raise ChangelogGateError(message)
    chunks.pop()
    records: list[DiffRecord] = []
    index = 0
    while index < len(chunks):
        record = _RAW_RECORD_RE.fullmatch(chunks[index])
        if record is None:
            raise ChangelogGateError(f"unparseable 'git diff --raw' record: {chunks[index]!r}")
        status = record.group("status")
        path_count = 2 if status[0] in "RC" else 1
        paths = chunks[index + 1 : index + 1 + path_count]
        if len(paths) < path_count:
            detail = "has no second path" if paths else "has no path"
            message = f"unparseable 'git diff --raw' record: {chunks[index]!r} {detail}"
            raise ChangelogGateError(message)
        if any(path == "" for path in paths):
            message = f"unparseable 'git diff --raw' record: empty path after {chunks[index]!r}"
            raise ChangelogGateError(message)
        if path_count == 2:
            records.append(DiffRecord(status, paths[0], record.group("old_mode")))
            records.append(DiffRecord(status, paths[1], record.group("new_mode")))
        else:
            records.append(DiffRecord(status, paths[0], record.group("new_mode")))
        index += 1 + path_count
    return records


def load_fragment_types(pyproject_path: Path) -> frozenset[str]:
    """Return the towncrier fragment types from ``[[tool.towncrier.type]]``.

    Each type entry contributes its ``directory`` (or ``name`` when
    ``directory`` is absent). An unreadable or unparsable pyproject, a missing
    ``[tool.towncrier]`` section, an absent/empty/badly shaped type list, and
    duplicate resolved type values all raise ``ChangelogGateError``: the gate
    never guesses the type set.
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
    seen: set[str] = set()
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
        if value in seen:
            message = f"{pyproject_path}: duplicate [[tool.towncrier.type]] value {value!r}"
            raise ChangelogGateError(message)
        seen.add(value)
        types.append(value)
    return frozenset(types)


def _is_fragment_name(name: str, allowed_types: Collection[str]) -> bool:
    """True iff name is exactly ``<topic>.<type>.md`` with a type from the set.

    ``allowed_types`` is the non-empty set produced by ``load_fragment_types``;
    the topic is non-empty ``[a-z0-9][a-z0-9._-]*`` with no subdirectory.
    towncrier 26.9.0 attributes the same (topic, type) to every name accepted
    here (``_builder.parse_newfragment_basename`` scans the basename from the
    back for the rightmost type segment and takes everything before it as the
    issue), so the gate never passes a name towncrier would parse
    differently; it is only stricter (it refuses e.g. ``a.fix.1.md``, which
    towncrier reads as a counter).
    """
    types = "|".join(re.escape(fragment_type) for fragment_type in sorted(allowed_types))
    return re.fullmatch(rf"[a-z0-9][a-z0-9._-]*\.({types})\.md", name) is not None


def added_fragments(
    changes: Sequence[DiffRecord],
    allowed_types: Collection[str],
) -> list[DiffRecord]:
    """Added records whose name is a valid fragment name, scaffolding aside.

    Mode is not considered: a wrongly-moded fragment still counts as the
    PR's one added fragment; the caller judges its content separately.
    """
    records: list[DiffRecord] = []
    for change in changes:
        if change.status != "A" or not change.path.startswith(f"{FRAGMENT_DIR}/"):
            continue
        name = change.path[len(FRAGMENT_DIR) + 1 :]
        if "/" in name:
            continue
        if _is_fragment_name(name, allowed_types):
            records.append(change)
    return records


def _mode_failure(label: str, path: str, mode: str) -> str:
    """Exact refusal text for an added changelog.d/ file with a bad mode."""
    return f"{label} mode must be {ADDED_FILE_MODE}: {path} has mode {mode}"


def gate_failures(
    changes: Sequence[DiffRecord],
    allowed_types: Collection[str],
) -> list[str]:
    """Return one human-readable violation per problem; empty list = compliant.

    Policy: exactly one Added file whose name is exactly
    ``changelog.d/<topic>.<type>.md`` (type in ``allowed_types``, no
    subdirectories), every Added file under ``changelog.d/`` — fragment or
    scaffolding — a plain regular file with mode 100644 (symlinks,
    executables, and submodules are refused), no non-Added status on
    fragment files or on the top-level ``README.md``/``.gitkeep``
    scaffolding, and ``CHANGELOG.md`` untouched — every diff record on it,
    whatever the status, is a violation.
    """
    failures: list[str] = []
    changelog_statuses: list[str] = []
    for change in changes:
        if change.path == CHANGELOG_PATH:
            changelog_statuses.append(change.status)
            continue
        if not change.path.startswith(f"{FRAGMENT_DIR}/"):
            continue
        name = change.path[len(FRAGMENT_DIR) + 1 :]
        if "/" not in name and name in SEED_DIR_FILES:
            if change.status == "A":
                if change.mode != ADDED_FILE_MODE:
                    failures.append(
                        _mode_failure("changelog.d scaffolding", change.path, change.mode),
                    )
                continue
            note = f"{change.path} has status {change.status}"
            failures.append(f"changelog.d scaffolding is add-only: {note}")
            continue
        if not _is_fragment_name(name, allowed_types):
            allowed = ", ".join(sorted(allowed_types))
            detail = f"expected {FRAGMENT_DIR}/<topic>.<type>.md with type one of: {allowed}"
            failures.append(f"invalid fragment name: {change.path}; {detail}")
            continue
        if change.status == "A":
            if change.mode != ADDED_FILE_MODE:
                failures.append(_mode_failure("fragment", change.path, change.mode))
        else:
            failures.append(f"fragments are add-only: {change.path} has status {change.status}")
    for status in changelog_statuses:
        failures.append(f"CHANGELOG.md was edited ({status}); edit {FRAGMENT_DIR}/ instead")
    added = added_fragments(changes, allowed_types)
    if len(added) != 1:
        failures.append(f"expected exactly one added fragment, found {len(added)}")
    return failures


def head_blob(path: str, repo_root: Path = REPO_ROOT) -> str:
    """Return the content of ``path`` at HEAD, read from the git object store.

    The worktree is never consulted: a symlink or a stale checkout cannot
    make the gate read anything but the committed bytes. A failing
    ``git cat-file`` is an error, never a pass.
    """
    proc = subprocess.run(
        ["git", "cat-file", "blob", f"HEAD:{path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        message = f"git cat-file blob HEAD:{path} failed: {proc.stderr.strip()}"
        raise ChangelogGateError(message)
    return proc.stdout


def changed_vs(base_ref: str, repo_root: Path = REPO_ROOT) -> list[DiffRecord]:
    """Return diff records for ``git diff --raw -z --no-abbrev base...HEAD``."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            f"{base_ref}...HEAD",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        message = f"git diff --raw {base_ref}...HEAD failed: {proc.stderr.strip()}"
        raise ChangelogGateError(message)
    return parse_raw_diff(proc.stdout)


def main(argv: Sequence[str], repo_root: Path = REPO_ROOT) -> int:
    """CLI entry: 0 = policy holds, 1 = violated or git/config error, 2 = usage."""
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    allowed_types = load_fragment_types(repo_root / PYPROJECT_NAME)
    changes = changed_vs(argv[0], repo_root)
    failures = gate_failures(changes, allowed_types)
    for fragment in added_fragments(changes, allowed_types):
        if fragment.mode != ADDED_FILE_MODE:
            continue
        if not head_blob(fragment.path, repo_root).strip():
            failures.append(f"fragment is empty: {fragment.path}")
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
