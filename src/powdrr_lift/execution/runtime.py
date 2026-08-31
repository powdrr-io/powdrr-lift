"""Durable owner for the typed execution kernel and capability boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from powdrr_lift.core.behavior_rule import FileBehaviorRuleStore
from powdrr_lift.core.capability_exception import CapabilityExceptionAuthority
from powdrr_lift.core.delivery_profile import DeliveryProfile, PhaseType
from powdrr_lift.core.execution_plan import ExecutionPlan, FileExecutionPlanStore
from powdrr_lift.core.execution_state import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionFinding,
    ExecutionMode,
    ExecutionState,
)
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityDecision,
    CapabilityExceptionStore,
    CapabilityRequest,
    CapabilityResolution,
    FileCapabilityExceptionStore,
)
from powdrr_lift.execution.checkpoints import ContentAddressedCheckpointStore
from powdrr_lift.execution.compaction import (
    FileContextRetrievalStore,
    compact_execution_context,
    compact_with_retrieval,
)
from powdrr_lift.execution.compile import compile_execution_plan
from powdrr_lift.execution.evidence import ReadinessEvaluator, ReadinessReport
from powdrr_lift.execution.kernel import ActionKernel
from powdrr_lift.execution.personas import (
    PersonaPacket,
    build_persona_packet,
    validate_handoff,
)
from powdrr_lift.execution.phases import PhaseController, PhaseTransitionDecision
from powdrr_lift.execution.store import (
    ExecutionStateConflict,
    ExecutionStateStore,
    FileExecutionStateStore,
)
from powdrr_lift.execution.tools import (
    ToolAdapter,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


class ExecutionRuntime:
    """Own one execution's durable state, tools, checkpoints, and readiness."""

    def __init__(
        self,
        execution_id: str,
        *,
        profile_id: str,
        workflow_directory: str | Path,
        repo_root: str | Path,
        adapters: tuple[ToolAdapter, ...] = (),
        registry: ToolRegistry | None = None,
        exception_authority: CapabilityExceptionAuthority | None = None,
        exception_store: CapabilityExceptionStore | None = None,
        phase: PhaseType = PhaseType.INTAKE,
        mode: ExecutionMode = ExecutionMode.ENFORCE,
        state_store: ExecutionStateStore | None = None,
        behavior_rule_store: FileBehaviorRuleStore | None = None,
        profile: DeliveryProfile | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.profile = profile
        self.repo_root = Path(repo_root).resolve()
        self.state_store = state_store or FileExecutionStateStore(workflow_directory)
        try:
            self.state = self.state_store.load(execution_id)
        except (FileNotFoundError, IsADirectoryError):
            self.state = self.state_store.create(
                execution_id, profile_id=profile_id, phase=phase, mode=mode
            )
        self.kernel = ActionKernel()
        self.kernel.restore_obligations(self.state.obligations)
        self.phase_controller = PhaseController()
        self.readiness_evaluator = ReadinessEvaluator()
        self.behavior_rule_store = behavior_rule_store or FileBehaviorRuleStore(
            workflow_directory
        )
        self.plan_store = FileExecutionPlanStore(workflow_directory)
        self.context_store = FileContextRetrievalStore(workflow_directory)
        self.checkpoint_store = ContentAddressedCheckpointStore(
            Path(workflow_directory) / "execution" / "checkpoints"
        )
        self.broker = CapabilityBroker(
            registry or ToolRegistry(adapters),
            exception_authority=exception_authority,
            exception_store=exception_store
            or FileCapabilityExceptionStore(workflow_directory),
            checkpoint_store=self.checkpoint_store,
            state_json_provider=lambda _context: self.state.to_json(),
        )
        self._projected_kernel_events = 0

    def context(
        self,
        *,
        semantic_actions: frozenset[str],
        allowed_effects: frozenset[Any],
        active_unit_id: str | None = None,
    ) -> ToolContext:
        return ToolContext(
            self.repo_root,
            self.repo_root,
            semantic_actions,
            allowed_effects,
            execution_id=self.execution_id,
            active_unit_id=active_unit_id,
        )

    def readiness(self) -> ReadinessReport:
        return self.readiness_evaluator.evaluate(self.state)

    def prompt_context(self) -> dict[str, Any]:
        """Return the bounded typed state used at every prompt boundary."""
        return compact_execution_context(
            {
                "execution_id": self.execution_id,
                "phase": self.state.current_phase.value,
                "persona_id": self.state.current_persona_id,
                "artifact_ids": [item.artifact_id for item in self.state.artifacts],
                "action_ids": [item.action_instance_id for item in self.state.actions],
                "obligation_ids": [
                    item.obligation_id for item in self.state.obligations
                ],
                "evidence_ids": [item.evidence_id for item in self.state.evidence],
                "finding_ids": [item.finding_id for item in self.state.findings],
            }
        )

    def guidance(self, context: dict[str, str]) -> tuple[Any, ...]:
        """Retrieve active scoped behavior rules for the current execution."""
        from powdrr_lift.execution.guidance import load_applicable_guidance

        return load_applicable_guidance(self.behavior_rule_store, context)

    def remember_guidance(
        self, rule: Any, *, expected_version: int | None = None
    ) -> Any:
        """Persist a user-requested behavior rule with optimistic versioning."""
        return self.behavior_rule_store.save(rule, expected_version=expected_version)

    def revoke_guidance(self, rule_id: str, *, expected_version: int) -> Any:
        return self.behavior_rule_store.revoke(
            rule_id, expected_version=expected_version
        )

    def record_artifact(self, artifact: ExecutionArtifact) -> ExecutionState:
        return self._append_event(
            ExecutionEventType.ARTIFACT_PRODUCED, artifact.to_data()
        )

    def accept_artifact(self, artifact_id: str) -> ExecutionState:
        return self._append_event(
            ExecutionEventType.ARTIFACT_ACCEPTED, {"artifact_id": artifact_id}
        )

    def record_finding(self, finding: ExecutionFinding) -> ExecutionState:
        return self._append_event(ExecutionEventType.FINDING_OPENED, finding.to_data())

    def dispose_finding(self, finding_id: str, status: str) -> ExecutionState:
        return self._append_event(
            ExecutionEventType.FINDING_DISPOSED,
            {"finding_id": finding_id, "status": status},
        )

    def save_plan(self, plan: ExecutionPlan) -> Path:
        """Persist the plan artifact owned by this execution."""
        return self.plan_store.save(plan)

    def load_plan(self, plan_id: str) -> ExecutionPlan:
        return self.plan_store.load(plan_id)

    def compile_plan(
        self,
        profile: DeliveryProfile,
        plan: ExecutionPlan,
        *,
        actions_by_phase: dict[PhaseType, tuple[str, ...]],
    ) -> tuple[Any, ...]:
        """Compile a typed plan through the runtime-owned plan boundary."""
        self.save_plan(plan)
        return compile_execution_plan(profile, plan, actions_by_phase=actions_by_phase)

    def compact_prompt_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Compact arbitrary prompt data while retaining runtime references."""
        return compact_with_retrieval(
            {**context, "runtime_state": self.prompt_context()},
            self.context_store,
        )

    def retrieve_prompt_context(self, reference: str) -> dict[str, Any]:
        return self.context_store.load(reference)

    def persona_packet(
        self,
        profile: DeliveryProfile,
        *,
        run_id: str,
        phase_type: PhaseType,
        phase_actions: frozenset[str],
        persona_actions: dict[str, frozenset[str]],
        allowed_effects: frozenset[Any],
    ) -> PersonaPacket:
        """Build a least-privilege persona packet from durable runtime state."""
        return build_persona_packet(
            profile,
            execution_id=self.execution_id,
            run_id=run_id,
            phase_type=phase_type,
            phase_actions=phase_actions,
            persona_actions=persona_actions,
            allowed_effects=allowed_effects,
            input_artifact_ids=tuple(
                artifact.artifact_id for artifact in self.state.artifacts
            ),
            guidance_rules=self.guidance(
                {
                    "profile_id": profile.profile_id,
                    "phase_type": phase_type.value,
                }
            ),
        )

    def transition(
        self,
        target_phase: PhaseType,
        *,
        persona_id: str | None = None,
    ) -> PhaseTransitionDecision:
        """Validate and durably apply one closed-topology phase transition."""
        decision = self.phase_controller.evaluate(
            self.state,
            target_phase,
            open_obligations=tuple(
                obligation.description for obligation in self.kernel.open_obligations
            ),
        )
        if decision.allowed and self.profile is not None:
            handoff = validate_handoff(
                self.profile,
                self.state,
                source_phase=self.state.current_phase,
                destination_phase=target_phase,
                artifact_ids=tuple(
                    artifact.artifact_id for artifact in self.state.artifacts
                ),
            )
            if not handoff.valid:
                return PhaseTransitionDecision(
                    False,
                    decision.current_phase,
                    decision.target_phase,
                    handoff.errors,
                )
        if not decision.allowed:
            return decision
        sequence = self.state.event_sequence + 1
        event = ExecutionEvent(
            self.execution_id,
            sequence,
            self.state.state_version,
            ExecutionEventType.PHASE_ENTERED,
            {
                "phase_type": target_phase.value,
                "persona_id": persona_id,
            },
            f"{self.execution_id}:{sequence}",
        )
        self.state = self.state_store.append(
            self.execution_id, self.state.state_version, (event,)
        )
        return decision

    def publish_readiness(self, **kwargs: Any) -> ReadinessReport:
        """Evaluate the publish boundary using the current durable state."""
        return self.readiness_evaluator.evaluate(self.state, **kwargs)

    def verify(self) -> ExecutionState:
        """Rebuild state from the durable event log and verify its cache."""
        verify = getattr(self.state_store, "verify", None)
        if not callable(verify):
            return self.state
        self.state = verify(self.execution_id)
        return self.state

    def invoke(
        self, context: ToolContext, request: CapabilityRequest
    ) -> ToolResult | CapabilityResolution:
        """Invoke a capability and persist the broker decision."""
        before = len(self.broker.decision_log)
        result = self.broker.invoke(context, request)
        decisions = self.broker.decision_log[before:]
        if decisions:
            self._append_capability_decisions(decisions)
            if isinstance(result, ToolResult) and any(
                decision.kind.value == "executable" for decision in decisions
            ):
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "tool": request.tool_name,
                            "action": request.semantic_action,
                            "arguments": dict(request.arguments),
                        },
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()
                self.record_evidence(
                    evidence_id=f"evidence-{self.state.event_sequence + 1}",
                    producer_action_instance_id=(
                        context.active_unit_id or request.semantic_action
                    ),
                    evidence_type=f"capability:{request.tool_name}",
                    input_fingerprint=fingerprint,
                    successful=True,
                )
        return result

    def invoke_adapter(
        self,
        adapter: ToolAdapter,
        context: ToolContext,
        request: CapabilityRequest,
    ) -> ToolResult | CapabilityResolution:
        """Run a context-bound adapter through this runtime's broker."""
        self.broker.registry.replace(adapter)
        return self.invoke(context, request)

    def record_evidence(
        self,
        *,
        evidence_id: str,
        producer_action_instance_id: str,
        evidence_type: str,
        input_fingerprint: str,
        successful: bool,
    ) -> ExecutionState:
        """Append a typed validation result to the durable evidence ledger."""
        sequence = self.state.event_sequence + 1
        event = ExecutionEvent(
            self.execution_id,
            sequence,
            self.state.state_version,
            ExecutionEventType.EVIDENCE_RECORDED,
            {
                "evidence_id": evidence_id,
                "producer_action_instance_id": producer_action_instance_id,
                "evidence_type": evidence_type,
                "input_fingerprint": input_fingerprint,
                "successful": successful,
                "fresh": True,
            },
            f"{self.execution_id}:{sequence}",
        )
        self.state = self.state_store.append(
            self.execution_id, self.state.state_version, (event,)
        )
        return self.state

    def _append_event(
        self, event_type: ExecutionEventType, payload: dict[str, Any]
    ) -> ExecutionState:
        sequence = self.state.event_sequence + 1
        event = ExecutionEvent(
            self.execution_id,
            sequence,
            self.state.state_version,
            event_type,
            payload,
            f"{self.execution_id}:{sequence}",
        )
        self.state = self.state_store.append(
            self.execution_id, self.state.state_version, (event,)
        )
        return self.state

    def sync_kernel(self, *, phase_type: str, actor_id: str) -> ExecutionState:
        pending = self.kernel.events[self._projected_kernel_events :]
        if not pending:
            return self.state
        durable_events = self.kernel.to_execution_events(
            self.execution_id,
            phase_type=phase_type,
            actor_id=actor_id,
            starting_state=self.state,
            events=pending,
        )
        try:
            self.state = self.state_store.append(
                self.execution_id, self.state.state_version, durable_events
            )
        except ExecutionStateConflict:
            # A chat/session adapter may recreate its materialized state while
            # retaining the in-memory kernel. Reload and replay the typed
            # lifecycle stream so recovery remains deterministic.
            self.state = self.state_store.load(self.execution_id)
            replay = self.kernel.to_execution_events(
                self.execution_id,
                phase_type=phase_type,
                actor_id=actor_id,
                starting_state=self.state,
            )
            self.state = self.state_store.append(
                self.execution_id, self.state.state_version, replay
            )
        self._projected_kernel_events = len(self.kernel.events)
        return self.state

    def _append_capability_decisions(
        self, decisions: tuple[CapabilityDecision, ...]
    ) -> None:
        events: list[ExecutionEvent] = []
        state_version = self.state.state_version
        sequence = self.state.event_sequence
        for decision in decisions:
            sequence += 1
            events.append(
                ExecutionEvent(
                    self.execution_id,
                    sequence,
                    state_version,
                    ExecutionEventType.CAPABILITY_DECISION,
                    decision.to_data(),
                    f"{self.execution_id}:{sequence}",
                )
            )
            state_version += 1
        self.state = self.state_store.append(
            self.execution_id, self.state.state_version, events
        )
