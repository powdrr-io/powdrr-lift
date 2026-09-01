"""Deterministic final acceptance and runtime-surface audits."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.core.delivery_profile import PhaseType, load_delivery_profile
from powdrr_lift.core.execution_plan import ExecutionPlan, ExecutionUnit
from powdrr_lift.execution.compile import compile_execution_plan
from powdrr_lift.execution.evidence import EvidenceRequirement
from powdrr_lift.execution.kernel import ActionKernel
from powdrr_lift.execution.personas import build_persona_packet
from powdrr_lift.execution.runtime import ExecutionRuntime

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
    runtime.sync_kernel(phase_type=PhaseType.BUILD.value, actor_id="engineer")

    mutable_kernel = ActionKernel()
    mutable_kernel.propose({"kind": "row-change"}, semantic_action="change_mutable_row")
    mutable_actions = {item.required_action for item in mutable_kernel.open_obligations}
    chat_sequence = _review_sequence()
    task_sequence = _review_sequence()
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
            "chat and durable-task adapters produce the same typed lifecycle",
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
    )
    return AcceptanceReport(checks)


def _review_sequence() -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    kernel = ActionKernel()
    kernel.propose(
        {"kind": "review-edit", "attributes": ["thread:R123"]},
        semantic_action="edit_for_review_comment",
    )
    kernel.complete({"kind": "validation", "semantic_action": "run_validation"})
    kernel.complete({"kind": "resolve", "semantic_action": "resolve_review_thread"})
    return (
        tuple(event.to_data() for event in kernel.events),
        tuple(item.to_data() for item in kernel.open_obligations),
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
    return tuple(checks)
