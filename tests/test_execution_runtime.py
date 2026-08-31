from pathlib import Path

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import (
    ExecutionArtifact,
    ExecutionEventType,
    ObligationStatus,
)
from powdrr_lift.execution.builtin_tools import (
    invoke_file_mutation,
    invoke_shell_capability,
)
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


def test_runtime_compaction_has_retrievable_full_context(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-context",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    compacted = runtime.compact_prompt_context({"transcript": "x" * 2_000})
    restored = runtime.retrieve_prompt_context(compacted["full_context_ref"])

    assert restored["transcript"] == "x" * 2_000
    assert compacted["runtime_state"]["execution_id"] == "run-context"


def test_runtime_profile_blocks_handoff_without_required_artifact(
    tmp_path: Path,
) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-handoff",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        profile=profile,
    )

    decision = runtime.transition(PhaseType.SPECIFY)

    assert not decision.allowed
    assert "request" in " ".join(decision.guards)


def test_runtime_profile_rejects_wrong_phase_persona(tmp_path: Path) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-persona",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        profile=profile,
    )

    decision = runtime.transition(PhaseType.SPECIFY, persona_id="engineer")

    assert not decision.allowed
    assert "architect" in " ".join(decision.guards)


def test_runtime_persona_packet_must_match_current_profile_phase(
    tmp_path: Path,
) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-persona-packet",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        profile=profile,
    )

    try:
        runtime.persona_packet(
            profile,
            run_id="run",
            phase_type=PhaseType.SPECIFY,
            phase_actions=frozenset({"read"}),
            persona_actions={"architect": frozenset({"read"})},
            allowed_effects=frozenset(),
        )
    except ValueError as error:
        assert "does not match runtime phase" in str(error)
    else:
        raise AssertionError("mismatched persona packet should be rejected")


def test_runtime_restores_workspace_and_typed_state_atomically(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime = ExecutionRuntime(
        "run-checkpoint",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=repo_root,
    )
    target = repo_root / "source.txt"
    target.write_text("before\n", encoding="utf-8")
    checkpoint = runtime.checkpoint_store.create(
        repo_root, "checkpoint-1", state_json=runtime.state.to_json()
    )
    target.write_text("after\n", encoding="utf-8")
    runtime.record_artifact(
        ExecutionArtifact("artifact-1", "request", "v1", "architect", "ref-1")
    )

    restored = runtime.restore_checkpoint(checkpoint.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "before\n"
    assert not restored.artifacts
    assert runtime.verify() == restored
    assert runtime.state_store.load_events("run-checkpoint")[-1].event_type is (
        ExecutionEventType.CHECKPOINT_REVERTED
    )


def test_runtime_captures_explicit_guidance_with_stable_identity(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-guidance",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    first = runtime.capture_guidance(
        "Always use optimistic locking for mutable rows.", source_ref="user:1"
    )
    second = runtime.capture_guidance(
        "Always use optimistic locking for mutable rows.", source_ref="user:2"
    )

    assert first.rule_id == second.rule_id
    assert second.version == 2
    assert runtime.guidance({"profile_id": "default"})[0].text.startswith("Always")


def test_runtime_action_contract_allows_only_declared_actions(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-actions",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.set_action_contract(frozenset({"read_document"}))

    assert runtime.validate_action("read_document") == ()
    assert runtime.validate_action("next_step") == ()
    assert runtime.validate_action("edit")


def test_mutating_capability_invalidates_prior_evidence(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-invalidation",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    invoke_shell_capability(
        {"command": ["echo", "first"]},
        worktree_root=tmp_path,
        executor=lambda _arguments: {"returncode": 0},
        runtime=runtime,
    )
    assert runtime.state.evidence[0].fresh

    invoke_file_mutation(
        ("changed.txt",),
        worktree_root=tmp_path,
        executor=lambda: (tmp_path / "changed.txt").write_text("changed\n"),
        runtime=runtime,
    )

    assert not runtime.state.evidence[0].fresh


def test_runtime_diagnostics_are_recorded_as_evidence(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-diagnostics",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    def broken(_root: Path) -> str:
        raise RuntimeError("diagnostic failed")

    results = runtime.diagnose(
        (
            ("tests", lambda _root: "all tests passed"),
            ("broken", broken),
        )
    )

    assert [result.successful for result in results] == [True, False]
    assert {item.evidence_type for item in runtime.state.evidence} == {
        "diagnostic:tests",
        "diagnostic:broken",
    }


def test_failed_command_does_not_produce_successful_evidence(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-failed-command",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    invoke_shell_capability(
        {"command": ["false"]},
        worktree_root=tmp_path,
        executor=lambda _arguments: {"returncode": 1, "stderr": "failed"},
        runtime=runtime,
    )

    assert runtime.state.evidence
    assert not runtime.state.evidence[-1].successful
