"""Static validation and conservative cost analysis for procedrr workflows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from procedrr.model import (
    CallNode,
    ControlNode,
    ForEachNode,
    Guarantee,
    JudgeNode,
    MatchNode,
    OperationNode,
    ParallelNode,
    ProofStatus,
    RepeatNode,
    RetryNode,
    SequenceNode,
    SuspendNode,
    TerminalNode,
    WorkflowDefinition,
    WorklistNode,
)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class SafetyCertificate:
    guarantees: dict[Guarantee, ProofStatus]

    def status(self, guarantee: Guarantee) -> ProofStatus:
        return self.guarantees.get(guarantee, ProofStatus.UNKNOWN)

    def to_data(self) -> dict[str, str]:
        return {key.value: value.value for key, value in self.guarantees.items()}


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    definition: WorkflowDefinition
    certificate: SafetyCertificate
    max_llm_activations: int
    max_tool_calls: int
    max_epochs: int

    @property
    def fingerprint(self) -> str:
        return self.definition.fingerprint


@dataclass(frozen=True, slots=True)
class CompilationReport:
    workflow: CompiledWorkflow | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def successful(self) -> bool:
        return not self.diagnostics and self.workflow is not None


class CompilationError(ValueError):
    """Raised when strict compilation finds one or more diagnostics."""

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        detail = "; ".join(
            f"{item.path}: {item.code}: {item.message}" for item in self.diagnostics
        )
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _Cost:
    llm: int = 0
    tools: int = 0
    epochs: int = 0

    def plus(self, other: _Cost) -> _Cost:
        return _Cost(
            self.llm + other.llm, self.tools + other.tools, self.epochs + other.epochs
        )

    def multiply(self, factor: int) -> _Cost:
        return _Cost(self.llm * factor, self.tools * factor, self.epochs * factor)


def compile_workflow(definition: WorkflowDefinition) -> CompiledWorkflow:
    """Compile a workflow or raise :class:`CompilationError`.

    All accepted cycles are represented by ``RepeatNode``, ``RetryNode``,
    ``ForEachNode``, or ``WorklistNode`` and therefore have finite structural
    bounds. Arbitrary graph back-edges are not part of the source model.
    """

    report = analyze_workflow(definition)
    if not report.successful:
        raise CompilationError(report.diagnostics)
    assert report.workflow is not None
    return report.workflow


def analyze_workflow(definition: WorkflowDefinition) -> CompilationReport:
    diagnostics: list[Diagnostic] = []
    cost = _analyze_node(definition.body, "body", diagnostics)

    if cost.llm > definition.limits.llm_activations:
        diagnostics.append(
            Diagnostic(
                "llm_activation_budget_exceeded",
                "body",
                f"worst-case {cost.llm} activations exceed "
                f"the limit of {definition.limits.llm_activations}",
            )
        )
    if cost.tools > definition.limits.tool_calls:
        diagnostics.append(
            Diagnostic(
                "tool_call_budget_exceeded",
                "body",
                f"worst-case {cost.tools} calls exceed "
                f"the limit of {definition.limits.tool_calls}",
            )
        )
    if cost.epochs > definition.limits.max_epochs:
        diagnostics.append(
            Diagnostic(
                "epoch_budget_exceeded",
                "body",
                f"worst-case {cost.epochs} epochs exceed "
                f"the limit of {definition.limits.max_epochs}",
            )
        )

    proven = not diagnostics
    statuses = {
        Guarantee.DECISION_SAFE: (
            ProofStatus.PROVEN if proven else ProofStatus.DISPROVEN
        ),
        Guarantee.CONTROL_TERMINATION_SAFE: (
            ProofStatus.PROVEN if proven else ProofStatus.DISPROVEN
        ),
        Guarantee.OPERATION_TERMINATION_SAFE: (
            ProofStatus.PROVEN if proven else ProofStatus.DISPROVEN
        ),
        Guarantee.RESOURCE_SAFE: ProofStatus.PROVEN
        if proven
        else ProofStatus.DISPROVEN,
        Guarantee.TERMINATION_SAFE: ProofStatus.PROVEN
        if proven
        else ProofStatus.DISPROVEN,
    }
    compiled = CompiledWorkflow(
        definition,
        SafetyCertificate(statuses),
        cost.llm,
        cost.tools,
        cost.epochs,
    )
    return CompilationReport(compiled, tuple(diagnostics))


def _analyze_node(node: ControlNode, path: str, diagnostics: list[Diagnostic]) -> _Cost:
    if isinstance(node, JudgeNode):
        decision = node.decision
        if not decision.subject.strip():
            diagnostics.append(
                Diagnostic("missing_decision_subject", path, "a subject is required")
            )
        if decision.transport_action_const in {"next_step", "complete", "retry"}:
            diagnostics.append(
                Diagnostic(
                    "model_owned_transition",
                    path,
                    "the transport action must be a dedicated constant",
                )
            )
        return _Cost(llm=1)

    if isinstance(node, OperationNode):
        return _Cost(tools=1)

    if isinstance(node, SequenceNode):
        if not node.nodes:
            diagnostics.append(
                Diagnostic("empty_sequence", path, "a sequence needs a node")
            )
        return _sum_costs(
            _analyze_node(child, f"{path}.nodes[{index}]", diagnostics)
            for index, child in enumerate(node.nodes)
        )

    if isinstance(node, MatchNode):
        if node.otherwise is None:
            diagnostics.append(
                Diagnostic(
                    "non_exhaustive_match", path, "a match needs cases or otherwise"
                )
            )
        values: list[Any] = []
        costs: list[_Cost] = []
        for index, case in enumerate(node.cases):
            try:
                if case.value in values:
                    diagnostics.append(
                        Diagnostic(
                            "overlapping_match_case",
                            f"{path}.cases[{index}]",
                            "case values overlap",
                        )
                    )
                values.append(case.value)
            except TypeError:
                diagnostics.append(
                    Diagnostic(
                        "unhashable_match_value",
                        f"{path}.cases[{index}]",
                        "case values must be comparable",
                    )
                )
            costs.append(
                _analyze_node(case.body, f"{path}.cases[{index}].body", diagnostics)
            )
        if node.otherwise is not None:
            costs.append(
                _analyze_node(node.otherwise, f"{path}.otherwise", diagnostics)
            )
        return _max_costs(costs)

    if isinstance(node, ForEachNode):
        body = _analyze_node(node.body, f"{path}.body", diagnostics)
        return body.multiply(node.snapshot.max_items * (node.item_retry_budget + 1))

    if isinstance(node, WorklistNode):
        body = _analyze_node(node.body, f"{path}.body", diagnostics)
        return body.multiply(node.max_admissions * node.max_epochs)

    if isinstance(node, RepeatNode):
        return _analyze_node(node.body, f"{path}.body", diagnostics).multiply(
            node.budget
        )

    if isinstance(node, RetryNode):
        return _analyze_node(node.body, f"{path}.body", diagnostics).multiply(
            node.budget + 1
        )

    if isinstance(node, ParallelNode):
        if not node.branches:
            diagnostics.append(
                Diagnostic("empty_parallel", path, "parallel needs a branch")
            )
        return _sum_costs(
            _analyze_node(branch, f"{path}.branches[{index}]", diagnostics)
            for index, branch in enumerate(node.branches)
        )

    if isinstance(node, CallNode):
        return _analyze_node(node.body, f"{path}.body", diagnostics)

    if isinstance(node, (SuspendNode, TerminalNode)):
        return _Cost()

    diagnostics.append(Diagnostic("unknown_control_node", path, type(node).__name__))
    return _Cost()


def _sum_costs(costs: Iterable[_Cost]) -> _Cost:
    result = _Cost()
    for cost in costs:
        result = result.plus(cost)
    return result


def _max_costs(costs: Sequence[_Cost]) -> _Cost:
    if not costs:
        return _Cost()
    return _Cost(
        max(cost.llm for cost in costs),
        max(cost.tools for cost in costs),
        max(cost.epochs for cost in costs),
    )


__all__ = [
    "CompilationError",
    "CompilationReport",
    "CompiledWorkflow",
    "Diagnostic",
    "SafetyCertificate",
    "analyze_workflow",
    "compile_workflow",
]
