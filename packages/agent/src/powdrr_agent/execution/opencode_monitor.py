"""Capture OpenCode JSON events and classify session liveness."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

Liveness = Literal["active", "slow", "stalled", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class OpenCodeEvent:
    """One structured event and the local time at which it was observed."""

    captured_at: float
    payload: dict[str, Any]

    @property
    def event_type(self) -> str:
        """Return the event type across the supported OpenCode envelopes."""
        value = self.payload.get("type")
        if isinstance(value, str):
            return value
        nested = self.payload.get("event")
        if isinstance(nested, dict) and isinstance(nested.get("type"), str):
            return nested["type"]
        return "unknown"

    @property
    def is_heartbeat(self) -> bool:
        return self.event_type in {"server.heartbeat", "heartbeat"}

    @property
    def is_progress(self) -> bool:
        """Whether this event indicates work, rather than only transport health."""
        return not self.is_heartbeat and self.event_type not in {
            "server.connected",
            "server.disconnected",
        }

    @property
    def activity(self) -> dict[str, Any]:
        """Summarize what OpenCode was doing without duplicating its payload."""
        return classify_event(self.payload)


@dataclass(frozen=True, slots=True)
class LivenessSnapshot:
    """A point-in-time liveness decision for an OpenCode process."""

    state: Liveness
    seconds_since_event: float | None
    seconds_since_progress: float | None
    event_count: int
    progress_event_count: int
    reason: str
    last_event_type: str = "unknown"
    last_activity_kind: str = "unknown"


def classify_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify an OpenCode event for human-readable run telemetry."""
    event = payload.get("event")
    source = event if isinstance(event, Mapping) else payload
    event_type = source.get("type")
    event_type = event_type if isinstance(event_type, str) else "unknown"
    part = source.get("part")
    part_mapping = part if isinstance(part, Mapping) else {}
    part_type = part_mapping.get("type")
    tool = part_mapping.get("tool") or source.get("tool")
    status = part_mapping.get("state") or part_mapping.get("status")
    text = part_mapping.get("text") or source.get("text")
    if event_type in {"tool_use", "tool_call"} or part_type == "tool":
        kind = "tool_call"
    elif event_type in {"tool_result", "tool_completed"}:
        kind = "tool_result"
    elif event_type in {"step_start", "step-start"}:
        kind = "step_start"
    elif event_type in {"step_finish", "step-finish"}:
        kind = "step_finish"
    elif event_type in {"text", "message.updated", "message.part.updated"}:
        kind = "model_text"
    elif event_type in {"session.completed", "session_complete"}:
        kind = "session_complete"
    elif event_type in {"server.heartbeat", "heartbeat"}:
        kind = "heartbeat"
    else:
        kind = "other"
    result: dict[str, Any] = {"kind": kind, "event_type": event_type}
    if isinstance(tool, str) and tool:
        result["tool"] = tool
    if isinstance(status, str) and status:
        result["status"] = status
    if isinstance(text, str):
        result["text_chars"] = len(text)
    for key in ("sessionID", "messageID", "callID"):
        value = part_mapping.get(key) or source.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


class OpenCodeLiveness:
    """Track events and distinguish quiet work from a missing/dead run."""

    def __init__(
        self,
        *,
        slow_after: float = 60.0,
        stalled_after: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if slow_after <= 0 or stalled_after <= slow_after:
            raise ValueError("stalled_after must be greater than slow_after > 0")
        self.slow_after = slow_after
        self.stalled_after = stalled_after
        self._clock = clock
        self._started_at = clock()
        self._events: list[OpenCodeEvent] = []

    @property
    def events(self) -> tuple[OpenCodeEvent, ...]:
        return tuple(self._events)

    def observe(
        self, payload: dict[str, Any], *, captured_at: float | None = None
    ) -> None:
        """Record an event received from OpenCode."""
        self._events.append(
            OpenCodeEvent(
                captured_at=self._clock() if captured_at is None else captured_at,
                payload=payload,
            )
        )

    def snapshot(self, *, process_returncode: int | None = None) -> LivenessSnapshot:
        now = self._clock()
        last_event_record = self._events[-1] if self._events else None
        last_event = (
            last_event_record.captured_at if last_event_record is not None else None
        )
        progress_events = [event for event in self._events if event.is_progress]
        last_progress = progress_events[-1].captured_at if progress_events else None
        since_event = max(0.0, now - (last_event or self._started_at))
        since_progress = max(0.0, now - (last_progress or self._started_at))

        if process_returncode is not None:
            state: Liveness = "completed" if process_returncode == 0 else "failed"
            reason = (
                "process exited successfully"
                if state == "completed"
                else (f"process exited with status {process_returncode}")
            )
        elif not self._events and since_event <= self.stalled_after:
            state = "active"
            reason = "waiting for the first structured event"
        elif since_event > self.stalled_after:
            state = "stalled"
            reason = "no structured event received within the stall threshold"
        elif since_progress > self.slow_after:
            state = "slow"
            reason = "transport is alive, but no progress event arrived recently"
        else:
            state = "active"
            reason = (
                f"process active; last progress event was {since_progress:.1f}s ago"
            )

        return LivenessSnapshot(
            state=state,
            seconds_since_event=since_event,
            seconds_since_progress=since_progress,
            event_count=len(self._events),
            progress_event_count=len(progress_events),
            reason=reason,
            last_event_type=(
                last_event_record.event_type
                if last_event_record is not None
                else "unknown"
            ),
            last_activity_kind=(
                last_event_record.activity["kind"]
                if last_event_record is not None
                else "unknown"
            ),
        )


def parse_event_line(line: str) -> dict[str, Any] | None:
    """Parse one OpenCode JSON line, ignoring blank/non-JSON output."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def append_event(
    log_path: Path, payload: dict[str, Any], *, captured_at: float
) -> None:
    """Append a replayable event envelope to an NDJSON log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "captured_monotonic": captured_at,
        "event": payload,
        "activity": classify_event(payload),
    }
    with log_path.open("a", encoding="utf-8") as stream:
        json.dump(record, stream, separators=(",", ":"))
        stream.write("\n")


def append_diagnostic(
    log_path: Path, kind: str, *, captured_at: float, **data: Any
) -> None:
    """Append a monitor diagnostic to the replayable OpenCode log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "record_type": "diagnostic",
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "captured_monotonic": captured_at,
        "kind": kind,
        **data,
    }
    with log_path.open("a", encoding="utf-8") as stream:
        json.dump(record, stream, separators=(",", ":"), default=str)
        stream.write("\n")


def run_opencode(
    command: Sequence[str],
    *,
    log_path: Path | None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    inactivity_timeout: float = 300.0,
    absolute_timeout: float | None = None,
    max_events: int | None = None,
    on_snapshot: Callable[[LivenessSnapshot], None] | None = None,
    on_activity: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run OpenCode with inactivity and optional absolute deadlines.

    Activity includes any streamed OpenCode output/event and can also be
    reported by the owning Procedrr flow through ``on_activity``.
    """
    if inactivity_timeout <= 0:
        raise ValueError("inactivity_timeout must be greater than zero")
    if absolute_timeout is not None and absolute_timeout <= 0:
        raise ValueError("absolute_timeout must be greater than zero")
    if absolute_timeout is not None and absolute_timeout < inactivity_timeout:
        raise ValueError("absolute_timeout must be at least inactivity_timeout")
    if max_events is not None and max_events <= 0:
        raise ValueError("max_events must be greater than zero")
    monitor = OpenCodeLiveness(
        slow_after=max(inactivity_timeout / 2, 0.001),
        stalled_after=inactivity_timeout,
    )
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if process.stdout is None:
        raise RuntimeError("OpenCode process did not provide stdout")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, process.stdout)
    output: list[bytes] = []
    pending = ""
    # Raw bytes are useful for diagnostics, but they are not proof that
    # OpenCode is making progress.  In particular, heartbeats and other
    # transport chatter must not keep a stalled session alive indefinitely.
    started_at = time.monotonic()
    last_progress = started_at
    timed_out = False
    budget_exceeded = False
    last_snapshot: tuple[Any, ...] | None = None
    last_snapshot_at = last_progress

    def report_snapshot(*, force: bool = False) -> None:
        nonlocal last_snapshot, last_snapshot_at
        snapshot = monitor.snapshot()
        now = time.monotonic()
        signature = (
            snapshot.state,
            snapshot.event_count,
            snapshot.progress_event_count,
            int(snapshot.seconds_since_event or 0),
            int(snapshot.seconds_since_progress or 0),
        )
        if force or signature != last_snapshot or now - last_snapshot_at >= 15.0:
            if log_path is not None:
                process_tree = _process_tree(process.pid)
                append_diagnostic(
                    log_path,
                    "liveness.snapshot",
                    captured_at=now,
                    state=snapshot.state,
                    reason=snapshot.reason,
                    event_count=snapshot.event_count,
                    progress_event_count=snapshot.progress_event_count,
                    last_event_type=snapshot.last_event_type,
                    last_activity_kind=snapshot.last_activity_kind,
                    seconds_since_event=snapshot.seconds_since_event,
                    seconds_since_progress=snapshot.seconds_since_progress,
                    process_tree=process_tree,
                )
            if on_snapshot is not None:
                on_snapshot(snapshot)
            last_snapshot = signature
            last_snapshot_at = now

    if log_path is not None:
        append_diagnostic(
            log_path,
            "process.started",
            captured_at=last_progress,
            pid=process.pid,
            command=_diagnostic_command(command),
            cwd=str(cwd) if cwd is not None else None,
            inactivity_timeout=inactivity_timeout,
            absolute_timeout=absolute_timeout,
        )
    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            inactivity_remaining = inactivity_timeout - (now - last_progress)
            absolute_remaining = (
                absolute_timeout - (now - started_at)
                if absolute_timeout is not None
                else None
            )
            remaining = (
                min(inactivity_remaining, absolute_remaining)
                if absolute_remaining is not None
                else inactivity_remaining
            )
            if remaining <= 0:
                timed_out = True
                timeout_kind = (
                    "absolute"
                    if absolute_remaining is not None and absolute_remaining <= 0
                    else "inactivity"
                )
                if log_path is not None:
                    append_diagnostic(
                        log_path,
                        "process.timed_out",
                        captured_at=time.monotonic(),
                        timeout_kind=timeout_kind,
                        reason=(
                            "absolute process deadline exceeded"
                            if timeout_kind == "absolute"
                            else "no progress event or Procedrr activity within timeout"
                        ),
                        process_tree=_process_tree(process.pid),
                    )
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    pass
                break
            ready = selector.select(timeout=min(0.5, remaining))
            if not ready:
                report_snapshot()
                continue
            for key, _ in ready:
                stream = cast(BinaryIO, key.data)
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    selector.unregister(stream)
                    continue
                output.append(chunk)
                if log_path is not None:
                    append_diagnostic(
                        log_path,
                        "stream.output",
                        captured_at=time.monotonic(),
                        bytes=len(chunk),
                        buffered_bytes=len(pending),
                    )
                pending += chunk.decode("utf-8", errors="replace")
                lines = pending.splitlines(keepends=True)
                pending = (
                    lines.pop()
                    if lines and not lines[-1].endswith(("\n", "\r"))
                    else ""
                )
                for line in lines:
                    payload = parse_event_line(line)
                    if payload is None:
                        continue
                    captured_at = time.monotonic()
                    monitor.observe(payload, captured_at=captured_at)
                    if log_path is not None:
                        append_event(log_path, payload, captured_at=captured_at)
                    if monitor.events[-1].is_progress:
                        last_progress = captured_at
                        if on_activity is not None:
                            on_activity()
                    report_snapshot()
                    if max_events is not None and len(monitor.events) >= max_events:
                        budget_exceeded = True
                        if log_path is not None:
                            append_diagnostic(
                                log_path,
                                "process.budget_exceeded",
                                captured_at=time.monotonic(),
                                budget="events",
                                limit=max_events,
                                event_count=len(monitor.events),
                                process_tree=_process_tree(process.pid),
                            )
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except (PermissionError, ProcessLookupError):
                            pass
                        break
            if budget_exceeded:
                break
    finally:
        # A caller may cancel the parent while OpenCode is blocked in select.
        # Since OpenCode owns its process group, reap the whole group so a
        # cancelled Workrr attempt cannot leak an orphaned agent session.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
        selector.close()
        process.wait()

    returncode = process.returncode
    assert returncode is not None
    if timed_out or budget_exceeded:
        returncode = 124
    terminal_snapshot = monitor.snapshot(process_returncode=returncode)
    if log_path is not None:
        append_diagnostic(
            log_path,
            "process.exited",
            captured_at=time.monotonic(),
            returncode=returncode,
            state=terminal_snapshot.state,
            reason=terminal_snapshot.reason,
            event_count=terminal_snapshot.event_count,
            progress_event_count=terminal_snapshot.progress_event_count,
            last_event_type=terminal_snapshot.last_event_type,
            last_activity_kind=terminal_snapshot.last_activity_kind,
            process_tree=_process_tree(process.pid),
        )
    if on_snapshot is not None:
        on_snapshot(terminal_snapshot)
    if pending:
        output.append(pending.encode("utf-8"))
    return subprocess.CompletedProcess(
        list(command),
        returncode,
        b"".join(output).decode("utf-8", errors="replace"),
        "",
    )


def _process_tree(root_pid: int) -> list[dict[str, Any]]:
    """Return best-effort process/child diagnostics for a monitored session."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    pending = [root_pid]
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc_dir = proc_root / str(pid)
        try:
            stat = (proc_dir / "stat").read_text(encoding="utf-8")
            command = (
                (proc_dir / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
                .strip()
            )
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        closing = stat.rfind(")")
        state = stat[closing + 2 : closing + 3] if closing >= 0 else "?"
        result.append({"pid": pid, "state": state, "command": _bounded_text(command)})
        try:
            children = (proc_dir / "task" / str(pid) / "children").read_text(
                encoding="utf-8"
            )
        except (FileNotFoundError, OSError):
            children = ""
        pending.extend(int(value) for value in children.split() if value.isdigit())
    return sorted(result, key=lambda item: int(item["pid"]))


def _bounded_text(value: str, limit: int = 500) -> str:
    """Keep diagnostic text readable without cutting it off ambiguously."""
    if len(value) <= limit:
        return value
    return value[:limit] + "…<truncated>"


def _diagnostic_command(command: Sequence[str]) -> list[str]:
    """Bound command arguments before writing them to the diagnostic log."""
    return [_bounded_text(str(argument), 240) for argument in command]


def replay_events(records: Iterable[dict[str, Any]]) -> OpenCodeLiveness:
    """Rebuild a monitor from event-log records for offline analysis."""
    monitor = OpenCodeLiveness()
    for record in records:
        payload = record.get("event")
        captured_at = record.get("captured_monotonic")
        if isinstance(payload, dict) and isinstance(captured_at, (int, float)):
            monitor.observe(payload, captured_at=float(captured_at))
    return monitor
