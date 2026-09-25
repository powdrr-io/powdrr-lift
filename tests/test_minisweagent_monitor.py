from __future__ import annotations

import json
import sys
from pathlib import Path

from powdrr_lift.minisweagent_monitor import (
    MiniTrajectory,
    read_trajectory,
    run_minisweagent,
    snapshot_trajectory,
)


def test_read_trajectory_summarizes_model_calls_and_tool_actions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attempt.traj.json"
    path.write_text(
        json.dumps(
            {
                "info": {"exit_status": "Submitted"},
                "messages": [
                    {"role": "system"},
                    {"role": "assistant", "extra": {"actions": []}},
                    {
                        "role": "assistant",
                        "extra": {
                            "actions": [
                                {"command": "rg -n State src"},
                                {"command": "pytest -q tests/test_state.py"},
                            ]
                        },
                    },
                    {"role": "user", "extra": {"actions": []}},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = read_trajectory(path)

    assert result == MiniTrajectory(
        message_count=4,
        model_call_count=2,
        tool_call_count=2,
        last_action="pytest -q tests/test_state.py",
        exit_status="Submitted",
    )


def test_read_trajectory_treats_partial_or_invalid_files_as_no_progress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attempt.traj.json"
    path.write_text('{"messages":', encoding="utf-8")

    assert read_trajectory(path) == MiniTrajectory()


def test_snapshot_marks_slow_and_stalled_trajectory() -> None:
    trajectory = MiniTrajectory(
        message_count=3,
        model_call_count=1,
        tool_call_count=1,
        last_action="pytest -q",
    )

    slow = snapshot_trajectory(
        trajectory,
        elapsed_seconds=160,
        seconds_since_progress=151,
        timeout_seconds=300,
    )
    stalled = snapshot_trajectory(
        trajectory,
        elapsed_seconds=300,
        seconds_since_progress=300,
        timeout_seconds=300,
    )

    assert slow.state == "slow"
    assert slow.last_action == "pytest -q"
    assert stalled.state == "stalled"
    assert stalled.reason == "no trajectory update within the timeout"


def test_snapshot_uses_process_exit_as_terminal_state() -> None:
    snapshot = snapshot_trajectory(
        MiniTrajectory(exit_status="LimitsExceeded"),
        elapsed_seconds=12,
        seconds_since_progress=12,
        timeout_seconds=300,
        process_returncode=0,
    )

    assert snapshot.state == "completed"
    assert snapshot.exit_status == "LimitsExceeded"
    assert snapshot.reason == "process exited successfully"


def test_runner_polls_trajectory_and_records_timeout(tmp_path: Path) -> None:
    trajectory = tmp_path / "attempt.traj.json"
    log = tmp_path / "attempt.ndjson"
    script = (
        "import json, pathlib, time; "
        "pathlib.Path(__import__('sys').argv[1]).write_text("
        "json.dumps({'info': {'exit_status': ''}, 'messages': ["
        "{'role': 'assistant', 'extra': {'actions': ["
        "{'command': 'pytest -q'}]}}]})); "
        "time.sleep(1)"
    )

    result = run_minisweagent(
        [sys.executable, "-c", script, str(trajectory)],
        trajectory_path=trajectory,
        log_path=log,
        cwd=tmp_path,
        timeout_seconds=0.1,
        poll_interval=0.02,
    )

    assert result.returncode == 124
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(record["kind"] == "minisweagent.snapshot" for record in records)
    timeout = next(
        record for record in records if record["kind"] == "minisweagent.timeout"
    )
    assert timeout["timeout_seconds"] == 0.1
    assert timeout["snapshot"]["tool_call_count"] == 1


def test_runner_does_not_interrupt_continuous_progress(tmp_path: Path) -> None:
    trajectory = tmp_path / "attempt.traj.json"
    log = tmp_path / "attempt.ndjson"
    script = """
import json
import pathlib
import sys
import time

path = pathlib.Path(sys.argv[1])
for index in range(6):
    path.write_text(json.dumps({
        "info": {"exit_status": ""},
        "messages": [{
            "role": "assistant",
            "extra": {"actions": [{"command": f"echo {index}"}]},
        }] * (index + 1),
    }))
    time.sleep(0.03)
"""

    result = run_minisweagent(
        [sys.executable, "-c", script, str(trajectory)],
        trajectory_path=trajectory,
        log_path=log,
        cwd=tmp_path,
        timeout_seconds=0.08,
        poll_interval=0.01,
    )

    assert result.returncode == 0
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(record["kind"] == "minisweagent.timeout" for record in records)
