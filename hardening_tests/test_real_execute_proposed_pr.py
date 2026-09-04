from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO, cast

import pytest

from powdrr_lift.core import WorkflowInstance, save_workflow_task
from powdrr_lift.core.workflow_template_specification import (
    instantiate_workflow_template,
)
from powdrr_lift.workflow_task_agent import (
    WorkflowTaskAgentConfig,
    _build_workflow_client,
    run_workflow_task,
)
from powdrr_lift.workflow_task_scenario import LiveWorkflowTaskExchangeRecorder


class _TeeTextIO(io.TextIOBase):
    def __init__(self, capture: io.StringIO, display: Any) -> None:
        self.capture = capture
        self.display = display

    def write(self, value: str) -> int:
        self.capture.write(value)
        written = self.display.write(value)
        self.display.flush()
        return written

    def flush(self) -> None:
        self.capture.flush()
        self.display.flush()


class _LoggingLiveRecorder(LiveWorkflowTaskExchangeRecorder):
    def __init__(self, client: Any, log: Callable[[str], None]) -> None:
        super().__init__(client)
        self._log = log
        self._roundtrip = 0

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self._roundtrip += 1
        self._log(f"[coding-loop] LLM roundtrip {self._roundtrip}: request sent")
        try:
            response = super().complete_json(messages)
        except Exception as error:
            self._log(
                f"[coding-loop] LLM roundtrip {self._roundtrip}: "
                f"transport error {type(error).__name__}: {error}"
            )
            raise
        self._log(
            f"[coding-loop] LLM roundtrip {self._roundtrip}: "
            f"action={response.get('action', '<missing>')}"
        )
        return response


def test_real_coding_loop_implements_substantial_change_in_repo_copy(
    tmp_path: Path,
) -> None:
    """Run the execute-proposed-pr coding task against a copied repository."""
    if os.environ.get("POWDRR_LIFT_RUN_LIVE_CODING_LOOP") != "1":
        pytest.skip("set POWDRR_LIFT_RUN_LIVE_CODING_LOOP=1 to run the live harness")
    api_key = os.environ.get("DEEPINFRA_API_TOKEN") or os.environ.get(
        "DEEPINFRA_API_KEY"
    )
    if not api_key:
        pytest.skip(
            "set DEEPINFRA_API_TOKEN or DEEPINFRA_API_KEY to run the live harness"
        )

    source_repo = Path(__file__).parents[1]
    repository = tmp_path / "repository-copy"
    shutil.copytree(
        source_repo,
        repository,
        ignore=shutil.ignore_patterns(
            ".git",
            ".worktrees",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            "*.pyc",
        ),
    )
    _initialize_git_repository(repository)
    (repository / "src" / "priority_queue.py").write_text(
        "class PriorityQueue:\n"
        "    def __init__(self) -> None:\n"
        "        self._items = []\n\n"
        "    def push(self, item: object, priority: int) -> None:\n"
        "        raise NotImplementedError\n\n"
        "    def peek(self) -> object:\n"
        "        raise NotImplementedError\n\n"
        "    def pop(self) -> object:\n"
        "        raise NotImplementedError\n\n"
        "    def empty(self) -> bool:\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_priority_queue.py").write_text(
        "from src.priority_queue import PriorityQueue\n\n\n"
        "def test_priority_queue_orders_items_and_preserves_ties() -> None:\n"
        "    queue = PriorityQueue()\n"
        '    queue.push("low", 1)\n'
        '    queue.push("first", 3)\n'
        '    queue.push("second", 3)\n'
        '    assert queue.peek() == "first"\n'
        '    assert queue.pop() == "first"\n'
        '    assert queue.pop() == "second"\n'
        '    assert queue.pop() == "low"\n'
        "    assert queue.empty()\n\n\n"
        "def test_priority_queue_rejects_pop_from_empty_queue() -> None:\n"
        "    with pytest.raises(IndexError):\n"
        "        PriorityQueue().pop()\n",
        encoding="utf-8",
    )
    test_file = repository / "tests" / "test_priority_queue.py"
    test_file.write_text(
        "import pytest\n\n" + test_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_file)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert baseline.returncode != 0, baseline.stdout + baseline.stderr

    workflow_dir, _ = instantiate_workflow_template(
        source_repo / "templates" / "execute-proposed-pr.yaml",
        work_item_name="priority-queue",
        workflow_instance_name="priority-queue-pr",
        output_root=tmp_path / "workflow",
        template_values={
            "proposed-pr-id": "priority-queue-pr",
            "feature-id": "priority-queue",
            "verification-command": "python -m pytest -q",
        },
    )
    workflow = WorkflowInstance.from_directory(workflow_dir)
    workflow.complete_task(
        "priority-queue-pr-task-001",
        {
            "proposed_pr": "priority-queue-pr",
            "acceptance_criteria": [
                {
                    "id": "stable-priority-order",
                    "description": (
                        "PriorityQueue.push(item, priority) accepts a string item "
                        "and integer priority, orders larger priorities first, and "
                        "preserves FIFO order for equal priorities."
                    ),
                },
                {
                    "id": "queue-safety",
                    "description": (
                        "peek, pop, and empty provide the expected queue behavior, "
                        "including IndexError for an empty pop."
                    ),
                },
            ],
            "expected_tests": [str(test_file.relative_to(repository))],
        },
    )
    workflow.complete_task(
        "priority-queue-pr-task-002",
        {
            "implementation_files": ["src/priority_queue.py"],
            "test_files": [str(test_file.relative_to(repository))],
            "verification_commands": [[sys.executable, "-m", "pytest", "-q"]],
        },
    )
    for later_task in workflow.tasks[3:]:
        (workflow_dir / f"{later_task.task_id}.yaml").unlink()

    task = WorkflowInstance.from_directory(workflow_dir).tasks[2]
    task = replace(
        task,
        details=(
            f"{task.details}\n\n"
            "Harness guidance: inspect only src/priority_queue.py and "
            "tests/test_priority_queue.py. For read_document, always provide "
            "file_path plus integer start_line 1 and end_line no greater than "
            "2000. Do not request a whole-repository read. The seeded tests are "
            "authoritative and already cover the acceptance criteria; do not "
            "edit the test file unless it is objectively incorrect. Implement "
            "the product file and do not finish until verification passes."
        ),
    )
    save_workflow_task(task, workflow_dir / f"{task.task_id}.yaml")
    config = WorkflowTaskAgentConfig(
        workflow_dir=workflow_dir,
        repo_root=repository,
        provider="deepinfra",
        task_id=task.task_id,
        api_key=api_key,
        allow_unmanaged_git=True,
        max_roundtrips=100,
        max_stalled_roundtrips=8,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    recorder = _LoggingLiveRecorder(
        _build_workflow_client(config, task),
        lambda message: print(message, flush=True),
    )
    stdout_stream = _TeeTextIO(stdout, sys.stdout)
    stderr_stream = _TeeTextIO(stderr, sys.stderr)

    assert (
        run_workflow_task(
            config,
            client=recorder,
            stdout=cast(TextIO, stdout_stream),
            stderr=cast(TextIO, stderr_stream),
        )
        == 0
    ), stderr.getvalue()
    implementation = (repository / "src" / "priority_queue.py").read_text(
        encoding="utf-8"
    )
    assert "class PriorityQueue" in implementation
    assert "NotImplementedError" not in implementation
    assert "heap" in implementation.lower() or "priority" in implementation.lower()

    final = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert final.returncode == 0, final.stdout + final.stderr
    assert WorkflowInstance.from_directory(workflow_dir).tasks[2].status.value == (
        "completed"
    )
    event_path = workflow_dir / "execution" / task.task_id / "events.jsonl"
    events = [
        json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("event_type") == "evidence_recorded"
        and event.get("payload", {}).get("evidence_type") == "capability:process"
        and event.get("payload", {}).get("successful") is True
        for event in events
    )


def _initialize_git_repository(repository: Path) -> None:
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Coding Loop Harness"),
        ("config", "user.email", "coding-loop-harness@example.invalid"),
        ("add", "."),
        ("commit", "-m", "Harness repository baseline"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        )
