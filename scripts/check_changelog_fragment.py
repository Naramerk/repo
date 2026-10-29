#!/usr/bin/env python3
"""Fail-closed changelog gate: one new fragment per PR, CHANGELOG.md untouched.

Each PR must add exactly one new (Added) file under ``changelog.d/`` —
``README.md`` and ``.gitkeep`` are scaffolding, not fragments — and must not
touch ``CHANGELOG.md`` at all: release notes are rendered into it by
``towncrier build`` at release time, so hand edits never belong in a PR.

Usage::

    python scripts/check_changelog_fragment.py <base-ref>

Example: ``python scripts/check_changelog_fragment.py origin/main``.
The diff is computed as ``git diff --name-status <base-ref>...HEAD``. Git
failures (unknown ref, missing repository) and unparseable diff records are
errors, never a pass. Exit codes: 0 policy holds, 1 policy violated or git
error, 2 usage error.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog.d"
CHANGELOG_PATH = "CHANGELOG.md"
SEED_DIR_FILES = frozenset({"README.md", ".gitkeep"})
USAGE = "usage: python scripts/check_changelog_fragment.py <base-ref>  (e.g. origin/main)"

# git diff --name-status record: "<status>\t<path>", status possibly carrying
# a similarity score (R100). Anything else is refused (fail-closed).
_RECORD_RE = re.compile(r"(?P<status>[A-Z][0-9]*)\t(?P<paths>.+)")
_SINGLE_PATH_RE = re.compile(r"(?P<path>[^\t]+)")
_RENAMED_PATHS_RE = re.compile(r"(?P<old>[^\t]+)\t(?P<new>[^\t]+)")


class ChangelogGateError(Exception):
    """git failed or its output could not be parsed."""


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


def gate_failures(changes: Sequence[tuple[str, str]]) -> list[str]:
    """Return one human-readable violation per problem; empty list = compliant.

    Policy: exactly one Added file under ``changelog.d/`` and no diff record
    touches ``CHANGELOG.md``.
    """
    failures: list[str] = []
    added_fragments: list[str] = []
    for status, path in changes:
        if path == CHANGELOG_PATH:
            failures.append(
                f"CHANGELOG.md was edited ({status}); edit {FRAGMENT_DIR}/ instead",
            )
            continue
        if not path.startswith(f"{FRAGMENT_DIR}/"):
            continue
        if Path(path).name in SEED_DIR_FILES:
            continue
        if status == "A":
            added_fragments.append(path)
        else:
            failures.append(f"fragments are add-only: {path} has status {status}")
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
    """CLI entry: 0 = policy holds, 1 = violated or git error, 2 = usage."""
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    failures = gate_failures(changed_vs(argv[0], repo_root))
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
