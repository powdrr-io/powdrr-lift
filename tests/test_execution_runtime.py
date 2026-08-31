from pathlib import Path

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import ExecutionEventType, ObligationStatus
from powdrr_lift.execution.builtin_tools import invoke_shell_capability
from powdrr_lift.execution.capabilities import CapabilityRequest, CapabilityResolution
from powdrr_lift.execution.runtime import ExecutionRuntime


def test_runtime_persists_kernel_lifecycle_and_relationships(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-1",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    runtime.kernel.propose(
        {"kind": "change"},
        semantic_action="change_mutable_row",
    )
    runtime.sync_kernel(phase_type="build", actor_id="engineer")

    restored = runtime.state_store.load("run-1")
    events = runtime.state_store.load_events("run-1")
    assert any(
        event.event_type is ExecutionEventType.OBLIGATION_OPENED for event in events
    )
    assert any(
        obligation.status is ObligationStatus.OPEN
        for obligation in restored.obligations
    )
    assert not runtime.readiness().ready


def test_runtime_does_not_duplicate_kernel_events(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-2",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.kernel.propose({"kind": "edit"}, semantic_action="edit_source")

    runtime.sync_kernel(phase_type="build", actor_id="engineer")
    first_count = len(runtime.state_store.load_events("run-2"))
    runtime.sync_kernel(phase_type="build", actor_id="engineer")

    assert len(runtime.state_store.load_events("run-2")) == first_count


def test_runtime_restores_open_obligations_for_resume(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-resume",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.kernel.propose(
        {"kind": "change"},
        semantic_action="change_mutable_row",
    )
    runtime.sync_kernel(phase_type="build", actor_id="engineer")

    resumed = ExecutionRuntime(
        "run-resume",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    assert resumed.kernel.open_obligations
    assert resumed.kernel.validate_proposal({"kind": "unrelated"})


def test_runtime_persists_capability_decisions(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-3",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    result = runtime.invoke(
        runtime.context(semantic_actions=frozenset(), allowed_effects=frozenset()),
        CapabilityRequest("missing", "inspect", {}),
    )

    assert isinstance(result, CapabilityResolution)
    assert result.reason == "unknown tool"
    events = runtime.state_store.load_events("run-3")
    assert events[-1].event_type is ExecutionEventType.CAPABILITY_DECISION
    assert events[-1].payload["kind"] == "denied"


def test_runtime_phase_controller_is_durable_and_closed_topology(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-4",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    allowed = runtime.transition(PhaseType.SPECIFY, persona_id="architect")
    rejected = runtime.transition(PhaseType.BUILD, persona_id="engineer")

    assert allowed.allowed
    assert not rejected.allowed
    assert runtime.state.current_phase is PhaseType.SPECIFY
    assert runtime.verify().current_phase is PhaseType.SPECIFY


def test_builtin_helper_can_only_execute_through_runtime_broker_when_supplied(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-5",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    result = invoke_shell_capability(
        {"command": ["echo", "ok"]},
        worktree_root=tmp_path,
        executor=lambda _arguments: {"returncode": 0, "stdout": "ok"},
        runtime=runtime,
    )

    assert result == {"returncode": 0, "stdout": "ok"}
    events = runtime.state_store.load_events("run-5")
    assert any(
        event.event_type is ExecutionEventType.CAPABILITY_DECISION for event in events
    )
    assert events[-1].event_type is ExecutionEventType.EVIDENCE_RECORDED
