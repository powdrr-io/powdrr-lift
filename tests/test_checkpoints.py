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
