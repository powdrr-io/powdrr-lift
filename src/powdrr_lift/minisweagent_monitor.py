"""Run mini-SWE-agent with trajectory-based progress telemetry."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from powdrr_lift.opencode_monitor import append_diagnostic

MiniLiveness = Literal["active", "slow", "stalled", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class MiniSWEAgentSnapshot:
    """Observable progress from one mini-SWE-agent trajectory."""

    state: MiniLiveness
    elapsed_seconds: float
    seconds_since_progress: float
    message_count: int
    model_call_count: int
    tool_call_count: int
    last_action: str | None
    exit_status: str | None
    reason: str

    def to_data(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "seconds_since_progress": round(self.seconds_since_progress, 3),
            "message_count": self.message_count,
            "model_call_count": self.model_call_count,
            "tool_call_count": self.tool_call_count,
            "last_action": self.last_action,
            "exit_status": self.exit_status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MiniTrajectory:
    """Small summary of mini's current trajectory file."""

    message_count: int = 0
    model_call_count: int = 0
    tool_call_count: int = 0
    last_action: str | None = None
    exit_status: str | None = None


def read_trajectory(path: Path) -> MiniTrajectory:
    """Read the durable progress fields mini writes after each step."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MiniTrajectory()
    if not isinstance(raw, Mapping):
        return MiniTrajectory()
    messages = raw.get("messages")
    if not isinstance(messages, list):
        messages = []
    model_calls = 0
    tool_calls = 0
    last_action: str | None = None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "assistant":
            model_calls += 1
        extra = message.get("extra")
        if not isinstance(extra, Mapping):
            continue
        actions = extra.get("actions")
        if not isinstance(actions, list):
            continue
        tool_calls += len(actions)
        for action in reversed(actions):
            if isinstance(action, Mapping):
                command = action.get("command")
                if isinstance(command, str) and command.strip():
                    last_action = command.strip()
                    break
    info = raw.get("info")
    exit_status = info.get("exit_status") if isinstance(info, Mapping) else None
    return MiniTrajectory(
        message_count=len(messages),
        model_call_count=model_calls,
        tool_call_count=tool_calls,
        last_action=last_action,
        exit_status=exit_status if isinstance(exit_status, str) else None,
    )


def snapshot_trajectory(
    trajectory: MiniTrajectory,
    *,
    elapsed_seconds: float,
    seconds_since_progress: float,
    timeout_seconds: float,
    process_returncode: int | None = None,
) -> MiniSWEAgentSnapshot:
    """Classify whether mini is advancing its model/tool trajectory."""
    if process_returncode is not None:
        state: MiniLiveness = "completed" if process_returncode == 0 else "failed"
        reason = (
            "process exited successfully"
            if state == "completed"
            else f"process exited with status {process_returncode}"
        )
    elif seconds_since_progress >= timeout_seconds:
        state = "stalled"
        reason = "no trajectory update within the timeout"
    elif seconds_since_progress >= timeout_seconds / 2:
        state = "slow"
        reason = "process is alive but trajectory progress is slow"
    else:
        state = "active"
        reason = "trajectory is advancing"
    return MiniSWEAgentSnapshot(
        state=state,
        elapsed_seconds=elapsed_seconds,
        seconds_since_progress=seconds_since_progress,
        message_count=trajectory.message_count,
        model_call_count=trajectory.model_call_count,
        tool_call_count=trajectory.tool_call_count,
        last_action=trajectory.last_action,
        exit_status=trajectory.exit_status,
        reason=reason,
    )


def run_minisweagent(
    command: Sequence[str],
    *,
    trajectory_path: Path | None,
    log_path: Path | None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 300.0,
    poll_interval: float = 10.0,
    on_snapshot: Callable[[MiniSWEAgentSnapshot], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run mini with an inactivity timeout and trajectory liveness snapshots.

    ``timeout_seconds`` is the maximum interval without observed stdout or
    trajectory progress.  It is deliberately not a wall-clock deadline:
    active runs may take longer than this value.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ)
    started = time.monotonic()
    last_progress_at = started
    last_signature: tuple[int, int, int, str | None, str | None] = (
        0,
        0,
        0,
        None,
        None,
    )
    last_snapshot_at = started
    output = bytearray()
    timed_out = False

    def observe(*, returncode: int | None = None) -> MiniSWEAgentSnapshot:
        nonlocal last_progress_at, last_signature, last_snapshot_at
        trajectory = (
            read_trajectory(trajectory_path) if trajectory_path else MiniTrajectory()
        )
        signature = (
            trajectory.message_count,
            trajectory.model_call_count,
            trajectory.tool_call_count,
            trajectory.last_action,
            trajectory.exit_status,
        )
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            last_progress_at = now
        last_snapshot_at = now
        snapshot = snapshot_trajectory(
            trajectory,
            elapsed_seconds=now - started,
            seconds_since_progress=now - last_progress_at,
            timeout_seconds=timeout_seconds,
            process_returncode=returncode,
        )
        if log_path is not None:
            append_diagnostic(
                log_path,
                "minisweagent.snapshot",
                captured_at=now,
                **snapshot.to_data(),
            )
        if on_snapshot is not None:
            on_snapshot(snapshot)
        return snapshot

    while process.poll() is None:
        now = time.monotonic()
        remaining = timeout_seconds - (now - last_progress_at)
        if remaining <= 0:
            timed_out = True
            break
        for key, _ in selector.select(timeout=min(poll_interval, remaining)):
            fileobj = key.fileobj
            file_descriptor = fileobj if isinstance(fileobj, int) else fileobj.fileno()
            chunk = os.read(file_descriptor, 65536)
            if chunk:
                output.extend(chunk)
                last_progress_at = time.monotonic()
                if log_path is not None:
                    append_diagnostic(
                        log_path,
                        "minisweagent.stdout",
                        captured_at=time.monotonic(),
                        chars=len(chunk),
                    )
        if time.monotonic() - last_snapshot_at >= poll_interval:
            observe()

    if timed_out:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
        final_snapshot = observe()
        if log_path is not None:
            append_diagnostic(
                log_path,
                "minisweagent.timeout",
                captured_at=time.monotonic(),
                timeout_seconds=timeout_seconds,
                snapshot=final_snapshot.to_data(),
            )
        return subprocess.CompletedProcess(
            list(command), 124, output.decode(errors="replace"), ""
        )

    for key, _ in selector.select(timeout=0):
        fileobj = key.fileobj
        file_descriptor = fileobj if isinstance(fileobj, int) else fileobj.fileno()
        chunk = os.read(file_descriptor, 65536)
        if chunk:
            output.extend(chunk)
    returncode = process.wait()
    observe(returncode=returncode)
    return subprocess.CompletedProcess(
        list(command), returncode, output.decode(errors="replace"), ""
    )


__all__ = [
    "MiniSWEAgentSnapshot",
    "MiniTrajectory",
    "read_trajectory",
    "run_minisweagent",
    "snapshot_trajectory",
]
