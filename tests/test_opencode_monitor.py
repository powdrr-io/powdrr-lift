from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from powdrr_lift.opencode_monitor import (
    OpenCodeLiveness,
    append_event,
    classify_event,
    parse_event_line,
    replay_events,
    run_opencode,
)


def test_classify_event_distinguishes_model_text_and_tool_calls() -> None:
    assert classify_event(
        {"type": "text", "part": {"type": "text", "text": "thinking"}}
    ) == {
        "kind": "model_text",
        "event_type": "text",
        "text_chars": 8,
    }
    assert classify_event(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": "completed",
                "callID": "call-1",
            },
        }
    ) == {
        "kind": "tool_call",
        "event_type": "tool_use",
        "tool": "read",
        "status": "completed",
        "callID": "call-1",
    }


def test_parse_event_line_accepts_json_objects_only() -> None:
    assert parse_event_line('{"type":"message.updated"}') == {"type": "message.updated"}
    assert parse_event_line("not json") is None
    assert parse_event_line("[]") is None
    assert parse_event_line("  ") is None


def test_heartbeat_keeps_run_slow_but_not_stalled() -> None:
    now = [100.0]
    monitor = OpenCodeLiveness(
        slow_after=10,
        stalled_after=30,
        clock=lambda: now[0],
    )
    monitor.observe({"type": "message.part.updated"}, captured_at=90)
    monitor.observe({"type": "server.heartbeat"}, captured_at=99)

    now[0] = 100
    assert monitor.snapshot().state == "active"

    now[0] = 110
    snapshot = monitor.snapshot()
    assert snapshot.state == "slow"
    assert snapshot.seconds_since_event == 11
    assert snapshot.seconds_since_progress == 20

    now[0] = 130
    assert monitor.snapshot().state == "stalled"


def test_startup_wait_is_active_then_stalled_without_events() -> None:
    now = [100.0]
    monitor = OpenCodeLiveness(
        slow_after=10,
        stalled_after=30,
        clock=lambda: now[0],
    )

    now[0] = 129
    assert monitor.snapshot().state == "active"
    now[0] = 131
    assert monitor.snapshot().state == "stalled"


def test_process_exit_is_terminal_even_when_last_event_is_old() -> None:
    monitor = OpenCodeLiveness(clock=lambda: 500.0)
    monitor.observe({"type": "message.updated"}, captured_at=100)

    assert monitor.snapshot(process_returncode=0).state == "completed"
    assert monitor.snapshot(process_returncode=7).state == "failed"


def test_active_snapshot_does_not_claim_stale_progress_is_recent() -> None:
    now = [100.0]
    monitor = OpenCodeLiveness(
        slow_after=300,
        stalled_after=600,
        clock=lambda: now[0],
    )
    monitor.observe({"type": "message.updated"}, captured_at=90)

    now[0] = 200
    snapshot = monitor.snapshot()

    assert snapshot.state == "active"
    assert "110.0s ago" in snapshot.reason


def test_append_and_replay_event_log(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    append_event(path, {"type": "server.connected"}, captured_at=12.5)
    append_event(path, {"type": "message.updated"}, captured_at=13.5)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["event"]["type"] == "server.connected"
    assert records[0]["activity"]["kind"] == "other"
    assert records[1]["captured_monotonic"] == 13.5
    assert replay_events(records).events[1].event_type == "message.updated"


def test_run_opencode_records_lifecycle_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    result = run_opencode(
        [sys.executable, "-c", 'print(\'{"type":"session.completed"}\', flush=True)'],
        log_path=path,
        inactivity_timeout=0.2,
    )

    assert result.returncode == 0
    records = [json.loads(line) for line in path.read_text().splitlines()]
    diagnostics = [
        record for record in records if record.get("record_type") == "diagnostic"
    ]
    kinds = [record["kind"] for record in diagnostics]
    assert kinds[0:3] == [
        "process.started",
        "stream.output",
        "liveness.snapshot",
    ]
    assert kinds[-1] == "process.exited"
    assert set(kinds[3:-1]) <= {"liveness.snapshot"}
    assert diagnostics[-1]["returncode"] == 0


def test_run_opencode_records_timeout_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    result = run_opencode(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        log_path=path,
        inactivity_timeout=0.05,
    )

    assert result.returncode == 124
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(record.get("kind") == "process.timed_out" for record in records)
    assert records[-1]["kind"] == "process.exited"


def test_run_opencode_resets_inactivity_timeout_on_progress_events(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            'print(\'{"type":"session.started"}\', flush=True); '
            "time.sleep(0.05); "
            'print(\'{"type":"session.completed"}\', flush=True)'
        ),
    ]
    result = run_opencode(
        command,
        log_path=tmp_path / "events.ndjson",
        inactivity_timeout=0.1,
    )

    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 2


def test_run_opencode_does_not_reset_timeout_on_heartbeats(tmp_path: Path) -> None:
    result = run_opencode(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                '[print(\'{"type":"server.heartbeat"}\', flush=True) or '
                "time.sleep(0.02) for _ in range(20)]"
            ),
        ],
        log_path=tmp_path / "events.ndjson",
        inactivity_timeout=0.05,
    )

    assert result.returncode == 124


def test_run_opencode_times_out_when_no_activity_is_seen(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_opencode(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        log_path=None,
        inactivity_timeout=0.05,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 0.5
