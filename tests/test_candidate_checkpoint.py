from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from powdrr_lift.workrr.candidate_checkpoint import checkpoint_candidate_changes


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "candidate"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / "src").mkdir()
    (root / "src" / "state.py").write_text("state = 1\n", encoding="utf-8")
    (root / "src" / "removed.py").write_text("old = True\n", encoding="utf-8")
    _git(root, "add", "src")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def test_candidate_checkpoints_commit_only_allowed_paths_and_keep_cumulative_diff(
    tmp_path: Path,
) -> None:
    root, submission_base = _repo(tmp_path)
    (root / "src" / "state.py").write_text("state = 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_state.py").write_text(
        "def test_state(): pass\n", encoding="utf-8"
    )
    artifact = root / "docs" / "proposals" / "demo" / "structrr-diff.yaml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("runtime: plan\n", encoding="utf-8")

    first = checkpoint_candidate_changes(
        root,
        submission_base=submission_base,
        allowed_paths=("src", "tests"),
        artifact_exclusions=("docs/proposals/demo/",),
        message="candidate one",
    )
    assert first.committed is True
    assert set(first.committed_paths) == {"src/state.py", "tests/test_state.py"}
    assert first.excluded_paths == ("docs/proposals/demo/structrr-diff.yaml",)

    (root / "src" / "state.py").write_text("state = 3\n", encoding="utf-8")
    (root / "src" / "removed.py").unlink()
    second = checkpoint_candidate_changes(
        root,
        submission_base=submission_base,
        allowed_paths=("src", "tests"),
        artifact_exclusions=("docs/proposals/demo/",),
        message="candidate two",
    )

    assert second.committed is True
    assert set(second.cumulative_paths) == {
        "src/state.py",
        "src/removed.py",
        "tests/test_state.py",
    }
    assert artifact.exists()
    assert set(
        _git(root, "diff", "--name-only", submission_base, "HEAD").splitlines()
    ) == {
        "src/state.py",
        "src/removed.py",
        "tests/test_state.py",
    }


def test_candidate_checkpoint_skips_empty_commits(tmp_path: Path) -> None:
    root, submission_base = _repo(tmp_path)

    checkpoint = checkpoint_candidate_changes(
        root,
        submission_base=submission_base,
        allowed_paths=("src", "tests"),
        artifact_exclusions=("docs/proposals",),
        message="should not be used",
    )

    assert checkpoint.committed is False
    assert checkpoint.candidate_revision == submission_base


def test_candidate_checkpoint_rejects_changes_outside_allowed_paths(
    tmp_path: Path,
) -> None:
    root, submission_base = _repo(tmp_path)
    (root / "unrelated.txt").write_text("out of scope\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside allowed product paths"):
        checkpoint_candidate_changes(
            root,
            submission_base=submission_base,
            allowed_paths=("src", "tests"),
            artifact_exclusions=(),
            message="candidate",
        )


def test_candidate_checkpoint_rejects_pre_staged_artifacts(tmp_path: Path) -> None:
    root, submission_base = _repo(tmp_path)
    artifact = root / "docs" / "proposals" / "demo" / "structrr-diff.yaml"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("plan: true\n", encoding="utf-8")
    _git(root, "add", "docs/proposals/demo/structrr-diff.yaml")

    with pytest.raises(ValueError, match="artifact paths are already staged"):
        checkpoint_candidate_changes(
            root,
            submission_base=submission_base,
            allowed_paths=("src", "tests"),
            artifact_exclusions=("docs/proposals",),
            message="candidate",
        )


def test_candidate_checkpoint_refuses_unrelated_staged_paths(tmp_path: Path) -> None:
    root, submission_base = _repo(tmp_path)
    (root / "src" / "state.py").write_text("state = 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_state.py").write_text(
        "def test_state(): pass\n", encoding="utf-8"
    )
    _git(root, "add", "src/state.py")
    _git(root, "add", "tests/test_state.py")

    checkpoint = checkpoint_candidate_changes(
        root,
        submission_base=submission_base,
        allowed_paths=("src", "tests"),
        artifact_exclusions=(),
        message="explicit staged candidate",
    )

    assert checkpoint.committed is True
    assert set(checkpoint.committed_paths) == {"src/state.py", "tests/test_state.py"}
