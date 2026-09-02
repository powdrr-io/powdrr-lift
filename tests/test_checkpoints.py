import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from powdrr_lift.execution.checkpoints import (
    ContentAddressedCheckpointStore,
    run_diagnostics,
)


def test_checkpoint_restores_exact_workspace_and_reuses_objects(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("one", encoding="utf-8")
    store = ContentAddressedCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(workspace, "before-edit")
    (workspace / "one.txt").write_text("changed", encoding="utf-8")
    (workspace / "new.txt").write_text("new", encoding="utf-8")
    store.restore(store.load("before-edit"))
    assert (workspace / "one.txt").read_text(encoding="utf-8") == "one"
    assert not (workspace / "new.txt").exists()
    assert checkpoint.objects


def test_git_checkpoint_records_revision_without_copying_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("before", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "source.txt"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=pytest",
            "-c",
            "user.email=pytest@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=workspace,
        check=True,
    )
    checkpoint_dir = workspace / "execution" / "checkpoints"
    checkpoint = ContentAddressedCheckpointStore(checkpoint_dir).create(
        workspace, "before-edit", state_json='{"should": "not be copied"}'
    )

    assert checkpoint.git_revision
    assert checkpoint.objects == {}
    assert checkpoint.state_ref is None
    object_files = (
        list((checkpoint_dir / "objects").glob("*"))
        if (checkpoint_dir / "objects").exists()
        else []
    )
    assert object_files == []


def test_checkpoint_can_capture_logical_execution_state(tmp_path: Path) -> None:
    store = ContentAddressedCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(tmp_path, "with-state", state_json='{"version": 3}')
    loaded = store.load("with-state")
    assert loaded.state_ref == checkpoint.state_ref
    assert store.load_state_json(loaded) == '{"version": 3}'


def test_checkpoint_restore_with_state_restores_workspace_and_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "state.txt").write_text("before", encoding="utf-8")
    store = ContentAddressedCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(workspace, "restore-with-state", state_json='{"step": 1}')

    (workspace / "state.txt").write_text("after", encoding="utf-8")
    restored_state = store.restore_with_state(checkpoint)

    assert (workspace / "state.txt").read_text(encoding="utf-8") == "before"
    assert restored_state == '{"step": 1}'


def test_checkpoint_restore_rolls_back_when_an_object_is_missing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("before", encoding="utf-8")
    store = ContentAddressedCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(workspace, "atomic-restore")
    (workspace / "one.txt").write_text("after", encoding="utf-8")
    (workspace / "new.txt").write_text("new", encoding="utf-8")
    (store.objects / checkpoint.objects["one.txt"]).unlink()

    with pytest.raises(FileNotFoundError):
        store.restore(checkpoint)

    assert (workspace / "one.txt").read_text(encoding="utf-8") == "after"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new"


def test_checkpoint_restore_rejects_symlink_escape_before_mutating(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "file.txt").write_text("before", encoding="utf-8")
    store = ContentAddressedCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(workspace, "safe-restore")

    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "nested" / "file.txt").unlink()
    (workspace / "nested").rmdir()
    (workspace / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        store.restore(checkpoint)
    assert not (outside / "file.txt").exists()


def test_checkpoint_reports_changed_created_and_deleted_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    deleted = workspace / "deleted.txt"
    source.write_text("before\n", encoding="utf-8")
    deleted.write_text("remove\n", encoding="utf-8")
    store = ContentAddressedCheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(workspace, "before")
    source.write_text("after\n", encoding="utf-8")
    deleted.unlink()
    (workspace / "created.txt").write_text("new\n", encoding="utf-8")

    assert store.changed_paths(checkpoint) == (
        "created.txt",
        "deleted.txt",
        "source.txt",
    )


def test_diagnostics_are_bounded_and_failures_are_evidence(tmp_path: Path) -> None:
    def long(root: Path) -> str:
        return "x" * 20

    def broken(root: Path) -> str:
        raise RuntimeError("diagnostic failed")

    hooks: list[tuple[str, Callable[[Path], str]]] = [
        ("long", long),
        ("broken", broken),
    ]
    results = run_diagnostics(tmp_path, hooks, max_output_chars=8)
    assert results[0].successful and results[0].truncated
    assert results[0].output == "x" * 8
    assert not results[1].successful
