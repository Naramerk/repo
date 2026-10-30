"""Unit tests for scripts/check_changelog_fragment.py (policy + parser + CLI)."""

from __future__ import annotations

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


def _invalid_name_failure(path: str) -> str:
    return (
        f"invalid fragment name: {path}; expected changelog.d/<topic>.<type>.md"
        " with type one of: cleanup, feat, fix, refactor"
    )


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
            [("A", "changelog.d/topic-1.cleanup.md")],
            [],
            id="exactly-one-ok",
        ),
        pytest.param(
            [("M", "coscientist/cli/run.py"), ("A", "tests/test_x.py")],
            ["expected exactly one added fragment, found 0"],
            id="no-fragment",
        ),
        pytest.param(
            [
                ("A", "changelog.d/one.cleanup.md"),
                ("A", "changelog.d/two.cleanup.md"),
            ],
            ["expected exactly one added fragment, found 2"],
            id="two-fragments",
        ),
        pytest.param(
            [("M", "changelog.d/tweaked.cleanup.md")],
            [
                "fragments are add-only: changelog.d/tweaked.cleanup.md has status M",
                "expected exactly one added fragment, found 0",
            ],
            id="modified-fragment",
        ),
        pytest.param(
            [("D", "changelog.d/gone.cleanup.md")],
            [
                "fragments are add-only: changelog.d/gone.cleanup.md has status D",
                "expected exactly one added fragment, found 0",
            ],
            id="deleted-fragment",
        ),
        pytest.param(
            [
                ("R100", "changelog.d/old.cleanup.md"),
                ("R100", "changelog.d/new.cleanup.md"),
            ],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status R100",
                "fragments are add-only: changelog.d/new.cleanup.md has status R100",
                "expected exactly one added fragment, found 0",
            ],
            id="renamed-fragment",
        ),
        pytest.param(
            [("A", "changelog.d/topic-1.cleanup.md"), ("M", "CHANGELOG.md")],
            [
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
            ],
            id="changelog-edit-with-fragment",
        ),
        pytest.param(
            [("A", "changelog.d/topic-1.cleanup.md"), ("A", "CHANGELOG.md")],
            [
                "CHANGELOG.md was edited (A); edit changelog.d/ instead",
            ],
            id="changelog-added",
        ),
        pytest.param(
            [("M", "CHANGELOG.md")],
            [
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="changelog-modified-only",
        ),
        pytest.param(
            [("M", "CHANGELOG.md"), ("D", "changelog.d/old.cleanup.md")],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status D",
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="exact-towncrier-build-output",
        ),
        pytest.param(
            [
                ("M", "coscientist/cli/run.py"),
                ("D", "changelog.d/foreign.cleanup.md"),
                ("M", "CHANGELOG.md"),
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
                ("M", "CHANGELOG.md"),
                ("D", "changelog.d/old.cleanup.md"),
                ("A", "changelog.d/new.cleanup.md"),
            ],
            [
                "fragments are add-only: changelog.d/old.cleanup.md has status D",
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
            ],
            id="changelog-edit-with-deleted-and-added-fragments",
        ),
        pytest.param(
            [
                ("M", "CHANGELOG.md"),
                ("D", "changelog.d/old.cleanup.md"),
                ("M", "changelog.d/tweaked.cleanup.md"),
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
            [("M", "CHANGELOG.md"), ("D", "changelog.d/old.bogus.md")],
            [
                _invalid_name_failure("changelog.d/old.bogus.md"),
                "CHANGELOG.md was edited (M); edit changelog.d/ instead",
                "expected exactly one added fragment, found 0",
            ],
            id="changelog-edit-with-invalid-deleted-name",
        ),
        pytest.param(
            [("D", "changelog.d/README.md")],
            [
                "changelog.d scaffolding is add-only: changelog.d/README.md has status D",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-readme-deleted",
        ),
        pytest.param(
            [("M", "changelog.d/README.md")],
            [
                "changelog.d scaffolding is add-only: changelog.d/README.md has status M",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-readme-modified",
        ),
        pytest.param(
            [("D", "changelog.d/.gitkeep")],
            [
                "changelog.d scaffolding is add-only: changelog.d/.gitkeep has status D",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-gitkeep-deleted",
        ),
        pytest.param(
            [("M", "changelog.d/.gitkeep")],
            [
                "changelog.d scaffolding is add-only: changelog.d/.gitkeep has status M",
                "expected exactly one added fragment, found 0",
            ],
            id="seed-gitkeep-modified",
        ),
        pytest.param(
            [("A", "changelog.d/README.md")],
            ["expected exactly one added fragment, found 0"],
            id="seed-readme-not-a-fragment",
        ),
        pytest.param(
            [("A", "changelog.d/.gitkeep")],
            ["expected exactly one added fragment, found 0"],
            id="seed-gitkeep-not-a-fragment",
        ),
        pytest.param(
            [("A", "changelog.d/x.txt")],
            [
                _invalid_name_failure("changelog.d/x.txt"),
                "expected exactly one added fragment, found 0",
            ],
            id="wrong-extension",
        ),
        pytest.param(
            [("A", "changelog.d/x.bogus.md")],
            [
                _invalid_name_failure("changelog.d/x.bogus.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="unknown-type",
        ),
        pytest.param(
            [("M", "changelog.d/x.txt")],
            [
                _invalid_name_failure("changelog.d/x.txt"),
                "expected exactly one added fragment, found 0",
            ],
            id="wrong-extension-modified",
        ),
        pytest.param(
            [("A", "changelog.d/x.md")],
            [
                _invalid_name_failure("changelog.d/x.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="missing-type",
        ),
        pytest.param(
            [("A", "changelog.d/.cleanup.md")],
            [
                _invalid_name_failure("changelog.d/.cleanup.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="empty-topic",
        ),
        pytest.param(
            [("A", "changelog.d/-topic.cleanup.md")],
            [
                _invalid_name_failure("changelog.d/-topic.cleanup.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="leading-dash-topic",
        ),
        pytest.param(
            [("A", "changelog.d/Topic.cleanup.md")],
            [
                _invalid_name_failure("changelog.d/Topic.cleanup.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="uppercase-topic",
        ),
        pytest.param(
            [("A", "changelog.d/sub/x.md")],
            [
                _invalid_name_failure("changelog.d/sub/x.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="subfolder-fragment",
        ),
        pytest.param(
            [("A", "changelog.d/sub/README.md")],
            [
                _invalid_name_failure("changelog.d/sub/README.md"),
                "expected exactly one added fragment, found 0",
            ],
            id="subfolder-readme-is-a-violation",
        ),
    ],
)
def test_gate_failures_table(changes: list[tuple[str, str]], expected: list[str]) -> None:
    assert gate.gate_failures(changes, STANDARD_TYPES) == expected


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("changelog.d/topic-1.cleanup.md", id="dashed-topic"),
        pytest.param("changelog.d/my.topic.fix.md", id="dotted-topic"),
        pytest.param("changelog.d/sandbox2.feat.md", id="digit-suffix-topic"),
    ],
)
def test_gate_failures_accepts_valid_fragment_names(path: str) -> None:
    assert gate.gate_failures([("A", path)], STANDARD_TYPES) == []


def test_gate_failures_uses_only_configured_types() -> None:
    changes = [("A", "changelog.d/topic.cleanup.md")]
    assert gate.gate_failures(changes, frozenset({"fix"})) == [
        (
            "invalid fragment name: changelog.d/topic.cleanup.md;"
            " expected changelog.d/<topic>.<type>.md with type one of: fix"
        ),
        "expected exactly one added fragment, found 0",
    ]
    assert gate.gate_failures(changes, frozenset({"cleanup"})) == []


def test_parse_name_status_single_paths() -> None:
    lines = [
        "A\tchangelog.d/topic-1.cleanup.md",
        "M\tcoscientist/cli/run.py",
        "D\tbench/old.py",
    ]
    assert gate.parse_name_status(lines) == [
        ("A", "changelog.d/topic-1.cleanup.md"),
        ("M", "coscientist/cli/run.py"),
        ("D", "bench/old.py"),
    ]


def test_parse_name_status_rename_yields_both_paths() -> None:
    line = "R100\tchangelog.d/old.cleanup.md\tchangelog.d/new.cleanup.md"
    assert gate.parse_name_status([line]) == [
        ("R100", "changelog.d/old.cleanup.md"),
        ("R100", "changelog.d/new.cleanup.md"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("", id="empty-line"),
        pytest.param("just-a-path", id="missing-status"),
        pytest.param("a\trelative-lowercase-status", id="lowercase-status"),
        pytest.param("R100\tone\ttwo\tthree", id="three-paths"),
    ],
)
def test_parse_name_status_rejects_unparseable(line: str) -> None:
    with pytest.raises(gate.ChangelogGateError, match="unparseable"):
        gate.parse_name_status([line])


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
    with pytest.raises(gate.ChangelogGateError, match="git diff --name-status no-such-base"):
        gate.changed_vs("no-such-base", repo_root=git_repo)


def test_main_passes_with_exactly_one_added_fragment(git_repo: Path) -> None:
    _commit_fragment(git_repo)
    assert gate.main(["base"], repo_root=git_repo) == 0


def test_main_rejects_changelog_mode_only_commit(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A pure mode flip still reports M in --name-status and must fail the
    # gate like any other CHANGELOG.md edit.
    _git(git_repo, "update-index", "--chmod=+x", "CHANGELOG.md")
    _git(git_repo, "commit", "-q", "-m", "mode-only change")
    assert gate.main(["base"], repo_root=git_repo) == 1
    err = capsys.readouterr().err
    assert re.search(re.escape("CHANGELOG.md was edited (M); edit changelog.d/ instead"), err)
    assert re.search(re.escape("expected exactly one added fragment, found 0"), err)


def test_main_rejects_exact_towncrier_build_output(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Exactly what `towncrier build` produces: CHANGELOG.md rendered, every
    # fragment deleted. Rendering is a maintainer operation outside this
    # gate, so such a diff must fail here.
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


def test_cli_exits_nonzero_on_git_failure() -> None:
    proc = subprocess.run(
        [sys.executable, str(Path(gate.__file__)), "no-such-ref-anywhere"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "changelog gate error" in proc.stderr
