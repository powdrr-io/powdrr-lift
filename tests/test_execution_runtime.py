from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.execution_state import (
    ExecutionArtifact,
    ExecutionEventType,
    ObligationStatus,
)
from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.builtin_tools import (
    invoke_file_mutation,
    invoke_shell_capability,
)
from powdrr_lift.execution.capabilities import CapabilityRequest, CapabilityResolution
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.execution.tools import (
    ToolContext,
    ToolResult,
    ToolValidationReport,
)


class MutableRowTool:
    manifest = ToolManifest(
        "mutable-row",
        ("change_mutable_row", "add_optimistic_lock", "run_concurrency_test"),
        (ToolEffect.WORKSPACE_WRITE,),
    )

    def validate(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> ToolValidationReport:
        return ToolValidationReport()

    def execute(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> ToolResult:
        return ToolResult(observed_effects=frozenset({ToolEffect.WORKSPACE_WRITE}))


class ExternalWriteTool:
    manifest = ToolManifest(
        "repository",
        ("mutate_pull_request",),
        (ToolEffect.GITHUB_MUTATION,),
    )

    def __init__(self) -> None:
        self.arguments: list[Mapping[str, object]] = []

    def validate(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> ToolValidationReport:
        return ToolValidationReport()

    def execute(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> ToolResult:
        self.arguments.append(arguments)
        return ToolResult(observed_effects=frozenset({ToolEffect.GITHUB_MUTATION}))


class CountingExecutionStore:
    def __init__(self, directory: Path) -> None:
        from powdrr_lift.execution.store import FileExecutionStateStore

        self.delegate = FileExecutionStateStore(directory)
        self.transaction_count = 0

    def create(self, *args: object, **kwargs: object) -> object:
        return self.delegate.create(*args, **kwargs)  # type: ignore[arg-type]

    def load(self, execution_id: str) -> object:
        return self.delegate.load(execution_id)

    def load_events(self, execution_id: str) -> object:
        return self.delegate.load_events(execution_id)

    def append(self, *args: object, **kwargs: object) -> object:
        return self.delegate.append(*args, **kwargs)  # type: ignore[arg-type]

    def append_transaction(self, *args: object, **kwargs: object) -> object:
        self.transaction_count += 1
        return self.delegate.append(*args, **kwargs)  # type: ignore[arg-type]


def test_runtime_owns_the_default_builtin_capability_registry(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-default-registry",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    assert {manifest.tool_name for manifest in runtime.capability_manifests()} == {
        "repository",
        "enrich",
        "process",
        "file-mutation",
        "validate-edit",
        "apply-edit",
        "fuzzy-match",
        "basedpyright-symbol",
        "basedpyright-structure",
        "repository-gather_context",
        "repository-read_document",
        "repository-list_files",
    }
    assert runtime.prompt_context()["capability_catalog"] == [
        manifest.to_data()
        for manifest in sorted(
            runtime.capability_manifests(), key=lambda item: item.tool_name
        )
    ]


def test_effective_action_contract_intersects_registered_adapter_scope(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-effective-actions",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.set_execution_scope(
        declared_actions=frozenset({"edit", "shell"}),
        phase_actions=frozenset({"edit", "shell"}),
        persona_actions=frozenset({"edit", "shell"}),
        unit_actions=frozenset({"edit", "shell"}),
        adapter_actions=frozenset({"edit"}),
    )

    assert runtime.effective_action_contract() == frozenset({"edit"})


def test_runtime_invocation_commits_one_transaction(tmp_path: Path) -> None:
    store = CountingExecutionStore(tmp_path / "workflow")
    runtime = ExecutionRuntime(
        "run-transaction",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        state_store=store,  # type: ignore[arg-type]
        adapters=(MutableRowTool(),),
    )
    runtime.invoke(
        runtime.context(
            semantic_actions=frozenset({"change_mutable_row"}),
            allowed_effects=frozenset({ToolEffect.WORKSPACE_WRITE}),
        ),
        CapabilityRequest("mutable-row", "change_mutable_row", {"row": "users:1"}),
    )

    assert store.transaction_count == 1


def test_runtime_binds_external_writes_to_a_stable_idempotency_key(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-idempotency",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    adapter = ExternalWriteTool()
    runtime.register_adapter(adapter)
    context = runtime.context(
        semantic_actions=frozenset({"mutate_pull_request"}),
        allowed_effects=frozenset({ToolEffect.GITHUB_MUTATION}),
    )
    request = CapabilityRequest(
        "repository",
        "mutate_pull_request",
        {
            "operation": "pr_edit",
            "pr_reference": "123",
            "title": "Updated",
            "body": "Body",
        },
    )

    runtime.invoke(context, request)
    with pytest.raises(PowdrrExecutionError, match="already completed"):
        runtime.invoke(context, request)

    assert len(adapter.arguments) == 1
    assert isinstance(adapter.arguments[0]["idempotency_key"], str)
    resumed = ExecutionRuntime(
        "run-idempotency",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    resumed.register_adapter(adapter)
    with pytest.raises(PowdrrExecutionError, match="already completed"):
        resumed.invoke(
            resumed.context(
                semantic_actions=frozenset({"mutate_pull_request"}),
                allowed_effects=frozenset({ToolEffect.GITHUB_MUTATION}),
            ),
            request,
        )


def test_runtime_rejects_nested_capability_outside_step_contract(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-contract",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        adapters=(ExternalWriteTool(),),
    )
    runtime.set_action_contract(frozenset({"read_document"}))
    with pytest.raises(PowdrrExecutionError, match="not allowed by the active step"):
        runtime.invoke(
            runtime.context(
                semantic_actions=frozenset({"mutate_pull_request"}),
                allowed_effects=frozenset({ToolEffect.GITHUB_MUTATION}),
            ),
            CapabilityRequest(
                "repository",
                "mutate_pull_request",
                {
                    "operation": "pr_edit",
                    "pr_reference": "123",
                    "title": "Updated",
                    "body": "Body",
                },
            ),
        )


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


def test_runtime_invalidates_evidence_through_durable_events(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-evidence-invalidation",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.record_evidence(
        evidence_id="evidence-1",
        producer_action_instance_id="action-1",
        evidence_type="pytest",
        input_fingerprint="fingerprint-1",
        successful=True,
    )

    runtime.invalidate_evidence(frozenset({"fingerprint-1"}))

    assert not runtime.state.evidence[0].fresh
    assert any(
        event.event_type is ExecutionEventType.EVIDENCE_INVALIDATED
        for event in runtime.state_store.load_events("run-evidence-invalidation")
    )


def test_runtime_publish_gate_returns_typed_remediation(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-publish-gate",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.kernel.propose({"kind": "change"}, semantic_action="change_mutable_row")
    runtime.sync_kernel(phase_type="build", actor_id="engineer")

    with pytest.raises(PowdrrExecutionError) as raised:
        runtime.require_publish_readiness()

    assert raised.value.error_code == "readiness_blocked"
    assert raised.value.remediation


def test_runtime_publish_gate_cannot_be_bypassed_by_direct_capability_invocation(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-direct-publish",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.kernel.propose({"kind": "change"}, semantic_action="change_mutable_row")
    runtime.sync_kernel(phase_type="build", actor_id="engineer")

    with pytest.raises(PowdrrExecutionError) as raised:
        runtime.invoke(
            runtime.context(
                semantic_actions=frozenset({"mutate_pull_request"}),
                allowed_effects=frozenset(),
            ),
            CapabilityRequest(
                "repository", "mutate_pull_request", {"operation": "pr_create"}
            ),
        )

    assert raised.value.error_code == "readiness_blocked"


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


def test_runtime_capability_invocation_projects_relationship_obligations(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-capability-relationships",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        adapters=(MutableRowTool(),),
    )
    context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset(
            {"change_mutable_row", "add_optimistic_lock", "run_concurrency_test"}
        ),
        frozenset({ToolEffect.WORKSPACE_WRITE}),
        execution_id=runtime.execution_id,
    )

    result = runtime.invoke(
        context,
        CapabilityRequest("mutable-row", "change_mutable_row", {}),
    )

    assert isinstance(result, ToolResult)
    assert {item.required_action for item in runtime.kernel.open_obligations} == {
        "add_optimistic_lock",
        "run_concurrency_test",
    }
    assert any(
        event.event_type is ExecutionEventType.OBLIGATION_OPENED
        for event in runtime.state_store.load_events(runtime.execution_id)
    )
    assert {
        item["required_action"] for item in runtime.prompt_context()["open_obligations"]
    } == {"add_optimistic_lock", "run_concurrency_test"}

    for semantic_action in ("add_optimistic_lock", "run_concurrency_test"):
        runtime.invoke(
            context,
            CapabilityRequest("mutable-row", semantic_action, {}),
        )
    assert not runtime.kernel.open_obligations


def test_runtime_rejects_capability_context_from_another_execution(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-context-owner",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    foreign_context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset(),
        frozenset(),
        execution_id="other-execution",
    )

    with pytest.raises(PowdrrExecutionError) as raised:
        runtime.invoke(foreign_context, CapabilityRequest("missing", "inspect", {}))

    assert raised.value.error_code == "capability_execution_mismatch"


def test_persona_packet_installs_the_same_action_contract_used_for_validation(
    tmp_path: Path,
) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-persona-contract",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        profile=profile,
    )

    packet = runtime.persona_packet(
        profile,
        run_id="persona-run",
        phase_type=PhaseType.INTAKE,
        phase_actions=frozenset({"next_step", "edit"}),
        persona_actions={"architect": frozenset({"next_step"})},
        allowed_effects=frozenset(),
    )

    assert packet.allowed_actions == frozenset({"next_step"})
    assert runtime.validate_action("edit")
    assert not runtime.validate_action("next_step")
    assert runtime.state.current_persona_id == "architect"
    assert runtime.prompt_context()["persona_id"] == "architect"
    assert runtime.prompt_context()["persona"] == {
        "persona_id": "architect",
        "persona_type": "architect",
        "model_profile": "architecture",
        "prompt_catalogs": ["architect-responsibilities"],
    }
    assert runtime.verify().current_persona_id == "architect"
    resumed = ExecutionRuntime(
        "run-persona-contract",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        profile=profile,
    )
    assert resumed.prompt_context()["persona_id"] == "architect"
    assert resumed.prompt_context()["persona"]["persona_type"] == "architect"


def test_runtime_rejects_capability_context_from_another_persona(
    tmp_path: Path,
) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-persona-owner",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        profile=profile,
    )
    runtime.persona_packet(
        profile,
        run_id="persona-owner",
        phase_type=PhaseType.INTAKE,
        phase_actions=frozenset({"next_step"}),
        persona_actions={"architect": frozenset({"next_step"})},
        allowed_effects=frozenset(),
    )
    context = runtime.context(
        semantic_actions=frozenset({"inspect_repository"}),
        allowed_effects=frozenset(),
    )
    mismatched = replace(context, active_persona_id="engineer")

    with pytest.raises(PowdrrExecutionError) as raised:
        runtime.invoke(
            mismatched, CapabilityRequest("missing", "inspect_repository", {})
        )

    assert raised.value.error_code == "capability_persona_mismatch"


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


def test_runtime_phase_transition_consults_durable_obligations(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-durable-obligation-phase-gate",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime._append_event(
        ExecutionEventType.OBLIGATION_OPENED,
        {
            "obligation_id": "plan-decision:run:approve",
            "description": "Resolve plan decision: approve",
            "relationship_id": "run",
        },
    )
    runtime.state = replace(runtime.state, current_phase=PhaseType.REVIEW_PR)

    rejected = runtime.transition(
        PhaseType.CONFIRM_READINESS, persona_id="code-reviewer"
    )

    assert not rejected.allowed
    assert any("Resolve plan decision: approve" in guard for guard in rejected.guards)


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
    except PowdrrExecutionError as error:
        assert "does not match runtime phase" in str(error)
    else:
        raise AssertionError("mismatched persona packet should be rejected")


def test_runtime_rejects_invalid_plan_before_compilation(tmp_path: Path) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile
    from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-plan-validation",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    plan = ExecutionPlan(
        "plan-invalid",
        "fingerprint",
        (ExecutionUnit("unit", "", ("../escape",)),),
        ("src",),
    )

    try:
        runtime.compile_plan(profile, plan, actions_by_phase={})
    except PowdrrExecutionError as error:
        assert "not compilable" in str(error)
    else:
        raise AssertionError("invalid plan should not compile")


def test_plan_decisions_become_durable_readiness_obligations(tmp_path: Path) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile
    from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-plan-decision",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    plan = ExecutionPlan(
        "plan-decision",
        "fingerprint",
        (
            ExecutionUnit(
                "unit",
                "Implement the unit",
                ("src",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("tests pass",),
            ),
        ),
        ("src",),
        introduced_decisions=("approve-build",),
    )

    runtime.compile_plan(profile, plan, actions_by_phase={})

    assert not runtime.readiness().ready
    runtime.resolve_plan_decision(plan.plan_id, "approve-build")
    assert runtime.readiness().ready


def test_compile_plan_to_workflow_uses_runtime_validation_boundary(
    tmp_path: Path,
) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile
    from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
    from powdrr_lift.core.workflow_task_specification import WorkflowInstance

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-compiled-workflow",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "runtime",
        repo_root=tmp_path,
        profile=profile,
    )
    plan = ExecutionPlan(
        plan_id="plan-compiled-workflow",
        proposed_pr_fingerprint="workflow-fingerprint",
        units=(
            ExecutionUnit(
                unit_id="unit-1",
                objective="Implement the change",
                paths=("src/app.py",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("The implementation is validated.",),
            ),
        ),
        allowed_paths=("src",),
    )

    workflow = runtime.compile_plan_to_workflow(
        profile,
        plan,
        actions_by_phase={
            phase.phase_type: ("read_document",) for phase in profile.phases
        },
        workflow_directory=tmp_path / "workflow",
    )

    assert isinstance(workflow, WorkflowInstance)
    assert runtime.load_plan(plan.plan_id) == plan
    generated = workflow.tasks[0]
    assert generated.input_state["execution_plan_id"] == plan.plan_id
    assert generated.input_state["proposed_pr_fingerprint"] == (
        plan.proposed_pr_fingerprint
    )
    assert generated.input_state["persona_id"] == generated.persona_id
    assert generated.output_state["acceptance_criteria"] == [
        "The implementation is validated."
    ]


def test_compile_plan_requires_one_action_contract_per_phase(tmp_path: Path) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile
    from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-missing-actions",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "runtime",
        repo_root=tmp_path,
        profile=profile,
    )
    plan = ExecutionPlan(
        "plan-missing-actions",
        "fingerprint",
        (
            ExecutionUnit(
                "unit-1",
                "Implement the change",
                ("src/app.py",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("tests pass",),
            ),
        ),
        ("src",),
    )

    with pytest.raises(PowdrrExecutionError, match="action contract"):
        runtime.compile_plan_to_workflow(
            profile,
            plan,
            actions_by_phase={},
            workflow_directory=tmp_path / "workflow",
        )


def test_compile_plan_accepts_explicit_empty_phase_contracts(tmp_path: Path) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile
    from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-empty-actions",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "runtime",
        repo_root=tmp_path,
        profile=profile,
    )
    plan = ExecutionPlan(
        "plan-empty-actions",
        "fingerprint",
        (
            ExecutionUnit(
                "unit-1",
                "Wait",
                ("src/app.py",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("wait completes",),
            ),
        ),
        ("src",),
    )

    workflow = runtime.compile_plan_to_workflow(
        profile,
        plan,
        actions_by_phase={phase.phase_type: () for phase in profile.phases},
        workflow_directory=tmp_path / "workflow",
    )

    assert workflow.tasks
    assert all(task.actions == () for task in workflow.tasks)
    assert all(task.actions_declared for task in workflow.tasks)


def test_compiled_workflow_preserves_multi_unit_dependencies_and_personas(
    tmp_path: Path,
) -> None:
    from powdrr_lift.core.delivery_profile import load_delivery_profile
    from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit

    profile = load_delivery_profile(
        Path(__file__).parents[1] / "delivery-profiles/default-software-delivery.yaml"
    )
    runtime = ExecutionRuntime(
        "run-multi-unit-plan",
        profile_id=profile.profile_id,
        workflow_directory=tmp_path / "runtime",
        repo_root=tmp_path,
        profile=profile,
    )
    plan = ExecutionPlan(
        "plan-multi-unit",
        "multi-fingerprint",
        (
            ExecutionUnit(
                "foundation",
                "Build the foundation",
                ("src/foundation.py",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("foundation is tested",),
            ),
            ExecutionUnit(
                "feature",
                "Build the feature",
                ("src/feature.py",),
                dependencies=("foundation",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("feature is tested",),
            ),
        ),
        ("src",),
    )
    actions: dict[PhaseType, tuple[str, ...]] = {
        phase.phase_type: ("read_document", "next_step") for phase in profile.phases
    }

    workflow = runtime.compile_plan_to_workflow(
        profile,
        plan,
        actions_by_phase=actions,
        intent_ids_by_phase={PhaseType.BUILD: ("intent-locking",)},
        clause_ids_by_phase={PhaseType.BUILD: ("clause-locking-v1",)},
        workflow_directory=tmp_path / "workflow",
    )
    feature_build = next(
        task for task in workflow.tasks if task.task_id == "feature-build"
    )

    assert feature_build.upstream_task_ids == (
        "feature-await_plan_decision",
        "foundation-build",
    )
    assert feature_build.input_state["execution_unit_id"] == "feature"
    assert feature_build.phase_type is PhaseType.BUILD
    assert feature_build.persona_id
    assert feature_build.actions == ("read_document", "next_step")
    assert feature_build.input_state["intent_ids"] == ["intent-locking"]
    assert feature_build.input_state["clause_ids"] == ["clause-locking-v1"]
    reloaded = workflow.__class__.from_directory(tmp_path / "workflow")
    assert {task.task_id: task for task in reloaded.tasks} == {
        task.task_id: task for task in workflow.tasks
    }


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
    assert runtime.state_store.load_events("run-checkpoint")[-1].payload[
        "changed_paths"
    ]


def test_runtime_rejects_mismatched_checkpoint_before_mutating_workspace(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = repo_root / "source.txt"
    target.write_text("current\n", encoding="utf-8")
    runtime = ExecutionRuntime(
        "run-current",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=repo_root,
    )
    other = ExecutionRuntime(
        "run-other",
        profile_id="default",
        workflow_directory=tmp_path / "other-workflow",
        repo_root=repo_root,
    )
    checkpoint = runtime.checkpoint_store.create(
        repo_root,
        "checkpoint-mismatch",
        state_json=other.state.to_json(),
    )

    with pytest.raises(PowdrrExecutionError, match="different execution"):
        runtime.restore_checkpoint(checkpoint.checkpoint_id)

    assert target.read_text(encoding="utf-8") == "current\n"


def test_runtime_rejects_checkpoint_restore_into_another_workspace(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime = ExecutionRuntime(
        "run-workspace-mismatch",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=repo_root,
    )
    checkpoint = runtime.checkpoint_store.create(
        repo_root, "checkpoint-workspace", state_json=runtime.state.to_json()
    )

    with pytest.raises(PowdrrExecutionError) as raised:
        runtime.restore_checkpoint(checkpoint.checkpoint_id, workspace_root=tmp_path)

    assert raised.value.error_code == "checkpoint_workspace_mismatch"


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


def test_runtime_prompt_context_applies_phase_scoped_guidance(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-phase-guidance",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    runtime.capture_guidance(
        "Always include the architecture tradeoff table.",
        source_ref="user:phase-guidance",
        scope={"profile_id": "default", "phase_type": "intake"},
    )

    prompt_context = runtime.prompt_context()

    assert [item["text"] for item in prompt_context["guidance"]] == [
        "Always include the architecture tradeoff table."
    ]


def test_runtime_action_contract_allows_only_declared_actions(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-actions",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    assert runtime.allowed_actions() is None
    assert runtime.prompt_context()["allowed_actions"] is None
    runtime.set_action_contract(frozenset({"read_document"}))

    assert runtime.validate_action("read_document") == ()
    assert runtime.validate_action("next_step") == ()
    assert runtime.validate_action("edit")
    assert runtime.allowed_actions() == ("next_step", "read_document")
    assert runtime.prompt_context()["allowed_actions"] == [
        "next_step",
        "read_document",
    ]

    runtime.set_action_contract(frozenset({"next_step"}))
    assert runtime.capability_catalog() == ()

    runtime.set_action_contract(frozenset(), enforce_empty=True)
    assert runtime.allowed_actions() == ("next_step",)
    assert runtime.validate_action("edit")
    assert runtime.capability_catalog() == ()

    runtime.set_action_contract(frozenset({"edit", "next_step"}))
    assert {item["tool_name"] for item in runtime.capability_catalog()} == {
        "apply-edit",
        "file-mutation",
        "validate-edit",
    }


def test_engine_bookkeeping_temporarily_suspends_action_contract(
    tmp_path: Path,
) -> None:
    runtime = ExecutionRuntime(
        "run-bookkeeping",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )
    runtime.set_action_contract(frozenset({"read_document"}))

    with runtime.without_action_contract():
        assert runtime.validate_action("git") == ()
        assert runtime.allowed_actions() is None

    assert runtime.allowed_actions() == ("next_step", "read_document")
    assert runtime.validate_action("git")


def test_runtime_persists_observer_decisions_in_event_stream(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(
        "run-observer",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
    )

    runtime.record_observer_decision(
        verdict="coach",
        reason="The action made no material progress.",
        action_kind="edit",
        action_signature='{"kind":"edit","path":"src/app.py"}',
        material_progress=False,
    )

    event = runtime.state_store.load_events("run-observer")[-1]
    assert event.event_type is ExecutionEventType.OBSERVER_DECISION
    assert event.payload["verdict"] == "coach"
    assert runtime.verify() == runtime.state


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
