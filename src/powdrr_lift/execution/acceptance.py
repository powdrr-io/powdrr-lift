"""Deterministic final acceptance and runtime-surface audits."""

from __future__ import annotations

import argparse
import inspect
import io
import json
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from powdrr_lift.core.architecture_specification import (
    build_architecture_specification_validation_report,
)
from powdrr_lift.core.capability_exception import CapabilityExceptionAuthority
from powdrr_lift.core.delivery_profile import PhaseType, load_delivery_profile
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.core.execution_state import ExecutionArtifact
from powdrr_lift.core.implementation_specification import (
    build_implementation_specification_validation_report,
)
from powdrr_lift.core.pr_specification import build_pr_specification_validation_report
from powdrr_lift.core.system_specification import (
    build_system_specification_validation_report,
)
from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.errors import (
    ExecutionCancelled,
    PersistenceCorruptionError,
    PowdrrExecutionError,
    ProgrammerInvariantError,
    ProviderExecutionError,
)
from powdrr_lift.execution.capabilities import CapabilityRequest, CapabilityResolution
from powdrr_lift.execution.compile import compile_execution_plan
from powdrr_lift.execution.evidence import EvidenceRequirement
from powdrr_lift.execution.personas import build_persona_packet
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.execution.tools import ToolContext, ToolResult, ToolValidationReport
from powdrr_lift.workflow_execution import ProgressDecision
from powdrr_lift.workflow_llm import (
    WorkflowAction,
    WorkflowActionObservation,
    WorkflowActionOutcome,
    WorkflowActionRequest,
    WorkflowStepRunner,
)
from powdrr_lift.workflow_observer import (
    ObserverExecutionContext,
    ShadowWorkflowObserver,
)
from powdrr_lift.workflow_scenario import load_workflow_scenario, run_workflow_scenario

REQUIRED_BUILTIN_MANIFESTS = frozenset(
    {
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
)


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str

    def to_data(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    checks: tuple[AcceptanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    def to_data(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [item.to_data() for item in self.checks],
        }


class _AcceptanceExceptionAdapter:
    """Small deterministic adapter used to exercise the exception boundary."""

    def __init__(self, tool_name: str, action: str, calls: list[str]) -> None:
        self.manifest = ToolManifest(
            tool_name,
            (action,),
            (ToolEffect.PROCESS_EXECUTION,),
            scope="worktree",
            sandbox_profile="acceptance-exception",
        )
        self._calls = calls

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        self._calls.append(self.manifest.tool_name)
        return ToolResult(
            "approved execution", frozenset({ToolEffect.PROCESS_EXECUTION})
        )


class _StructuredDeliveryAdapter:
    """Drive the runtime with the repository's real artifact validators."""

    manifest = ToolManifest(
        "structured-delivery",
        (
            "validate_specification",
            "create_proposed_pr",
            "implement_proposed_pr",
            "review_implementation",
            "publish_proposed_pr",
        ),
        (ToolEffect.WORKSPACE_READ,),
        scope="worktree",
    )

    def __init__(self, artifact_root: Path, calls: list[str]) -> None:
        self.artifact_root = artifact_root
        self.calls = calls

    def validate(
        self, context: ToolContext, arguments: Mapping[str, Any]
    ) -> ToolValidationReport:
        stage = arguments.get("stage")
        implementation_path = (
            self.artifact_root
            / "docs/specs/powdrr-lift/implementation-specification.yaml"
        )
        proposal_path = (
            self.artifact_root
            / "docs/proposals/acceptance-feature/proposed-pr-specification.yaml"
        )
        if stage in {"validate_specification", "create_proposed_pr"}:
            if not implementation_path.is_file() or not proposal_path.is_file():
                return ToolValidationReport(("delivery artifacts are incomplete",))
            try:
                implementation = implementation_path.read_text(encoding="utf-8")
                proposal = proposal_path.read_text(encoding="utf-8")
                if stage == "create_proposed_pr":
                    report = build_pr_specification_validation_report(
                        proposal,
                        work_item_name="acceptance-feature",
                        repo_root=self.artifact_root,
                        file_path=proposal_path,
                    )
                    if not report.validation_successful:
                        return ToolValidationReport(
                            tuple(issue.message for issue in report.issues)
                        )
                elif "specification-v1" not in implementation:
                    return ToolValidationReport(
                        ("implementation specification has an invalid schema",)
                    )
            except OSError as exc:
                return ToolValidationReport((str(exc),))
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        self.calls.append(str(arguments["stage"]))
        return ToolResult(
            f"completed {arguments['stage']}", frozenset({ToolEffect.WORKSPACE_READ})
        )


def run_final_acceptance(
    repo_root: str | Path, workflow_directory: str | Path
) -> AcceptanceReport:
    """Exercise the typed path without an LLM or external GitHub mutation."""
    root = Path(repo_root)
    structured_artifacts = _run_structured_artifact_chain(Path(workflow_directory))
    profile = load_delivery_profile(
        root / "delivery-profiles/default-software-delivery.yaml"
    )
    plan = ExecutionPlan(
        "final-acceptance-plan",
        "final-acceptance-pr",
        (
            ExecutionUnit(
                "architecture",
                "Architect validates the structured delivery specification",
                ("docs/specs/powdrr-lift/implementation-specification.yaml",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("the structured specification is valid",),
            ),
            ExecutionUnit(
                "proposed-pr",
                "Engineering Manager creates one scoped proposed PR",
                ("docs/proposals/acceptance-feature/proposed-pr-specification.yaml",),
                dependencies=("architecture",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("the proposed PR is scoped and reviewable",),
            ),
            ExecutionUnit(
                "implementation",
                "Engineer implements the approved proposed PR",
                ("src/feature.py",),
                dependencies=("proposed-pr",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("the implementation satisfies the proposal",),
            ),
            ExecutionUnit(
                "review",
                "Independent reviewers validate the implementation",
                ("tests/test_feature.py",),
                dependencies=("implementation",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("blocking findings are resolved",),
            ),
        ),
        ("src", "docs", "tests"),
    )
    actions: dict[PhaseType, tuple[str, ...]] = {
        phase.phase_type: ("next_step", "complete") for phase in profile.phases
    }
    tasks = compile_execution_plan(profile, plan, actions_by_phase=actions)
    runtime = ExecutionRuntime(
        "final-acceptance-execution",
        profile_id=profile.profile_id,
        workflow_directory=workflow_directory,
        repo_root=root,
        profile=profile,
        exception_authority=CapabilityExceptionAuthority(b"final-acceptance-key"),
    )
    workflow = runtime.compile_plan_to_workflow(
        profile,
        plan,
        actions_by_phase=actions,
        workflow_directory=workflow_directory,
    )
    delivery_calls: list[str] = []
    delivery_adapter = _StructuredDeliveryAdapter(
        Path(workflow_directory) / "structured-artifacts", delivery_calls
    )
    runtime.register_adapter(delivery_adapter)
    intake = next(
        phase for phase in profile.phases if phase.phase_type is PhaseType.INTAKE
    )
    delivery_actions = frozenset(delivery_adapter.manifest.semantic_actions)
    runtime.persona_packet(
        profile,
        run_id=runtime.execution_id,
        phase_type=PhaseType.INTAKE,
        phase_actions=delivery_actions,
        persona_actions={intake.persona_id: delivery_actions},
        allowed_effects=frozenset({ToolEffect.WORKSPACE_READ}),
    )
    runtime.set_execution_scope(
        declared_actions=delivery_actions,
        phase_actions=delivery_actions,
        persona_actions=delivery_actions,
        unit_actions=delivery_actions,
        adapter_actions=runtime.available_adapter_actions(),
    )
    delivery_context = ToolContext(
        root,
        root,
        delivery_actions,
        frozenset({ToolEffect.WORKSPACE_READ}),
        execution_id=runtime.execution_id,
        active_persona_id=runtime.state.current_persona_id,
    )
    for stage in delivery_adapter.manifest.semantic_actions:
        delivery_result = runtime.invoke(
            delivery_context,
            CapabilityRequest("structured-delivery", stage, {"stage": stage}),
        )
        if not isinstance(delivery_result, ToolResult):
            raise PowdrrExecutionError(
                f"structured delivery stage did not execute: {stage}",
                error_code="acceptance_delivery_incomplete",
            )
    contract = runtime.prompt_context()
    persona_packets = tuple(
        build_persona_packet(
            profile,
            execution_id=runtime.execution_id,
            run_id=f"acceptance-{phase.phase_type.value}",
            phase_type=phase.phase_type,
            phase_actions=frozenset({"next_step"}),
            persona_actions={
                persona.persona_id: frozenset({"next_step"})
                for persona in profile.personas
            },
            allowed_effects=frozenset(),
        )
        for phase in profile.phases
    )
    boundary_contexts: list[dict[str, Any]] = []
    phase_transitions, published = _walk_profile(
        runtime, profile, boundary_contexts=boundary_contexts
    )
    phase_state = runtime.state
    replayed_runtime = ExecutionRuntime(
        runtime.execution_id,
        profile_id=profile.profile_id,
        workflow_directory=workflow_directory,
        repo_root=root,
        profile=profile,
    )
    replayed_runtime.verify()

    checkpoint_workspace = Path(workflow_directory) / "acceptance-workspace"
    checkpoint_workspace.mkdir(parents=True, exist_ok=True)
    protected_file = checkpoint_workspace / "protected.txt"
    protected_file.write_text("before", encoding="utf-8")
    checkpoint = runtime.checkpoint_store.create(
        checkpoint_workspace,
        "acceptance-partial-failure",
        state_json=runtime.state.to_json(),
    )
    protected_file.write_text("partially changed", encoding="utf-8")
    (checkpoint_workspace / "partial.txt").write_text("partial", encoding="utf-8")
    changed_paths = runtime.checkpoint_store.changed_paths(
        checkpoint, checkpoint_workspace
    )
    restored_state = runtime.restore_checkpoint(
        checkpoint.checkpoint_id, workspace_root=checkpoint_workspace
    )
    partial_failure_recovered = (
        changed_paths == ("partial.txt", "protected.txt")
        and protected_file.read_text(encoding="utf-8") == "before"
        and restored_state.execution_id == runtime.execution_id
    )

    calls: list[str] = []
    denied_adapter = _AcceptanceExceptionAdapter(
        "acceptance-secret-denied", "read_secret_denied", calls
    )
    approved_adapter = _AcceptanceExceptionAdapter(
        "acceptance-secret-approved", "read_secret_approved", calls
    )
    runtime.register_adapter(denied_adapter)
    runtime.register_adapter(approved_adapter)
    runtime.set_execution_scope(
        declared_actions=frozenset({"read_secret_denied", "read_secret_approved"}),
        phase_actions=frozenset({"read_secret_denied", "read_secret_approved"}),
        persona_actions=frozenset({"read_secret_denied", "read_secret_approved"}),
        unit_actions=frozenset({"read_secret_denied", "read_secret_approved"}),
        adapter_actions=runtime.available_adapter_actions(),
    )
    exception_context = ToolContext(
        root,
        root,
        frozenset({"read_secret_denied", "read_secret_approved"}),
        frozenset(),
        execution_id=runtime.execution_id,
        active_persona_id=runtime.state.current_persona_id,
    )
    denied_request = CapabilityRequest(
        "acceptance-secret-denied", "read_secret_denied", {"target": "secret"}
    )
    denied_exception = runtime.request_capability_exception(
        exception_context,
        denied_request,
        "acceptance denial path",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    duplicate_denied_exception = runtime.request_capability_exception(
        exception_context,
        denied_request,
        "duplicate denial prompt must be suppressed",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    from powdrr_lift.cli import _run_execution_exceptions
    from powdrr_lift.mcp_server import execution_exceptions_tool

    cli_output = io.StringIO()
    with redirect_stdout(cli_output):
        cli_pending_exit = _run_execution_exceptions(
            argparse.Namespace(
                workflow_dir=Path(workflow_directory),
                exception_id=None,
                decision=None,
                decided_by="acceptance-reviewer",
            )
        )
    cli_pending = json.loads(cli_output.getvalue())
    mcp_pending = json.loads(execution_exceptions_tool(workflow_directory))
    denied_decision = runtime.decide_capability_exception(
        denied_exception, approved=False, decided_by="acceptance-reviewer"
    )
    denied_blocked = not denied_decision.approved
    approved_request = CapabilityRequest(
        "acceptance-secret-approved", "read_secret_approved", {"target": "secret"}
    )
    approved_exception = runtime.request_capability_exception(
        exception_context,
        approved_request,
        "acceptance approval path",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    approved_decision = runtime.decide_capability_exception(
        approved_exception, approved=True, decided_by="acceptance-reviewer"
    )
    approved_result = runtime.invoke_approved_exception(
        exception_context, approved_request, approved_decision
    )
    altered_result = runtime.invoke(
        exception_context,
        CapabilityRequest(
            "acceptance-secret-approved",
            "read_secret_approved",
            {"target": "different-secret"},
            approved_decision.token,
        ),
    )
    exception_flow = (
        denied_blocked
        and isinstance(approved_result, ToolResult)
        and calls == ["acceptance-secret-approved"]
    )
    scope_context = ToolContext(
        root,
        root,
        frozenset({"edit_files"}),
        frozenset({ToolEffect.WORKSPACE_WRITE}),
        execution_id=runtime.execution_id,
    )
    from powdrr_lift.execution.builtin_tools import FileMutationAdapter

    scope_report = FileMutationAdapter(lambda: None).validate(
        scope_context, {"paths": ["../outside-worktree"]}
    )
    scope_rejected = not scope_report.valid

    review_kernel = runtime.kernel
    runtime.set_execution_scope(
        declared_actions=frozenset(
            {
                "edit_for_review",
                "resolve_review_thread",
                "run_validation",
                "add_optimistic_lock",
                "run_concurrency_test",
            }
        ),
        phase_actions=frozenset(
            {
                "edit_for_review",
                "resolve_review_thread",
                "run_validation",
                "add_optimistic_lock",
                "run_concurrency_test",
            }
        ),
    )
    with runtime.transaction():
        review_kernel.propose(
            {
                "kind": "review-edit",
                "attributes": ["thread:R123", "validated"],
            },
            semantic_action="edit_for_review_comment",
        )
        blocked_resolution = review_kernel.validate_proposal(
            {"kind": "resolve", "thread": "R123", "attributes": ["thread:R123"]},
            semantic_action="resolve_review_thread",
        )
        review_kernel.complete(
            {
                "kind": "validation",
                "semantic_action": "run_validation",
                "attributes": ["thread:R123"],
            }
        )
        allowed_resolution = review_kernel.validate_proposal(
            {"kind": "resolve", "thread": "R123", "attributes": ["thread:R123"]},
            semantic_action="resolve_review_thread",
        )
        review_kernel.complete(
            {
                "kind": "resolve",
                "semantic_action": "resolve_review_thread",
                "attributes": ["thread:R123"],
            }
        )
        runtime._projected_kernel_events = 0
        runtime.sync_kernel(phase_type=PhaseType.BUILD.value, actor_id="engineer")
    review_closed = not review_kernel.open_obligations

    mutable_kernel = runtime.kernel
    mutable_kernel.propose({"kind": "row-change"}, semantic_action="change_mutable_row")
    mutable_actions = {item.required_action for item in mutable_kernel.open_obligations}
    chat_sequence = _run_shared_runner(workflow_directory, root, "chat")
    task_sequence = _run_shared_runner(workflow_directory, root, "task")
    production_task = _run_production_task_adapter(Path(workflow_directory), root)
    production_chat = _run_production_chat_adapter(Path(workflow_directory), root)
    guidance_before = runtime.prompt_context()
    runtime.capture_guidance(
        "When a review-driven change is made, resolve the comment after validation.",
        source_ref="acceptance:user-request:review-resolution",
    )
    runtime.capture_guidance(
        "Always use optimistic locking for mutable row changes.",
        source_ref="acceptance:user-request:optimistic-locking",
    )
    guidance_after = runtime.prompt_context()
    guidance_requirements = runtime.guidance_required_actions()
    guidance_execution = ExecutionRuntime(
        "guidance-follow-up-execution",
        profile_id=profile.profile_id,
        workflow_directory=workflow_directory,
        repo_root=root,
    )
    guidance_calls: list[str] = []
    guidance_execution.register_adapter(
        _AcceptanceExceptionAdapter(
            "guidance-row", "change_mutable_row", guidance_calls
        )
    )
    guidance_execution.capture_guidance(
        "Always use optimistic locking for mutable row changes.",
        source_ref="acceptance:user-request:optimistic-locking",
    )
    guidance_execution.set_execution_scope(
        declared_actions=frozenset({"change_mutable_row"}),
        phase_actions=frozenset({"change_mutable_row"}),
        persona_actions=frozenset({"change_mutable_row"}),
        unit_actions=frozenset({"change_mutable_row"}),
        adapter_actions=guidance_execution.available_adapter_actions(),
    )
    guidance_execution.invoke(
        guidance_execution.context(
            semantic_actions=frozenset({"change_mutable_row"}),
            allowed_effects=frozenset({ToolEffect.PROCESS_EXECUTION}),
        ),
        CapabilityRequest("guidance-row", "change_mutable_row", {"row": "users:1"}),
    )
    guidance_obligations = {
        item.required_action for item in guidance_execution.kernel.open_obligations
    }
    compacted = runtime.compact_prompt_context(
        {
            "transcript": "x" * 2_000,
            "contract_fingerprint": contract["contract_fingerprint"],
        }
    )
    restarted_runtime = ExecutionRuntime(
        runtime.execution_id,
        profile_id=profile.profile_id,
        workflow_directory=workflow_directory,
        repo_root=root,
        profile=profile,
    )
    restarted_context = restarted_runtime.retrieve_prompt_context(
        compacted["full_context_ref"]
    )
    later_prompt = restarted_runtime.prompt_context()
    retrieved = runtime.retrieve_prompt_context(compacted["full_context_ref"])
    boundary_retrieval = tuple(
        ExecutionRuntime(
            runtime.execution_id,
            profile_id=profile.profile_id,
            workflow_directory=workflow_directory,
            repo_root=root,
            profile=profile,
        ).retrieve_prompt_context(item["full_context_ref"])
        for item in boundary_contexts
    )
    evidence_fingerprint = "source-fingerprint-v1"
    runtime.record_evidence(
        evidence_id="acceptance-validation",
        producer_action_instance_id="run_validation",
        evidence_type="validation:repository",
        input_fingerprint=evidence_fingerprint,
        successful=True,
    )
    evidence_ready = runtime.publish_readiness(
        required_evidence=(
            EvidenceRequirement(
                "validation:repository",
                evidence_fingerprint,
                "repository validation",
            ),
        )
    )
    runtime.invalidate_evidence(frozenset({evidence_fingerprint}))
    stale_evidence_blocked = not runtime.publish_readiness(
        required_evidence=(
            EvidenceRequirement(
                "validation:repository",
                evidence_fingerprint,
                "repository validation",
            ),
        )
    ).ready
    exception_store = runtime.broker.exception_store
    assert exception_store is not None
    pending_exceptions = exception_store.pending()
    try:
        WorkflowStepRunner(max_stalled_roundtrips=1)
    except ProgrammerInvariantError:
        legacy_runner_is_explicit = True
    else:
        legacy_runner_is_explicit = False
    checks = (
        AcceptanceCheck(
            "observer-production-boundary",
            _run_observer_acceptance(Path(workflow_directory)),
            (
                "observer coaching is exercised, logged, and deduplicated at the "
                "shared boundary"
            ),
        ),
        AcceptanceCheck(
            "vertical-structured-delivery",
            structured_artifacts
            and tuple(delivery_calls) == delivery_adapter.manifest.semantic_actions
            and production_task.status == "passed"
            and production_chat.status == "passed",
            (
                "the production adapters execute the structured request through "
                "implementation and review surfaces"
            ),
        ),
        AcceptanceCheck(
            "production-guidance-context",
            _exchanges_contain_guidance(production_task)
            and _exchanges_contain_guidance(production_chat),
            "durable guidance is present in prompts sent by both production adapters",
        ),
        AcceptanceCheck(
            "durable-guidance-changes-behavior",
            len(guidance_after["guidance"]) == len(guidance_before["guidance"]) + 2
            and "resolve the comment" in str(guidance_after["guidance"]).casefold()
            and "optimistic locking" in str(guidance_after["guidance"]).casefold()
            and guidance_requirements
            == frozenset(
                {
                    "run_validation",
                    "resolve_review_thread",
                    "add_optimistic_lock",
                    "run_concurrency_test",
                }
            )
            and later_prompt["guidance_required_actions"]
            == sorted(guidance_requirements)
            and guidance_obligations == {"add_optimistic_lock", "run_concurrency_test"}
            and guidance_calls == ["guidance-row"],
            (
                "explicit user instructions are persisted and present in a later "
                "prompt context"
            ),
        ),
        AcceptanceCheck(
            "effective-action-intersection",
            runtime.effective_action_contract()
            == frozenset({"add_optimistic_lock", "run_concurrency_test"}),
            "declared, phase, persona, and obligation scopes intersect before exposure",
        ),
        AcceptanceCheck(
            "transaction-boundary",
            any(
                event.event_type.value == "action_proposed"
                for event in runtime.state_store.load_events(runtime.execution_id)
            ),
            "lifecycle projection is committed through the runtime transaction "
            "boundary",
        ),
        AcceptanceCheck(
            "enforce-mode-runtime-authority",
            runtime.state.mode.value == "enforce"
            and runtime.broker.checkpoint_store is runtime.checkpoint_store,
            (
                "enforce-mode execution owns the broker, checkpoint, and durable "
                "state authority"
            ),
        ),
        AcceptanceCheck(
            "legacy-runner-isolation",
            legacy_runner_is_explicit,
            "runtime-less workflow execution requires an explicit compatibility opt-in",
        ),
        AcceptanceCheck(
            "normal-adapter-exception-flow",
            len(pending_exceptions) == 0
            and duplicate_denied_exception.exception_id == denied_exception.exception_id
            and cli_pending_exit == 0
            and any(
                item["exception_id"] == denied_exception.exception_id
                for item in cli_pending
            )
            and any(
                item["exception_id"] == denied_exception.exception_id
                for item in mcp_pending
            )
            and isinstance(altered_result, CapabilityResolution)
            and altered_result.kind.value == "exception_required"
            and calls == ["acceptance-secret-approved"],
            (
                "the shared adapter path supports inspect, deny, approve, and "
                "one-time execution"
            ),
        ),
        AcceptanceCheck(
            "durable-exception-request",
            any(
                event.event_type.value == "capability_exception_requested"
                and event.payload.get("exception_id") == denied_exception.exception_id
                for event in runtime.state_store.load_events(runtime.execution_id)
            ),
            "exception request creation is retained in the durable execution stream",
        ),
        AcceptanceCheck(
            "interruption-retrieval",
            restarted_context == retrieved
            and restarted_context["transcript"].startswith("x"),
            "omitted tool output remains retrievable after a fresh runtime is started",
        ),
        AcceptanceCheck(
            "phase-boundary-retrieval",
            len(boundary_retrieval) == len(boundary_contexts)
            and all(
                isinstance(item.get("phase"), str)
                and item["tool_output"].startswith("{")
                for item in boundary_retrieval
            ),
            (
                "each phase boundary preserves actual runtime output for "
                "restart retrieval"
            ),
        ),
        AcceptanceCheck(
            "typed-error-boundary",
            issubclass(ProviderExecutionError, PowdrrExecutionError)
            and issubclass(PersistenceCorruptionError, PowdrrExecutionError)
            and issubclass(ProgrammerInvariantError, PowdrrExecutionError)
            and issubclass(ExecutionCancelled, PowdrrExecutionError),
            (
                "provider, cancellation, persistence, invariant, and action errors "
                "are distinct"
            ),
        ),
        AcceptanceCheck(
            "compiled-task-graph",
            len(tasks) == len(plan.units) * len(profile.phases)
            and len(workflow.tasks) == len(tasks),
            (
                f"compiled {len(workflow.tasks)} typed tasks across "
                f"{len(plan.units)} units"
            ),
        ),
        AcceptanceCheck(
            "runtime-contract",
            "effective_contract" in contract and "clause_ids" in contract,
            "prompt context contains the derived contract and typed references",
        ),
        AcceptanceCheck(
            "persona-phase-assignments",
            len(persona_packets) == len(profile.phases)
            and {packet.persona.persona_id for packet in persona_packets}
            == {phase.persona_id for phase in profile.phases},
            "every compiled phase has its assigned least-privilege persona packet",
        ),
        AcceptanceCheck(
            "review-resolution-order",
            bool(blocked_resolution) and not allowed_resolution and review_closed,
            "review resolution is blocked before validation and closes after it",
        ),
        AcceptanceCheck(
            "mutable-row-consequences",
            mutable_actions == {"add_optimistic_lock", "run_concurrency_test"},
            "mutable-row edits require locking and concurrency evidence",
        ),
        AcceptanceCheck(
            "durable-lifecycle",
            any(
                event.event_type.value == "obligation_opened"
                for event in runtime.state_store.load_events(
                    "final-acceptance-execution"
                )
            ),
            "kernel lifecycle and obligations are projected to durable events",
        ),
        AcceptanceCheck(
            "adapter-parity",
            chat_sequence == task_sequence,
            "chat and durable-task adapters use the same durable typed lifecycle",
        ),
        AcceptanceCheck(
            "production-task-adapter",
            production_task.status == "passed"
            and any(
                item["name"] == "task_status" and item["passed"]
                for item in production_task.assertions
            ),
            "the production workflow-task adapter completes a persisted task handoff",
        ),
        AcceptanceCheck(
            "production-chat-adapter",
            production_chat.status == "passed"
            and any(
                item["name"] == "outcome" and item["passed"]
                for item in production_chat.assertions
            ),
            "the production chat adapter completes a parsed action sequence",
        ),
        AcceptanceCheck(
            "structured-artifact-chain",
            structured_artifacts,
            "a structured implementation catalog produces a validated proposed PR",
        ),
        AcceptanceCheck(
            "stale-evidence-gate",
            evidence_ready.ready and stale_evidence_blocked,
            (
                "fresh validation evidence is accepted and invalidated evidence "
                "blocks readiness"
            ),
        ),
        AcceptanceCheck(
            "compaction-retrieval",
            compacted["contract_fingerprint"] == contract["contract_fingerprint"]
            and retrieved["contract_fingerprint"] == contract["contract_fingerprint"]
            and len(compacted["transcript"]) < len(retrieved["transcript"]),
            (
                "compaction preserves typed references and retains bounded "
                "full-context retrieval"
            ),
        ),
        AcceptanceCheck(
            "full-phase-walk",
            len(phase_transitions) == 15 and published,
            "all configured phases and required artifact handoffs reach publication",
        ),
        AcceptanceCheck(
            "interruption-replay",
            replayed_runtime.state == phase_state,
            "a restarted runtime rebuilds the same durable typed state",
        ),
        AcceptanceCheck(
            "partial-failure-recovery",
            partial_failure_recovered,
            (
                "checkpoint restore recovers workspace and logical state after "
                "partial mutation"
            ),
        ),
        AcceptanceCheck(
            "exception-decision-flow",
            exception_flow,
            (
                "denial blocks execution and approval resumes exactly the bound "
                "request once"
            ),
        ),
        AcceptanceCheck(
            "scope-expansion-blocked",
            scope_rejected,
            "a path outside the active worktree is rejected at the adapter boundary",
        ),
    )
    return AcceptanceReport(checks)


def _run_structured_artifact_chain(workflow_directory: Path) -> bool:
    """Run the real specification handoff validators before execution."""
    artifact_root = workflow_directory / "structured-artifacts"
    proposal_root = artifact_root / "docs" / "proposals" / "acceptance-feature"
    proposal_root.mkdir(parents=True, exist_ok=True)
    system_path = proposal_root / "system-specification.yaml"
    system_path.write_text(
        "schema: https://powdrr.io/schemas/specification-v1\n"
        "id: acceptance-system\n"
        "title: Acceptance system\n"
        "requirements:\n"
        "- id: acceptance-requirement\n"
        "  description: Preserve the typed delivery boundary.\n"
        "  state: added\n"
        "approach:\n"
        "- id: acceptance-approach\n"
        "  description: Use the validated execution runtime.\n"
        "  state: added\n",
        encoding="utf-8",
    )
    architecture_path = proposal_root / "architecture-specification.yaml"
    architecture_path.write_text(
        "schema: https://powdrr.io/schemas/specification-v1\n"
        "id: acceptance-architecture\n"
        "title: Acceptance architecture\n"
        "entities: []\nmodules: []\ntools: []\n"
        "entity_relationships: []\ninvariants: []\nguidance: []\n",
        encoding="utf-8",
    )
    implementation_path = (
        artifact_root
        / "docs"
        / "specs"
        / "powdrr-lift"
        / "implementation-specification.yaml"
    )
    implementation_path.parent.mkdir(parents=True, exist_ok=True)
    implementation_path.write_text(
        """schema: https://powdrr.io/schemas/specification-v1
architecture_id: acceptance-architecture
features:
  - id: acceptance-feature
    action: added
    description: Exercise structured delivery.
    functional_requirements:
      - Preserve the typed delivery boundary.
""",
        encoding="utf-8",
    )
    proposal_path = (
        artifact_root
        / "docs"
        / "proposals"
        / "acceptance-feature"
        / "proposed-pr-specification.yaml"
    )
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(
        """schema: https://powdrr.io/schemas/proposed-pr-specification-v1
id: acceptance-feature-pr
feature_ids: [acceptance-feature]
intent:
  problem: Exercise structured delivery.
  goal: Preserve the typed delivery boundary.
  reasoning: The runtime must retain high-level intent.
acceptance_criteria:
  - id: acceptance-criterion
    description: The typed delivery boundary is preserved.
expected_tests:
  - id: acceptance-test
    description: The vertical acceptance scenario passes.
required_test_cases:
  - id: acceptance-case
    description: The proposed PR is validated before execution.
expected_outcomes:
  - id: acceptance-outcome
    description: A typed execution plan can be compiled.
non_goals:
  - id: acceptance-non-goal
    description: External GitHub mutation.
risks:
  - id: acceptance-risk
    description: Fixture-only execution could bypass validation.
""",
        encoding="utf-8",
    )
    system_report = build_system_specification_validation_report(
        system_path.read_text(encoding="utf-8"),
        work_item_name="acceptance-feature",
        repo_root=artifact_root,
    )
    architecture_report = build_architecture_specification_validation_report(
        architecture_path.read_text(encoding="utf-8"),
        entity_types=("Service", "Skill"),
        work_item_name="acceptance-feature",
        repo_root=artifact_root,
    )
    implementation_report = build_implementation_specification_validation_report(
        implementation_path.read_text(encoding="utf-8"),
        work_item_name="acceptance-feature",
        architecture_specification_path=architecture_path,
        repo_root=artifact_root,
    )
    report = build_pr_specification_validation_report(
        proposal_path.read_text(encoding="utf-8"),
        work_item_name="acceptance-feature",
        repo_root=artifact_root,
        file_path=proposal_path,
    )
    return all(
        item.validation_successful
        for item in (system_report, architecture_report, implementation_report, report)
    )


def _run_production_task_adapter(workflow_directory: Path, repo_root: Path) -> Any:
    scenario_root = workflow_directory / "production-task"
    scenario_root.mkdir(parents=True, exist_ok=True)
    fixture_path = (
        repo_root / "workflow-evals/scenarios/execute-proposed-pr/fixtures/task-001"
    )
    workflow_path = scenario_root / "workflow"
    workflow_path.mkdir(parents=True, exist_ok=True)
    (workflow_path / "task-001.yaml").write_text(
        """\
task_id: task-001
status: open
upstream_task_ids: []
dependent_state: [proposed-pr-context-gathered]
complexity: high
input_state:
  proposed_pr: <proposed-pr-id>
  feature_id: acceptance-feature
assignee_type: agent
assignee_role: architect
output_state_type: proposed-pr-context-state
description: Gather context about the proposed PR
step_type: invoke_tool
actions: []
pre_step:
  action: gather_context
  template:
    feature_id: <feature_id>
    types:
    - proposed_prs
    - requirements
    - features
    - acceptance_criteria
    - expected_tests
    - intent
    - risks
    - decisions
""",
        encoding="utf-8",
    )
    scenario_path = scenario_root / "scenario.yaml"
    scenario_path.write_text(
        f"""\
schema_version: 1
id: acceptance-task
execution_mode: workflow_task
workflow_dir: {workflow_path}
work_item_name: acceptance-feature
task_id: task-001
fixture: {fixture_path}
provider:
  mode: scripted
  responses:
  - action: next_step
    output_state: $deterministic_pre_step
expect:
  output_state: $deterministic_pre_step
""",
        encoding="utf-8",
    )
    scenario = load_workflow_scenario(scenario_path)
    return run_workflow_scenario(
        scenario,
        scenario_path=scenario_path,
        repo_root=repo_root,
        guidance=("Always use optimistic locking for mutable row changes.",),
    )


def _run_production_chat_adapter(workflow_directory: Path, repo_root: Path) -> Any:
    scenario_root = workflow_directory / "production-chat"
    fixture_root = scenario_root / "fixture"
    fixture_root.mkdir(parents=True, exist_ok=True)
    (fixture_root / "example.txt").write_text("acceptance\n", encoding="utf-8")
    skill_path = scenario_root / "skill.yaml"
    skill_path.write_text(
        """\
name: acceptance-chat
when_to_use:
- Exercise the production chat adapter.
steps:
- id: inspect
  description: Inspect the fixture.
  actions: [invoke_tool, complete]
  step_type: freeform
  tool_invocations:
  - tool: shell
    command: [git, status, --short]
""",
        encoding="utf-8",
    )
    scenario_path = scenario_root / "scenario.yaml"
    scenario_path.write_text(
        f"""\
schema_version: 1
id: acceptance-chat
definition: {skill_path}
execution_mode: workflow_chat
fixture: fixture
request: Inspect the acceptance fixture.
provider:
  mode: scripted
  responses:
  - action: invoke_tool
    tool: shell
    parameters:
      command: [git, status, --short]
  - action: complete
    text: Inspection complete.
expect:
  outcome: complete
  visited_steps:
    ordered: [inspect]
  required_actions:
  - kind: invoke_tool
    tool: shell
  - kind: complete
  max_roundtrips: 2
""",
        encoding="utf-8",
    )
    return run_workflow_scenario(
        load_workflow_scenario(scenario_path),
        scenario_path=scenario_path,
        repo_root=repo_root,
        guidance=("Always use optimistic locking for mutable row changes.",),
    )


def _exchanges_contain_guidance(result: Any) -> bool:
    """Check the actual production adapter prompt payload, not a helper prompt."""
    exchanges = (
        result.get("exchanges", ())
        if isinstance(result, Mapping)
        else getattr(result, "llm_exchanges", ())
    )
    return "optimistic locking" in json.dumps(exchanges).casefold()


def _run_observer_acceptance(workflow_directory: Path) -> bool:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
            self.calls += 1
            return {
                "verdict": "coach",
                "reason": "The same action is repeating.",
                "guidance": ["Inspect the latest validation result."],
                "expected_progress": "validation state changes",
            }

    client = Client()
    observer = ShadowWorkflowObserver(
        client=client,
        model="acceptance-observer",
        provider="scripted",
        worktree_root=workflow_directory,
        log_root=workflow_directory,
        context_provider=lambda: ObserverExecutionContext(
            "workflow_chat",
            "acceptance",
            "acceptance-workflow",
            "build",
            "Build the fixture",
        ),
    )
    action = {"kind": "invoke_tool", "tool": "shell"}
    observation = WorkflowActionObservation(
        "same-action", False, ProgressDecision.THRESHOLD
    )
    first = observer.action_completed(action, observation)
    second = observer.action_completed(action, observation)
    restarted_client = Client()
    restarted = ShadowWorkflowObserver(
        client=restarted_client,
        model="acceptance-observer",
        provider="scripted",
        worktree_root=workflow_directory,
        log_root=workflow_directory,
        context_provider=lambda: ObserverExecutionContext(
            "workflow_chat",
            "acceptance",
            "acceptance-workflow",
            "build",
            "Build the fixture",
        ),
    )
    third = restarted.action_completed(action, observation)
    log_path = workflow_directory / "workflow-observer-events.jsonl"
    return (
        first is None
        and second is not None
        and third is None
        and client.calls == 1
        and restarted.state.last_decision is not None
        and restarted_client.calls == 0
        and log_path.is_file()
    )


def _walk_profile(
    runtime: ExecutionRuntime,
    profile: Any,
    *,
    boundary_contexts: list[dict[str, Any]] | None = None,
) -> tuple[tuple[bool, ...], bool]:
    """Drive the real phase controller through every profile assignment."""
    assignments = {phase.phase_type: phase for phase in profile.phases}
    route = (
        PhaseType.INTAKE,
        PhaseType.SPECIFY,
        PhaseType.REVIEW_SPECIFICATIONS,
        PhaseType.DECOMPOSE,
        PhaseType.REVIEW_PROPOSED_PRS,
        PhaseType.PLAN_PR,
        PhaseType.AWAIT_PLAN_DECISION,
        PhaseType.BUILD,
        PhaseType.VALIDATE,
        PhaseType.REVIEW_PR,
        PhaseType.RESOLVE_FINDINGS,
        PhaseType.VALIDATE,
        PhaseType.REVIEW_PR,
        PhaseType.CONFIRM_READINESS,
        PhaseType.PUBLISH_PR,
        PhaseType.COMPLETE_FEATURE,
    )
    transitions: list[bool] = []
    published = False
    for index, phase_type in enumerate(route):
        phase = assignments[phase_type]
        for artifact_index, artifact_type in enumerate(phase.output_artifacts):
            artifact = ExecutionArtifact(
                f"acceptance-{index}-{phase.phase_type.value}-{artifact_index}",
                artifact_type,
                "acceptance-v1",
                phase.persona_id,
                f"acceptance:{phase.phase_type.value}:{artifact_index}",
                accepted=True,
            )
            runtime.record_artifact(artifact)
            runtime.accept_artifact(artifact.artifact_id)
        if boundary_contexts is not None:
            boundary_contexts.append(
                runtime.compact_prompt_context(
                    {
                        "phase": phase.phase_type.value,
                        "tool_output": json.dumps(
                            runtime.prompt_context(), sort_keys=True
                        ),
                        "decision": f"phase-boundary-{phase.phase_type.value}",
                    }
                )
            )
        if phase_type is PhaseType.CONFIRM_READINESS:
            published = runtime.require_publish_readiness().ready
        if index + 1 == len(route):
            continue
        target = assignments[route[index + 1]]
        decision = runtime.transition(target.phase_type, persona_id=target.persona_id)
        transitions.append(decision.allowed)
        if not decision.allowed:
            break
    return tuple(transitions), published


class _AcceptanceClient:
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        raise AssertionError("the acceptance runner must use request_action")


class _ScriptedRunnerStrategy:
    """Minimal adapter that drives the production shared runner in CI."""

    def __init__(self, runtime: ExecutionRuntime, mode: str) -> None:
        self.runtime = runtime
        self.mode = mode
        self._requested = False
        self._material_state = 0

    def next_request(self) -> WorkflowActionRequest | None:
        if self._requested:
            return None
        self._requested = True
        return WorkflowActionRequest(
            client=_AcceptanceClient(),
            messages=[],
            parser=lambda payload: payload,
            model="acceptance",
            stderr=None,
            max_timeout_retries=0,
            timeout_backoff_seconds=0,
            request_action=lambda: WorkflowAction(kind="next_step"),
        )

    def report_roundtrip(self, roundtrip: int, action: Any) -> None:
        pass

    def execute_action(self, action: Any) -> WorkflowActionOutcome:
        self._material_state += 1
        return WorkflowActionOutcome(continue_running=False)

    def material_state(self, action: Any) -> object:
        return self._material_state

    def record_no_progress(self, action: Any, observation: Any) -> None:
        pass

    def record_response_error(self, error: RuntimeError, payload: Any) -> None:
        raise error

    def record_action_error(self, action: Any, error: Exception) -> None:
        raise error

    def action_failure_exit_code(self, action: Any) -> int:
        return 1

    def observe_outcome(
        self, action: Any, observation: Any, outcome: WorkflowActionOutcome
    ) -> WorkflowActionOutcome:
        return outcome

    def exhausted_roundtrips_exit_code(self) -> int:
        return 1


def _run_shared_runner(
    workflow_directory: str | Path, repo_root: Path, mode: str
) -> tuple[dict[str, Any], ...]:
    runtime = ExecutionRuntime(
        f"final-acceptance-{mode}",
        profile_id="default-software-delivery",
        workflow_directory=Path(workflow_directory) / mode,
        repo_root=repo_root,
    )
    strategy = _ScriptedRunnerStrategy(runtime, mode)
    runner = WorkflowStepRunner(
        max_stalled_roundtrips=1,
        runtime=runtime,
        phase_type=PhaseType.INTAKE.value,
        actor_id="architect",
    )
    exit_code = runner.run(strategy, max_roundtrips=1, signature=lambda _: "next-step")
    if exit_code != 0:
        raise PowdrrExecutionError(
            f"acceptance {mode} runner did not complete",
            error_code="acceptance_runner_incomplete",
            remediation=(
                "Inspect the durable runner events and correct the failed action."
            ),
        )
    return tuple(
        {
            "event_type": event.event_type.value,
            "payload": event.payload,
            "sequence": event.sequence,
        }
        for event in runtime.state_store.load_events(runtime.execution_id)
    )


def audit_capability_surface(registry: Any) -> tuple[AcceptanceCheck, ...]:
    """Verify every registered normal capability has an executable manifest."""
    manifests = tuple(registry.manifests())
    names = {manifest.tool_name for manifest in manifests}
    checks: list[AcceptanceCheck] = [
        AcceptanceCheck(
            "normal-capability-catalog",
            names == REQUIRED_BUILTIN_MANIFESTS,
            "registry contains exactly the declared normal capability surface",
        )
    ]
    for manifest in manifests:
        checks.append(
            AcceptanceCheck(
                f"manifest:{manifest.tool_name}",
                bool(manifest.semantic_actions and manifest.effects),
                "manifest declares semantic actions and effects",
            )
        )
    builtin_source = (
        Path(__file__).with_name("builtin_tools.py").read_text(encoding="utf-8")
    )
    checks.append(
        AcceptanceCheck(
            "normal-runtime-authority",
            "else CapabilityBroker" not in builtin_source
            and builtin_source.count("_require_runtime(runtime") >= 7,
            "normal builtin helpers cannot create an ephemeral broker",
        )
    )
    from powdrr_lift.execution import builtin_tools

    helpers = tuple(
        (name, helper)
        for name, helper in inspect.getmembers(builtin_tools, inspect.isfunction)
        if name.startswith("invoke_")
    )
    checks.append(
        AcceptanceCheck(
            "normal-helper-runtime-contract",
            bool(helpers)
            and all(
                "runtime" in inspect.signature(helper).parameters
                and "_require_runtime(runtime" in inspect.getsource(helper)
                for _, helper in helpers
            ),
            "every normal builtin helper requires the caller's durable runtime",
        )
    )
    return tuple(checks)
