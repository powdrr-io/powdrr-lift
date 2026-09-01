"""Deterministic final acceptance and runtime-surface audits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from powdrr_lift.core.capability_exception import CapabilityExceptionAuthority
from powdrr_lift.core.delivery_profile import PhaseType, load_delivery_profile
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.core.execution_state import ExecutionArtifact
from powdrr_lift.core.pr_specification import build_pr_specification_validation_report
from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.execution.capabilities import CapabilityRequest
from powdrr_lift.execution.compile import compile_execution_plan
from powdrr_lift.execution.evidence import EvidenceRequirement
from powdrr_lift.execution.kernel import ActionKernel
from powdrr_lift.execution.personas import build_persona_packet
from powdrr_lift.execution.runtime import ExecutionRuntime
from powdrr_lift.execution.tools import ToolContext, ToolResult, ToolValidationReport
from powdrr_lift.workflow_llm import (
    WorkflowAction,
    WorkflowActionOutcome,
    WorkflowActionRequest,
    WorkflowStepRunner,
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


def run_final_acceptance(
    repo_root: str | Path, workflow_directory: str | Path
) -> AcceptanceReport:
    """Exercise the typed path without an LLM or external GitHub mutation."""
    root = Path(repo_root)
    profile = load_delivery_profile(
        root / "delivery-profiles/default-software-delivery.yaml"
    )
    plan = ExecutionPlan(
        "final-acceptance-plan",
        "final-acceptance-pr",
        (
            ExecutionUnit(
                "feature",
                "Exercise the complete typed delivery path",
                ("src/feature.py",),
                validation_profiles=("repository-validation",),
                acceptance_criteria=("the typed path is auditable",),
            ),
        ),
        ("src",),
    )
    actions: dict[PhaseType, tuple[str, ...]] = {
        phase.phase_type: ("next_step",) for phase in profile.phases
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
    phase_transitions, published = _walk_profile(runtime, profile)
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

    review_kernel = ActionKernel()
    review_kernel.propose(
        {"kind": "review-edit", "attributes": ["thread:R123"]},
        semantic_action="edit_for_review_comment",
    )
    blocked_resolution = review_kernel.validate_proposal(
        {"kind": "resolve", "thread": "R123"},
        semantic_action="resolve_review_thread",
    )
    review_kernel.complete({"kind": "validation", "semantic_action": "run_validation"})
    allowed_resolution = review_kernel.validate_proposal(
        {"kind": "resolve", "thread": "R123"},
        semantic_action="resolve_review_thread",
    )
    review_kernel.complete(
        {"kind": "resolve", "semantic_action": "resolve_review_thread"}
    )
    runtime.kernel = review_kernel
    # This acceptance fixture swaps in a deliberately prepared kernel; normal
    # runtimes retain one kernel for their entire execution.
    runtime._projected_kernel_events = 0
    runtime.sync_kernel(phase_type=PhaseType.BUILD.value, actor_id="engineer")

    mutable_kernel = ActionKernel()
    mutable_kernel.propose({"kind": "row-change"}, semantic_action="change_mutable_row")
    mutable_actions = {item.required_action for item in mutable_kernel.open_obligations}
    chat_sequence = _run_shared_runner(workflow_directory, root, "chat")
    task_sequence = _run_shared_runner(workflow_directory, root, "task")
    production_task = _run_production_task_adapter(Path(workflow_directory), root)
    production_chat = _run_production_chat_adapter(Path(workflow_directory), root)
    structured_artifacts = _run_structured_artifact_chain(Path(workflow_directory))
    compacted = runtime.compact_prompt_context(
        {
            "transcript": "x" * 2_000,
            "contract_fingerprint": contract["contract_fingerprint"],
        }
    )
    retrieved = runtime.retrieve_prompt_context(compacted["full_context_ref"])
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
    checks = (
        AcceptanceCheck(
            "compiled-task-graph",
            len(tasks) == len(profile.phases) == len(workflow.tasks),
            f"compiled {len(workflow.tasks)} phase tasks with profile assignments",
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
            bool(blocked_resolution)
            and not allowed_resolution
            and not review_kernel.open_obligations,
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
    """Validate high-level intent artifacts before compiling execution work."""
    artifact_root = workflow_directory / "structured-artifacts"
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
    report = build_pr_specification_validation_report(
        proposal_path.read_text(encoding="utf-8"),
        work_item_name="acceptance-feature",
        repo_root=artifact_root,
        file_path=proposal_path,
    )
    return report.validation_successful


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
actions: [next_step]
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
  actions: [invoke_tool, complete, next_step]
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
    )


def _walk_profile(
    runtime: ExecutionRuntime, profile: Any
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
    return tuple(checks)
