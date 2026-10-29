"""Unit tests for scripts/check_changelog_fragment.py (policy + parser + CLI)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_changelog_fragment as gate


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
    _git(tmp_path, "add", "README.md")
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
            id="changelog-edited",
        ),
        pytest.param(
            [("A", "changelog.d/README.md")],
            ["expected exactly one added fragment, found 0"],
            id="seed-readme-not-a-fragment",
        ),
    ],
)
def test_gate_failures_table(changes: list[tuple[str, str]], expected: list[str]) -> None:
    assert gate.gate_failures(changes) == expected


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


def test_changed_vs_fails_closed_on_unknown_base(git_repo: Path) -> None:
    with pytest.raises(gate.ChangelogGateError, match="git diff --name-status no-such-base"):
        gate.changed_vs("no-such-base", repo_root=git_repo)


def test_main_passes_with_exactly_one_added_fragment(git_repo: Path) -> None:
    _commit_fragment(git_repo)
    assert gate.main(["base"], repo_root=git_repo) == 0


def test_main_rejects_changelog_edit(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _commit_fragment(git_repo)
    (git_repo / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
    _git(git_repo, "add", "CHANGELOG.md")
    _git(git_repo, "commit", "-q", "-m", "edit changelog")
    assert gate.main(["base"], repo_root=git_repo) == 1
    assert "CHANGELOG.md was edited (A)" in capsys.readouterr().err


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
