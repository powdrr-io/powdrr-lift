"""Capture OpenCode JSON events and classify session liveness."""

from __future__ import annotations

import json
import selectors
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO, cast

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


@dataclass(frozen=True, slots=True)
class LivenessSnapshot:
    """A point-in-time liveness decision for an OpenCode process."""

    state: Liveness
    seconds_since_event: float | None
    seconds_since_progress: float | None
    event_count: int
    progress_event_count: int
    reason: str


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
        last_event = self._events[-1].captured_at if self._events else None
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
            reason = "recent progress event observed"

        return LivenessSnapshot(
            state=state,
            seconds_since_event=since_event,
            seconds_since_progress=since_progress,
            event_count=len(self._events),
            progress_event_count=len(progress_events),
            reason=reason,
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
    }
    with log_path.open("a", encoding="utf-8") as stream:
        json.dump(record, stream, separators=(",", ":"))
        stream.write("\n")


def run_opencode(
    command: Sequence[str],
    *,
    log_path: Path,
    cwd: Path | None = None,
    slow_after: float = 60.0,
    stalled_after: float = 180.0,
    on_snapshot: Callable[[LivenessSnapshot], None] | None = None,
) -> int:
    """Run an OpenCode JSON-producing command while capturing its events."""
    monitor = OpenCodeLiveness(
        slow_after=slow_after,
        stalled_after=stalled_after,
    )
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("OpenCode process did not provide stdout")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, process.stdout)
    while selector.get_map() or process.poll() is None:
        ready = selector.select(timeout=0.5)
        if not ready:
            if on_snapshot is not None:
                on_snapshot(monitor.snapshot())
            continue
        for key, _ in ready:
            stream = cast(TextIO, key.data)
            line = stream.readline()
            if line == "":
                selector.unregister(stream)
                continue
            captured_at = time.monotonic()
            payload = parse_event_line(line)
            if payload is None:
                continue
            monitor.observe(payload, captured_at=captured_at)
            append_event(log_path, payload, captured_at=captured_at)
            if on_snapshot is not None:
                on_snapshot(monitor.snapshot())
    selector.close()
    returncode = process.wait()
    if on_snapshot is not None:
        on_snapshot(monitor.snapshot(process_returncode=returncode))
    return returncode


def replay_events(records: Iterable[dict[str, Any]]) -> OpenCodeLiveness:
    """Rebuild a monitor from event-log records for offline analysis."""
    monitor = OpenCodeLiveness()
    for record in records:
        payload = record.get("event")
        captured_at = record.get("captured_monotonic")
        if isinstance(payload, dict) and isinstance(captured_at, (int, float)):
            monitor.observe(payload, captured_at=float(captured_at))
    return monitor
