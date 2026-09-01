"""Atomic file-backed execution event and state persistence."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import (
    ExecutionEvent,
    ExecutionMode,
    ExecutionState,
    initial_execution_state,
    reduce_execution_event,
)
from powdrr_lift.errors import PowdrrExecutionError


class ExecutionStateConflict(PowdrrExecutionError):
    """Raised when a state append was based on a stale version."""

    def __init__(self, execution_id: str, expected: int, actual: int) -> None:
        self.execution_id = execution_id
        self.expected_version = expected
        self.actual_version = actual
        super().__init__(
            f"Execution {execution_id!r} expected state version {expected}, "
            f"but current version is {actual}.",
            error_code="execution_state_conflict",
            remediation=(
                "Reload the latest execution state and retry the append against "
                "its current version."
            ),
        )


class ExecutionStateStore(Protocol):
    def create(
        self,
        execution_id: str,
        *,
        profile_id: str,
        phase: PhaseType = PhaseType.INTAKE,
        mode: ExecutionMode = ExecutionMode.OBSERVE,
    ) -> ExecutionState: ...

    def load(self, execution_id: str) -> ExecutionState: ...

    def append(
        self,
        execution_id: str,
        expected_version: int,
        events: Sequence[ExecutionEvent],
    ) -> ExecutionState: ...

    def load_events(self, execution_id: str) -> tuple[ExecutionEvent, ...]: ...

    def append_transaction(
        self, execution_id: str, expected_version: int, events: Sequence[ExecutionEvent]
    ) -> ExecutionState: ...


class FileExecutionStateStore:
    """Persist one execution below ``workflow_directory/execution``.

    State and the complete event log are replaced through temporary files.  A
    stale caller cannot overwrite a newer state because ``append`` checks the
    expected materialized version before reducing or writing anything.
    """

    def __init__(self, workflow_directory: str | Path) -> None:
        self.root = Path(workflow_directory) / "execution"

    def create(
        self,
        execution_id: str,
        *,
        profile_id: str,
        phase: PhaseType = PhaseType.INTAKE,
        mode: ExecutionMode = ExecutionMode.OBSERVE,
    ) -> ExecutionState:
        state = initial_execution_state(
            execution_id, profile_id=profile_id, phase=phase, mode=mode
        )
        directory = self._execution_directory(execution_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._write_json(directory / "state.json", state.to_data())
        self._write_lines(directory / "events.jsonl", ())
        return state

    def load(self, execution_id: str) -> ExecutionState:
        self._recover_transaction(execution_id)
        path = self._execution_directory(execution_id) / "state.json"
        return ExecutionState.from_json(path.read_text(encoding="utf-8"))

    def load_events(self, execution_id: str) -> tuple[ExecutionEvent, ...]:
        path = self._execution_directory(execution_id) / "events.jsonl"
        if not path.exists():
            return ()
        return tuple(
            ExecutionEvent.from_data(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def append(
        self,
        execution_id: str,
        expected_version: int,
        events: Sequence[ExecutionEvent],
    ) -> ExecutionState:
        directory = self._execution_directory(execution_id)
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock(directory / "state.lock"):
            return self._append_locked(execution_id, expected_version, events)

    def append_transaction(
        self, execution_id: str, expected_version: int, events: Sequence[ExecutionEvent]
    ) -> ExecutionState:
        """Journal a state/event append so restart can finish an interrupted commit."""
        directory = self._execution_directory(execution_id)
        directory.mkdir(parents=True, exist_ok=True)
        journal = directory / "transaction.json"
        payload = {
            "expected_version": expected_version,
            "events": [event.to_data() for event in events],
        }
        with self._lock(directory / "state.lock"):
            self._write_json(journal, payload)
            try:
                return self._append_locked(execution_id, expected_version, events)
            finally:
                journal.unlink(missing_ok=True)

    def _append_locked(
        self, execution_id: str, expected_version: int, events: Sequence[ExecutionEvent]
    ) -> ExecutionState:
        directory = self._execution_directory(execution_id)
        state = ExecutionState.from_json(
            (directory / "state.json").read_text(encoding="utf-8")
        )
        if state.state_version != expected_version:
            raise ExecutionStateConflict(
                execution_id, expected_version, state.state_version
            )
        next_state = state
        for event in events:
            next_state = reduce_execution_event(next_state, event)
        event_path = directory / "events.jsonl"
        existing_events = (
            tuple(
                ExecutionEvent.from_data(json.loads(line))
                for line in event_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if event_path.exists()
            else ()
        )
        self._write_lines(
            event_path,
            tuple(
                json.dumps(event.to_data(), ensure_ascii=False)
                for event in (*existing_events, *events)
            ),
        )
        self._write_json(directory / "state.json", next_state.to_data())
        return next_state

    def _recover_transaction(self, execution_id: str) -> None:
        directory = self._execution_directory(execution_id)
        journal = directory / "transaction.json"
        if not journal.exists():
            return
        with self._lock(directory / "state.lock"):
            if not journal.exists():
                return
            payload = json.loads(journal.read_text(encoding="utf-8"))
            events = tuple(ExecutionEvent.from_data(item) for item in payload["events"])
            self._append_locked(execution_id, payload["expected_version"], events)
            journal.unlink(missing_ok=True)

    def verify(self, execution_id: str) -> ExecutionState:
        state = self.load(execution_id)
        rebuilt = initial_execution_state(
            state.execution_id,
            profile_id=state.profile_id,
            mode=state.mode,
            phase=state.current_phase,
        )
        events = self.load_events(execution_id)
        for event in events:
            rebuilt = reduce_execution_event(rebuilt, event)
        if rebuilt != state:
            raise ValueError(
                f"Execution state cache does not match its event log: {execution_id}."
            )
        return state

    def _execution_directory(self, execution_id: str) -> Path:
        if not execution_id or "/" in execution_id or "\\" in execution_id:
            raise ValueError("execution_id must be a non-empty path-safe identifier.")
        return self.root / execution_id

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        FileExecutionStateStore._atomic_write(
            path, json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        )

    @staticmethod
    def _write_lines(path: Path, lines: Sequence[str]) -> None:
        content = "" if not lines else "\n".join(lines) + "\n"
        FileExecutionStateStore._atomic_write(path, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, path)
        except BaseException:
            Path(temporary_path).unlink(missing_ok=True)
            raise

    @staticmethod
    @contextmanager
    def _lock(path: Path) -> Iterator[None]:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
