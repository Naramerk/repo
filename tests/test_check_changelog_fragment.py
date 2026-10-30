"""Unit tests for scripts/check_changelog_fragment.py (policy + parser + CLI)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_changelog_fragment as gate

STANDARD_TYPES = frozenset({"cleanup", "feat", "fix", "refactor"})

# Minimal pyproject the gate can load types from; committed to the fixture
# repo's base so gate.main() has a config to read.
SEED_PYPROJECT = """\
[tool.towncrier]
directory = "changelog.d"

[[tool.towncrier.type]]
directory = "cleanup"

[[tool.towncrier.type]]
directory = "feat"

[[tool.towncrier.type]]
directory = "fix"

[[tool.towncrier.type]]
directory = "refactor"
"""

SEED_CHANGELOG = "# Changelog\n\n## Unreleased\n"

# Two [[tool.towncrier.type]] entries resolving to the same "feat" value.
FEAT_NAME_DUPLICATED_TOML = """\
[tool.towncrier]

[[tool.towncrier.type]]
name = "feat"

[[tool.towncrier.type]]
name = "feat"
"""

# Full-length blob SHAs as printed by `git diff --raw --no-abbrev`.
SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


def _invalid_name_failure(path: str) -> str:
    return (
        f"invalid fragment name: {path}; expected changelog.d/<topic>.<type>.md"
        " with type one of: cleanup, feat, fix, refactor"
    )


def _scaffolding_mode_failure(path: str, mode: str) -> str:
    return f"changelog.d scaffolding mode must be 100644: {path} has mode {mode}"


def _raw_record(old_mode: str, new_mode: str, status: str, *paths: str) -> str:
    """One NUL-terminated ``git diff --raw -z --no-abbrev`` record.

    With ``-z`` the header and each path are separate NUL-terminated chunks.
    """
    head = f":{old_mode} {new_mode} {SHA_OLD} {SHA_NEW} {status}"
    return "\0".join([head, *paths, ""])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway repo with one committed base state and a ``base`` branch."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "gate-tests@example.com")
    _git(tmp_path, "config", "user.name", "Changelog Gate Tests")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(SEED_CHANGELOG, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(SEED_PYPROJECT, encoding="utf-8")
    _git(tmp_path, "add", "README.md", "CHANGELOG.md", "pyproject.toml")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "branch", "base")
    return tmp_path


def _commit_fragment(repo: Path) -> None:
    fragment = repo / "changelog.d" / "topic-1.cleanup.md"
    fragment.parent.mkdir()
    fragment.write_text("Something changed.\n", encoding="utf-8")
    _git(repo, "add", "changelog.d")
    _git(repo, "commit", "-q", "-m", "fragment")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/topic-1.cleanup.md", "100644")],
            [],
            id="exactly-one-ok",
        ),
        pytest.param(
            [
                gate.DiffRecord("M", "coscientist/cli/run.py", "100644"),
                gate.DiffRecord("A", "tests/test_x.py", "100644"),
            ],
            ["expected exactly one added fragment, found 0"],
            id="no-fragment",
        ),
        pytest.param(
            [
                gate.DiffRecord("A", "changelog.d/one.cleanup.md", "100644"),
                gate.DiffRecord("A", "changelog.d/two.cleanup.md", "100644"),
            ],
            ["expected exactly one added fragment, found 2"],
            id="two-fragments",
        ),
        pytest.param(
            [gate.DiffRecord("M", "changelog.d/tweaked.cleanup.md", "100644")],
            [
                "fragments are add-only: changelog.d/tweaked.cleanup.md has status M",
                "expected exactly one added fragment, found 0",
            ],
            id="modified-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("D", "changelog.d/gone.cleanup.md", "000000")],
            [
                "fragments are add-only: changelog.d/gone.cleanup.md has status D",
                "expected exactly one added fragment, found 0",
            ],
            id="deleted-fragment",
        ),
        pytest.param(
            [
                gate.DiffRecord("R100", "changelog.d/old.cleanup.md", "100644"),
                gate.DiffRecord("R100", "changelog.d/new.cleanup.md", "100644"),
            ],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status R100",
                "fragments are add-only: changelog.d/new.cleanup.md has status R100",
                "expected exactly one added fragment, found 0",
            ],
            id="renamed-fragment",
        ),
        pytest.param(
            [
                gate.DiffRecord("C100", "changelog.d/src.cleanup.md", "100644"),
                gate.DiffRecord("C100", "changelog.d/dst.cleanup.md", "100644"),
            ],
            [
                "fragments are add-only: changelog.d/src.cleanup.md has status C100",
                "fragments are add-only: changelog.d/dst.cleanup.md has status C100",
                "expected exactly one added fragment, found 0",
            ],
            id="copied-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("T", "changelog.d/flipped.cleanup.md", "120000")],
            [
                "fragments are add-only: changelog.d/flipped.cleanup.md has status T",
                "expected exactly one added fragment, found 0",
            ],
            id="typechanged-fragment",
        ),
        pytest.param(
            [
                gate.DiffRecord("A", "changelog.d/topic-1.cleanup.md", "100644"),
                gate.DiffRecord("M", "CHANGELOG.md", "100644"),
            ],
            [
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
            ],
            id="changelog-edit-with-fragment",
        ),
        pytest.param(
            [
                gate.DiffRecord("A", "changelog.d/topic-1.cleanup.md", "100644"),
                gate.DiffRecord("A", "CHANGELOG.md", "100644"),
            ],
            [
                "CHANGELOG.md was edited (A); edit changelog.d/ instead",
            ],
            id="changelog-added",
        ),
        pytest.param(
            [gate.DiffRecord("M", "CHANGELOG.md", "100644")],
            [
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="changelog-modified-only",
        ),
        pytest.param(
            [
                gate.DiffRecord("M", "CHANGELOG.md", "100644"),
                gate.DiffRecord("D", "changelog.d/old.cleanup.md", "000000"),
            ],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status D",
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="build-shaped-changelog-edit",
        ),
        pytest.param(
            [
                gate.DiffRecord("M", "coscientist/cli/run.py", "100644"),
                gate.DiffRecord("D", "changelog.d/foreign.cleanup.md", "000000"),
                gate.DiffRecord("M", "CHANGELOG.md", "100644"),
            ],
            [
                "fragments are add-only: changelog.d/foreign.cleanup.md has status D",
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="deleted-fragment-plus-changelog-edit-plus-code",
        ),
        pytest.param(
            [
                gate.DiffRecord("M", "CHANGELOG.md", "100644"),
                gate.DiffRecord("D", "changelog.d/old.cleanup.md", "000000"),
                gate.DiffRecord("A", "changelog.d/new.cleanup.md", "100644"),
            ],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status D",
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
            ],
            id="changelog-edit-with-deleted-and-added-fragments",
        ),
        pytest.param(
            [
                gate.DiffRecord("M", "CHANGELOG.md", "100644"),
                gate.DiffRecord("D", "changelog.d/old.cleanup.md", "000000"),
                gate.DiffRecord("M", "changelog.d/tweaked.cleanup.md", "100644"),
            ],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status D",
                "fragments are add-only: changelog.d/tweaked.cleanup.md has status M",
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="changelog-edit-with-modified-fragment",
        ),
        pytest.param(
            [
                gate.DiffRecord("M", "CHANGELOG.md", "100644"),
                gate.DiffRecord("D", "changelog.d/old.bogus.md", "000000"),
            ],
            [
                _invalid_name_failure("changelog.d/old.bogus.md"),
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="changelog-edit-with-invalid-deleted-name",
        ),
        pytest.param(
            [gate.DiffRecord("D", "changelog.d/README.md", "000000")],
            [
                "changelog.d scaffolding is add-only: changelog.d/README.md has status D",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-readme-deleted",
        ),
        pytest.param(
            [gate.DiffRecord("M", "changelog.d/README.md", "100644")],
            [
                "changelog.d scaffolding is add-only: changelog.d/README.md has status M",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-readme-modified",
        ),
        pytest.param(
            [gate.DiffRecord("D", "changelog.d/.gitkeep", "000000")],
            [
                "changelog.d scaffolding is add-only: changelog.d/.gitkeep has status D",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-gitkeep-deleted",
        ),
        pytest.param(
            [gate.DiffRecord("M", "changelog.d/.gitkeep", "100644")],
            [
                "changelog.d scaffolding is add-only: changelog.d/.gitkeep has status M",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-gitkeep-modified",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/README.md", "100644")],
            ["expected exactly one added fragment, found 0"],
            id="seed-readme-not-a-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/.gitkeep", "100644")],
            ["expected exactly one added fragment, found 0"],
            id="seed-gitkeep-not-a-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/link.fix.md", "120000")],
            ["fragment mode must be 100644: changelog.d/link.fix.md has mode 120000"],
            id="symlink-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/prog.fix.md", "100755")],
            ["fragment mode must be 100644: changelog.d/prog.fix.md has mode 100755"],
            id="executable-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/dep.fix.md", "160000")],
            ["fragment mode must be 100644: changelog.d/dep.fix.md has mode 160000"],
            id="submodule-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/README.md", "120000")],
            [
                _scaffolding_mode_failure("changelog.d/README.md", "120000"),
                "expected exactly one added fragment, found 0",
            ],
            id="symlink-scaffolding-readme",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/.gitkeep", "100755")],
            [
                _scaffolding_mode_failure("changelog.d/.gitkeep", "100755"),
                "expected exactly one added fragment, found 0",
            ],
            id="executable-scaffolding-gitkeep",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/x.txt", "100644")],
            [
                _invalid_name_failure("changelog.d/x.txt"),
                "expected exactly one added fragment, found 0",
            ],
            id="wrong-extension",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/x.bogus.md", "100644")],
            [
                _invalid_name_failure("changelog.d/x.bogus.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="unknown-type",
        ),
        pytest.param(
            [gate.DiffRecord("M", "changelog.d/x.txt", "100644")],
            [
                _invalid_name_failure("changelog.d/x.txt"),
                "expected exactly one added fragment, found 0",
            ],
            id="wrong-extension-modified",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/x.md", "100644")],
            [
                _invalid_name_failure("changelog.d/x.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="missing-type",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/.cleanup.md", "100644")],
            [
                _invalid_name_failure("changelog.d/.cleanup.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="empty-topic",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/-topic.cleanup.md", "100644")],
            [
                _invalid_name_failure("changelog.d/-topic.cleanup.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="leading-dash-topic",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/Topic.cleanup.md", "100644")],
            [
                _invalid_name_failure("changelog.d/Topic.cleanup.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="uppercase-topic",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/a.fix.1.md", "100644")],
            [
                _invalid_name_failure("changelog.d/a.fix.1.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="counter-suffix-topic",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/a.fix.md.bak", "100644")],
            [
                _invalid_name_failure("changelog.d/a.fix.md.bak"),
                "expected exactly one added fragment, found 0",
            ],
            id="bak-extension",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/sub/x.md", "100644")],
            [
                _invalid_name_failure("changelog.d/sub/x.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="subfolder-fragment",
        ),
        pytest.param(
            [gate.DiffRecord("A", "changelog.d/sub/README.md", "100644")],
            [
                _invalid_name_failure("changelog.d/sub/README.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="subfolder-readme-is-a-violation",
        ),
    ],
)
def test_gate_failures_table(changes: list[gate.DiffRecord], expected: list[str]) -> None:
    assert gate.gate_failures(changes, STANDARD_TYPES) == expected


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("changelog.d/topic-1.cleanup.md", id="dashed-topic"),
        pytest.param("changelog.d/my.topic.fix.md", id="dotted-topic"),
        pytest.param("changelog.d/a.b.fix.md", id="dotted-topic-two-segments"),
        pytest.param("changelog.d/sandbox2.feat.md", id="digit-suffix-topic"),
    ],
)
def test_gate_failures_accepts_valid_fragment_names(path: str) -> None:
    changes = [gate.DiffRecord("A", path, "100644")]
    assert gate.gate_failures(changes, STANDARD_TYPES) == []


def test_gate_failures_uses_only_configured_types() -> None:
    changes = [gate.DiffRecord("A", "changelog.d/topic.cleanup.md", "100644")]
    assert gate.gate_failures(changes, frozenset({"fix"})) == [
        (
            "invalid fragment name: changelog.d/topic.cleanup.md;"
            " expected changelog.d/<topic>.<type>.md with type one of: fix"
        ),
        "expected exactly one added fragment, found 0",
    ]
    assert gate.gate_failures(changes, frozenset({"cleanup"})) == []


def test_parse_raw_diff_single_paths() -> None:
    output = (
        _raw_record("100644", "100644", "A", "changelog.d/topic-1.cleanup.md")
        + _raw_record("100644", "100755", "M", "coscientist/cli/run.py")
        + _raw_record("100644", "000000", "D", "bench/old.py")
    )
    assert gate.parse_raw_diff(output) == [
        gate.DiffRecord("A", "changelog.d/topic-1.cleanup.md", "100644"),
        gate.DiffRecord("M", "coscientist/cli/run.py", "100755"),
        gate.DiffRecord("D", "bench/old.py", "000000"),
    ]


def test_parse_raw_diff_rename_yields_both_paths() -> None:
    output = _raw_record(
        "100755",
        "100644",
        "R100",
        "changelog.d/old.cleanup.md",
        "changelog.d/new.cleanup.md",
    )
    assert gate.parse_raw_diff(output) == [
        gate.DiffRecord("R100", "changelog.d/old.cleanup.md", "100755"),
        gate.DiffRecord("R100", "changelog.d/new.cleanup.md", "100644"),
    ]


def test_parse_raw_diff_empty_output_is_no_changes() -> None:
    assert gate.parse_raw_diff("") == []


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("A\tchangelog.d/x.fix.md", id="missing-trailing-nul"),
        pytest.param(
            f"100644 100644 {SHA_OLD} {SHA_NEW} A\tchangelog.d/x.fix.md\0",
            id="missing-colon-header",
        ),
        pytest.param(
            _raw_record("100644", "100644", "a", "changelog.d/x.fix.md"),
            id="lowercase-status",
        ),
        pytest.param(
            _raw_record("100644", "100644", "R100", "only-one-path"),
            id="rename-without-second-path",
        ),
        pytest.param(
            _raw_record("100644", "100644", "A", ""),
            id="empty-path",
        ),
        pytest.param(
            ":100644 100644 aaaa bbbb A\tchangelog.d/x.fix.md\0",
            id="abbreviated-shas",
        ),
        pytest.param(
            _raw_record("100644", "1006g4", "A", "changelog.d/x.fix.md"),
            id="non-octal-mode",
        ),
    ],
)
def test_parse_raw_diff_rejects_unparseable(output: str) -> None:
    with pytest.raises(gate.ChangelogGateError, match="unparseable"):
        gate.parse_raw_diff(output)


def test_parse_raw_diff_rejects_record_without_path() -> None:
    output = _raw_record("100644", "100644", "A")
    header = f":100644 100644 {SHA_OLD} {SHA_NEW} A"
    expected = f"unparseable 'git diff --raw' record: {header!r} has no path"
    with pytest.raises(gate.ChangelogGateError, match=re.escape(expected)):
        gate.parse_raw_diff(output)


def _write_pyproject(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_fragment_types_reads_directory_and_name_fallback(tmp_path: Path) -> None:
    path = _write_pyproject(
        tmp_path,
        '[tool.towncrier]\n\n[[tool.towncrier.type]]\ndirectory = "cleanup"\n\n'
        '[[tool.towncrier.type]]\nname = "feat"\n',
    )
    assert gate.load_fragment_types(path) == frozenset({"cleanup", "feat"})


def test_load_fragment_types_prefers_directory_over_name(tmp_path: Path) -> None:
    path = _write_pyproject(
        tmp_path,
        '[[tool.towncrier.type]]\ndirectory = "dir-type"\nname = "name-type"\n',
    )
    assert gate.load_fragment_types(path) == frozenset({"dir-type"})


def test_load_fragment_types_fails_closed_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(gate.ChangelogGateError, match="cannot read"):
        gate.load_fragment_types(tmp_path / "absent.toml")


@pytest.mark.parametrize(
    ("type_entries", "duplicate_value"),
    [
        pytest.param(
            '[tool.towncrier]\n\n[[tool.towncrier.type]]\ndirectory = "cleanup"\n\n'
            '[[tool.towncrier.type]]\ndirectory = "cleanup"\n',
            "cleanup",
            id="same-directory-twice",
        ),
        pytest.param(
            '[tool.towncrier]\n\n[[tool.towncrier.type]]\ndirectory = "cleanup"\n\n'
            '[[tool.towncrier.type]]\nname = "cleanup"\n',
            "cleanup",
            id="directory-and-name-resolve-alike",
        ),
        pytest.param(
            FEAT_NAME_DUPLICATED_TOML,
            "feat",
            id="same-name-twice",
        ),
        pytest.param(
            '[tool.towncrier]\n\n[[tool.towncrier.type]]\ndirectory = "cleanup"\n\n'
            '[[tool.towncrier.type]]\ndirectory = "feat"\n\n'
            '[[tool.towncrier.type]]\nname = "feat"\n',
            "feat",
            id="duplicate-after-distinct-entries",
        ),
    ],
)
def test_load_fragment_types_rejects_duplicate_types(
    tmp_path: Path,
    type_entries: str,
    duplicate_value: str,
) -> None:
    expected = f"duplicate [[tool.towncrier.type]] value '{duplicate_value}'"
    with pytest.raises(gate.ChangelogGateError, match=re.escape(expected)):
        gate.load_fragment_types(_write_pyproject(tmp_path, type_entries))


@pytest.mark.parametrize(
    ("text", "expected_message"),
    [
        pytest.param("not [ valid", "cannot read", id="unparsable-toml"),
        pytest.param("", "no [tool] section", id="empty-file"),
        pytest.param("[tool.other]\nx = 1\n", "no [tool.towncrier] section", id="no-towncrier"),
        pytest.param(
            '[tool.towncrier]\ntype = "cleanup"\n',
            "no non-empty [[tool.towncrier.type]] list",
            id="type-not-a-list",
        ),
        pytest.param(
            "[tool.towncrier]\n",
            "no non-empty [[tool.towncrier.type]] list",
            id="type-missing",
        ),
        pytest.param(
            "[tool.towncrier]\ntype = []\n",
            "no non-empty [[tool.towncrier.type]] list",
            id="type-empty",
        ),
        pytest.param(
            "[tool.towncrier]\ntype = [1]\n",
            "entry 0 is not a table",
            id="entry-not-a-table",
        ),
        pytest.param(
            "[tool.towncrier]\n[[tool.towncrier.type]]\nshowcontent = true\n",
            "needs a non-empty 'directory' (or 'name') string",
            id="entry-without-directory-or-name",
        ),
        pytest.param(
            "[tool.towncrier]\n[[tool.towncrier.type]]\ndirectory = 3\n",
            "needs a non-empty 'directory' (or 'name') string",
            id="entry-directory-not-a-string",
        ),
        pytest.param(
            '[tool.towncrier]\n[[tool.towncrier.type]]\ndirectory = ""\n',
            "needs a non-empty 'directory' (or 'name') string",
            id="entry-directory-empty",
        ),
    ],
)
def test_load_fragment_types_fails_closed(tmp_path: Path, text: str, expected_message: str) -> None:
    path = _write_pyproject(tmp_path, text)
    with pytest.raises(gate.ChangelogGateError, match=re.escape(expected_message)):
        gate.load_fragment_types(path)


def test_changed_vs_fails_closed_on_unknown_base(git_repo: Path) -> None:
    with pytest.raises(gate.ChangelogGateError, match="git diff --raw no-such-base"):
        gate.changed_vs("no-such-base", repo_root=git_repo)


def test_head_blob_fails_closed_on_non_utf8_blob(git_repo: Path) -> None:
    fragment = git_repo / "changelog.d" / "binary.cleanup.md"
    fragment.parent.mkdir()
    fragment.write_bytes(b"\xff\xfe binary\n")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "non-utf-8 fragment")
    with pytest.raises(
        gate.ChangelogGateError,
        match=re.escape(
            "git cat-file blob HEAD:changelog.d/binary.cleanup.md is not valid UTF-8: "
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
        ),
    ):
        gate.head_blob("changelog.d/binary.cleanup.md", repo_root=git_repo)


def test_head_blob_fails_closed_when_git_is_missing(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_bin = git_repo / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(
        gate.ChangelogGateError,
        match=re.escape("git not found: [Errno 2] No such file or directory: 'git'"),
    ):
        gate.head_blob("changelog.d/topic-1.cleanup.md", repo_root=git_repo)


def test_changed_vs_fails_closed_when_git_is_missing(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_bin = git_repo / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(
        gate.ChangelogGateError,
        match=re.escape("git not found: [Errno 2] No such file or directory: 'git'"),
    ):
        gate.changed_vs("base", repo_root=git_repo)


def test_changed_vs_reports_modes(git_repo: Path) -> None:
    _commit_fragment(git_repo)
    assert gate.changed_vs("base", repo_root=git_repo) == [
        gate.DiffRecord("A", "changelog.d/topic-1.cleanup.md", "100644"),
    ]


def test_head_blob_reads_committed_bytes(git_repo: Path) -> None:
    _commit_fragment(git_repo)
    fragment = git_repo / "changelog.d" / "topic-1.cleanup.md"
    fragment.write_text("worktree-only edit\n", encoding="utf-8")
    blob = gate.head_blob("changelog.d/topic-1.cleanup.md", repo_root=git_repo)
    assert blob == "Something changed.\n"


def test_head_blob_fails_closed_on_missing_blob(git_repo: Path) -> None:
    with pytest.raises(
        gate.ChangelogGateError,
        match=re.escape("git cat-file blob HEAD:changelog.d/ghost.cleanup.md failed"),
    ):
        gate.head_blob("changelog.d/ghost.cleanup.md", repo_root=git_repo)


def test_main_passes_with_exactly_one_added_fragment(git_repo: Path) -> None:
    _commit_fragment(git_repo)
    assert gate.main(["base"], repo_root=git_repo) == 0


def test_main_passes_when_base_with_foreign_fragment_is_merged(git_repo: Path) -> None:
    # A branch that merges its base — which already carries another author's
    # fragment — must still pass: base...HEAD is judged from the merge-base,
    # so a fragment present on both sides is not an added record here.
    _git(git_repo, "checkout", "-q", "-b", "feature")
    _commit_fragment(git_repo)
    _git(git_repo, "checkout", "-q", "base")
    foreign = git_repo / "changelog.d" / "foreign-999.fix.md"
    foreign.parent.mkdir(exist_ok=True)
    foreign.write_text("Someone else's change.\n", encoding="utf-8")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "foreign fragment")
    _git(git_repo, "checkout", "-q", "feature")
    _git(git_repo, "merge", "-q", "--no-edit", "base")
    assert gate.main(["base"], repo_root=git_repo) == 0


def test_main_judges_content_from_head_not_worktree(git_repo: Path) -> None:
    # A stale or tampered worktree must not influence the gate: the blanked
    # fragment on disk is ignored, the committed bytes are what counts.
    _commit_fragment(git_repo)
    (git_repo / "changelog.d" / "topic-1.cleanup.md").write_text("   \n", encoding="utf-8")
    assert gate.main(["base"], repo_root=git_repo) == 0


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty"),
        pytest.param(" \t\n \n", id="whitespace-only"),
        pytest.param("\u200b\u200c\u200d", id="zero-width-joiner-family-only"),
        pytest.param("\u2060\ufeff", id="word-joiner-and-bom-only"),
        pytest.param("\u00ad\u200b \n\t", id="soft-hyphen-zero-width-whitespace"),
    ],
)
def test_main_rejects_blank_fragment(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    fragment = git_repo / "changelog.d" / "blank.cleanup.md"
    fragment.parent.mkdir()
    fragment.write_text(content, encoding="utf-8")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "blank fragment")
    assert gate.main(["base"], repo_root=git_repo) == 1
    err = capsys.readouterr().err
    assert re.search(re.escape("fragment is empty: changelog.d/blank.cleanup.md"), err)
    assert not re.search(re.escape("fragment mode must be"), err)


def test_main_accepts_visible_content_padded_with_zero_width_chars(
    git_repo: Path,
) -> None:
    # Invisible characters must not shadow real content: only the visible
    # ones count, so text padded with zero-width format characters passes.
    fragment = git_repo / "changelog.d" / "padded.cleanup.md"
    fragment.parent.mkdir()
    fragment.write_text("\u200bSandbox note.\u200b\n", encoding="utf-8")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "padded fragment")
    assert gate.main(["base"], repo_root=git_repo) == 0


def test_main_rejects_symlink_fragment(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The mode-blindness blocker: a symlinked fragment (blob = target path,
    # mode 120000) used to pass; it must be refused by exact mode name.
    link = git_repo / "changelog.d" / "x.fix.md"
    link.parent.mkdir()
    link.symlink_to("../../../etc/hostname")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "symlink fragment")
    assert gate.main(["base"], repo_root=git_repo) == 1
    err = capsys.readouterr().err
    expected = "fragment mode must be 100644: changelog.d/x.fix.md has mode 120000"
    assert re.search(re.escape(expected), err)
    assert "expected exactly one added fragment, found 1" not in err


def test_main_rejects_executable_fragment(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fragment = git_repo / "changelog.d" / "prog.fix.md"
    fragment.parent.mkdir()
    fragment.write_text("Run me.\n", encoding="utf-8")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "update-index", "--chmod=+x", "changelog.d/prog.fix.md")
    _git(git_repo, "commit", "-q", "-m", "executable fragment")
    assert gate.main(["base"], repo_root=git_repo) == 1
    expected = "fragment mode must be 100644: changelog.d/prog.fix.md has mode 100755"
    assert re.search(re.escape(expected), capsys.readouterr().err)


def test_main_rejects_changelog_mode_only_commit(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A pure mode flip still reports M and must fail the gate like any other
    # CHANGELOG.md edit.
    _git(git_repo, "update-index", "--chmod=+x", "CHANGELOG.md")
    _git(git_repo, "commit", "-q", "-m", "mode-only change")
    assert gate.main(["base"], repo_root=git_repo) == 1
    err = capsys.readouterr().err
    assert re.search(re.escape("CHANGELOG.md was edited (M); edit changelog.d/ instead"), err)
    assert re.search(re.escape("expected exactly one added fragment, found 0"), err)


def test_main_rejects_build_shaped_changelog_edit(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The diff `towncrier build` would produce: CHANGELOG.md rendered, every
    # fragment deleted. The diff here is assembled by hand with plain git
    # (no real towncrier is invoked); rendering is a maintainer operation
    # outside this gate, so such a diff must fail here.
    fragment = git_repo / "changelog.d" / "old.cleanup.md"
    fragment.parent.mkdir()
    fragment.write_text("Old entry.\n", encoding="utf-8")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "accumulated fragments")
    _git(git_repo, "branch", "-f", "base", "HEAD")  # base (like main) carries the fragment
    (git_repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.1.0 (2026-10-30)\n\n- Old entry. (old)\n",
        encoding="utf-8",
    )
    _git(git_repo, "rm", "-q", "changelog.d/old.cleanup.md")
    _git(git_repo, "add", "CHANGELOG.md")
    _git(git_repo, "commit", "-q", "-m", "release 0.1.0")
    assert gate.main(["base"], repo_root=git_repo) == 1
    err = capsys.readouterr().err
    assert re.search(re.escape("CHANGELOG.md was edited (M); edit changelog.d/ instead"), err)
    assert re.search(
        re.escape("fragments are add-only: changelog.d/old.cleanup.md has status D"),
        err,
    )
    assert re.search(re.escape("expected exactly one added fragment, found 0"), err)


def test_main_rejects_changelog_edit(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _commit_fragment(git_repo)
    (git_repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\nhand edit\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "CHANGELOG.md")
    _git(git_repo, "commit", "-q", "-m", "edit changelog")
    assert gate.main(["base"], repo_root=git_repo) == 1
    assert "CHANGELOG.md was edited (M)" in capsys.readouterr().err


def test_main_rejects_invalid_fragment_name(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = git_repo / "changelog.d" / "x.txt"
    bad.parent.mkdir()
    bad.write_text("not a fragment\n", encoding="utf-8")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "bad name")
    assert gate.main(["base"], repo_root=git_repo) == 1
    assert "invalid fragment name: changelog.d/x.txt" in capsys.readouterr().err


def test_main_fails_closed_on_broken_pyproject(git_repo: Path) -> None:
    _commit_fragment(git_repo)
    (git_repo / "pyproject.toml").write_text("not [ valid", encoding="utf-8")
    with pytest.raises(gate.ChangelogGateError, match="cannot read"):
        gate.main(["base"], repo_root=git_repo)


def test_main_rejects_missing_fragment(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (git_repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(git_repo, "add", "src.py")
    _git(git_repo, "commit", "-q", "-m", "code only")
    assert gate.main(["base"], repo_root=git_repo) == 1
    assert "found 0" in capsys.readouterr().err


def test_main_propagates_git_failure(git_repo: Path) -> None:
    with pytest.raises(gate.ChangelogGateError, match="no-such-base"):
        gate.main(["no-such-base"], repo_root=git_repo)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-args"),
        pytest.param(["base", "extra"], id="two-args"),
    ],
)
def test_main_usage_refuses_wrong_argc(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def _run_gate_cli(
    repo: Path,
    *argv: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the gate script as a CLI scoped to ``repo`` and nothing else.

    The script is copied under ``repo/scripts/`` so the CLI's module-level
    ``REPO_ROOT`` (the copy's parent directory) resolves to ``repo``: the
    subprocess never reads or judges the real checkout.
    """
    script = repo / "scripts" / "check_changelog_fragment.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(Path(gate.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_cli_exits_nonzero_on_git_failure(git_repo: Path) -> None:
    proc = _run_gate_cli(git_repo, "no-such-ref-anywhere")
    assert proc.returncode == 1
    assert "changelog gate error" in proc.stderr


def test_cli_exits_nonzero_when_git_is_missing(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_bin = git_repo / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    proc = _run_gate_cli(git_repo, "base", env=dict(os.environ))
    assert proc.returncode == 1
    expected = "changelog gate error: git not found: [Errno 2] No such file or directory: 'git'"
    assert re.search(re.escape(expected), proc.stderr)


def test_cli_exits_nonzero_on_non_utf8_fragment(git_repo: Path) -> None:
    fragment = git_repo / "changelog.d" / "binary.cleanup.md"
    fragment.parent.mkdir()
    fragment.write_bytes(b"\xff\xfe binary\n")
    _git(git_repo, "add", "changelog.d")
    _git(git_repo, "commit", "-q", "-m", "non-utf-8 fragment")
    proc = _run_gate_cli(git_repo, "base")
    assert proc.returncode == 1
    expected = (
        "changelog gate error: git cat-file blob HEAD:changelog.d/binary.cleanup.md"
        " is not valid UTF-8: 'utf-8' codec can't decode byte 0xff in position 0:"
        " invalid start byte"
    )
    assert re.search(re.escape(expected), proc.stderr)
