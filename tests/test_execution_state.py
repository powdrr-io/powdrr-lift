from __future__ import annotations

import json
from pathlib import Path

import pytest

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import (
    ActionStatus,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionMode,
    FindingStatus,
    initial_execution_state,
    reduce_execution_event,
)
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.phases import PhaseController
from powdrr_lift.execution.shadow import ShadowExecutionRecorder
from powdrr_lift.execution.store import ExecutionStateConflict, FileExecutionStateStore


def _event(
    state_version: int,
    sequence: int,
    event_type: ExecutionEventType,
    payload: dict[str, object],
) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id="execution-1",
        sequence=sequence,
        expected_state_version=state_version,
        event_type=event_type,
        payload=payload,
        event_id=f"event-{sequence}",
    )


def test_reducer_applies_phase_action_obligation_and_finding_events() -> None:
    state = initial_execution_state(
        "execution-1", profile_id="default", mode=ExecutionMode.OBSERVE
    )
    state = reduce_execution_event(
        state,
        _event(
            0,
            1,
            ExecutionEventType.PHASE_ENTERED,
            {"phase_type": "build", "persona_id": "engineer"},
        ),
    )
    state = reduce_execution_event(
        state,
        _event(
            1,
            2,
            ExecutionEventType.ACTION_PROPOSED,
            {
                "action_instance_id": "action-1",
                "kind": "edit",
                "actor_id": "engineer",
                "phase_type": "build",
                "arguments_fingerprint": "args-1",
            },
        ),
    )
    state = reduce_execution_event(
        state,
        _event(
            2,
            3,
            ExecutionEventType.ACTION_COMPLETED,
            {"action_instance_id": "action-1"},
        ),
    )
    state = reduce_execution_event(
        state,
        _event(
            3,
            4,
            ExecutionEventType.OBLIGATION_OPENED,
            {
                "obligation_id": "obligation-1",
                "description": "Run changed-file diagnostics.",
            },
        ),
    )
    state = reduce_execution_event(
        state,
        _event(
            4,
            5,
            ExecutionEventType.FINDING_OPENED,
            {
                "finding_id": "finding-1",
                "reviewer_run_id": "review-1",
                "severity": "blocking",
                "description": "Missing validation evidence.",
            },
        ),
    )

    assert state.current_phase is PhaseType.BUILD
    assert state.actions[0].status is ActionStatus.COMPLETED
    assert state.obligations[0].description == "Run changed-file diagnostics."
    assert state.findings[0].status is FindingStatus.OPEN
    assert state.state_version == 5
    assert state.event_sequence == 5


def test_reducer_rejects_stale_or_out_of_order_events() -> None:
    state = initial_execution_state("execution-1", profile_id="default")
    event = _event(
        0,
        1,
        ExecutionEventType.PHASE_ENTERED,
        {"phase_type": "specify"},
    )
    updated = reduce_execution_event(state, event)

    with pytest.raises(ValueError, match="sequence"):
        reduce_execution_event(
            updated,
            _event(1, 3, ExecutionEventType.PHASE_EXITED, {}),
        )
    with pytest.raises(ValueError, match="expected_state_version"):
        reduce_execution_event(
            updated,
            _event(0, 2, ExecutionEventType.PHASE_EXITED, {}),
        )


def test_phase_controller_has_closed_transition_topology() -> None:
    controller = PhaseController()
    state = initial_execution_state("execution-1", profile_id="default")

    allowed = controller.evaluate(state, PhaseType.SPECIFY)
    blocked = controller.evaluate(state, PhaseType.BUILD)

    assert allowed.allowed
    assert allowed.guards == ()
    assert not blocked.allowed
    assert blocked.guards


def test_file_store_round_trips_and_verifies_event_log(tmp_path: Path) -> None:
    store = FileExecutionStateStore(tmp_path / "workflow")
    state = store.create("execution-1", profile_id="default")
    next_state = store.append(
        "execution-1",
        state.state_version,
        [
            _event(
                0,
                1,
                ExecutionEventType.PHASE_ENTERED,
                {"phase_type": "specify", "persona_id": "architect"},
            )
        ],
    )

    assert store.load("execution-1") == next_state
    assert store.verify("execution-1") == next_state
    assert len(store.load_events("execution-1")) == 1
    assert (tmp_path / "workflow" / "execution" / "execution-1" / "state.json").exists()


def test_file_store_rejects_stale_append_without_writing(tmp_path: Path) -> None:
    store = FileExecutionStateStore(tmp_path / "workflow")
    store.create("execution-1", profile_id="default")
    event = _event(
        0,
        1,
        ExecutionEventType.PHASE_ENTERED,
        {"phase_type": "specify"},
    )
    store.append("execution-1", 0, [event])

    with pytest.raises(ExecutionStateConflict) as raised:
        store.append("execution-1", 0, [event])
    assert isinstance(raised.value, PowdrrExecutionError)
    assert raised.value.error_code == "execution_state_conflict"

    assert len(store.load_events("execution-1")) == 1


def test_file_store_recovers_an_interrupted_transaction_journal(tmp_path: Path) -> None:
    store = FileExecutionStateStore(tmp_path / "workflow")
    store.create("execution-1", profile_id="default")
    event = _event(
        0,
        1,
        ExecutionEventType.PHASE_ENTERED,
        {"phase_type": "specify"},
    )
    journal = tmp_path / "workflow" / "execution" / "execution-1" / "transaction.json"
    journal.write_text(
        json.dumps({"expected_version": 0, "events": [event.to_data()]}),
        encoding="utf-8",
    )

    recovered = store.load("execution-1")

    assert recovered.current_phase is PhaseType.SPECIFY
    assert store.load_events("execution-1") == (event,)
    assert not journal.exists()


def test_state_json_has_typed_schema_version(tmp_path: Path) -> None:
    store = FileExecutionStateStore(tmp_path / "workflow")
    store.create("execution-1", profile_id="default")

    document = json.loads(
        (tmp_path / "workflow" / "execution" / "execution-1" / "state.json").read_text(
            encoding="utf-8"
        )
    )

    assert document["schema_version"] == "execution-state-v1"


def test_shadow_recorder_records_existing_runner_action_lifecycle(
    tmp_path: Path,
) -> None:
    store = FileExecutionStateStore(tmp_path / "workflow")
    recorder = ShadowExecutionRecorder(
        store,
        "execution-1",
        profile_id="legacy-workflow",
        phase=PhaseType.BUILD,
    )
    action = {"kind": "edit", "file_path": "src/example.py"}

    recorder.record_action(ExecutionEventType.ACTION_PROPOSED, action)
    recorder.record_action(ExecutionEventType.ACTION_COMPLETED, action)

    assert recorder.state.current_phase is PhaseType.BUILD
    assert recorder.state.actions[0].status is ActionStatus.COMPLETED
    assert recorder.state.event_sequence == 2
