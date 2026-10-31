from __future__ import annotations

import multiprocessing
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from filelock import Timeout
from powdrr_local.file_lock import file_lock

from powdrr_lift.workrr.github_issue_queue import GitHubIssueQueueError, _worker_lock


def _hold_file_lock(path: str, acquired: Any, release: Any) -> None:
    with file_lock(path):
        acquired.set()
        release.wait()


def test_affected_workflow_modules_import_portably() -> None:
    for module_name in (
        "powdrr_lift.core.behavior_rule",
        "powdrr_lift.execution.store",
        "powdrr_lift.structrr.intent",
        "powdrr_lift.workrr.github_issue_queue",
        "powdrr_local.planning.checkpoints",
    ):
        import_module(module_name)


def test_file_lock_excludes_processes_and_releases_after_process_exit(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_file_lock,
        args=(str(tmp_path / "worker.lock"), acquired, release),
    )
    process.start()
    try:
        assert acquired.wait(timeout=10)
        with pytest.raises(Timeout):
            with file_lock(tmp_path / "worker.lock", blocking=False):
                pytest.fail("a second process acquired an occupied lock")
    finally:
        process.terminate()
        process.join(timeout=10)

    assert process.exitcode is not None
    with file_lock(tmp_path / "worker.lock", blocking=False):
        pass


def test_issue_worker_lock_keeps_nonblocking_busy_error(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", str(repository)], check=True, capture_output=True, text=True
    )

    with _worker_lock(repository):
        with pytest.raises(GitHubIssueQueueError, match="another issue worker"):
            with _worker_lock(repository):
                pytest.fail("a second issue worker acquired the lock")
