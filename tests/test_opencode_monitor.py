from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from powdrr_lift.opencode_monitor import (
    OpenCodeLiveness,
    append_event,
    parse_event_line,
    replay_events,
    run_opencode,
)


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


def test_append_and_replay_event_log(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    append_event(path, {"type": "server.connected"}, captured_at=12.5)
    append_event(path, {"type": "message.updated"}, captured_at=13.5)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["event"]["type"] == "server.connected"
    assert records[1]["captured_monotonic"] == 13.5
    assert replay_events(records).events[1].event_type == "message.updated"


def test_run_opencode_resets_inactivity_timeout_on_streamed_events(
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


def test_run_opencode_times_out_when_no_activity_is_seen(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_opencode(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        log_path=None,
        inactivity_timeout=0.05,
    )

    assert result.returncode == 124
    assert time.monotonic() - started < 0.5
