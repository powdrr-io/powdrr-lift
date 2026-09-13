"""Architecture tests for the agent/definition separation seam."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "powdrr_lift"


def test_language_and_agent_package_surfaces_exist() -> None:
    for package in ("structrr", "process", "workrr"):
        assert (SOURCE_ROOT / package / "__init__.py").is_file()


def test_structrr_language_does_not_import_runtime_or_workrr_modules() -> None:
    forbidden_prefixes = (
        "powdrr_lift.workrr",
        "powdrr_lift.process",
        "powdrr_lift.execution",
        "powdrr_lift.workflow_chat_agent",
        "powdrr_lift.workflow_task_agent",
    )
    for path in _python_files("structrr"):
        imports = _imported_modules(path)
        assert not any(
            module.startswith(forbidden)
            for module in imports
            for forbidden in forbidden_prefixes
        ), path


def test_process_language_does_not_import_workrr_or_runtime_modules() -> None:
    forbidden_prefixes = (
        "powdrr_lift.workrr",
        "powdrr_lift.execution",
        "powdrr_lift.workflow_chat_agent",
        "powdrr_lift.workflow_task_agent",
    )
    for path in _python_files("process"):
        imports = _imported_modules(path)
        assert not any(
            module.startswith(forbidden)
            for module in imports
            for forbidden in forbidden_prefixes
        ), path


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _python_files(package: str) -> tuple[Path, ...]:
    return tuple((SOURCE_ROOT / package).rglob("*.py"))


def test_contracts_do_not_import_runtime_definition_or_provider_modules() -> None:
    forbidden_prefixes = (
        "powdrr_lift.workrr",
        "powdrr_lift.definitions",
        "powdrr_lift.execution",
        "powdrr_lift.workflow_chat_agent",
        "powdrr_lift.workflow_task_agent",
    )
    for path in _python_files("contracts"):
        imports = _imported_modules(path)
        assert not any(
            module.startswith(forbidden)
            for module in imports
            for forbidden in forbidden_prefixes
        ), path


def test_definitions_do_not_import_workrr_or_provider_modules() -> None:
    forbidden_prefixes = (
        "powdrr_lift.workrr",
        "powdrr_lift.workflow_chat_agent",
        "powdrr_lift.workflow_task_agent",
        "powdrr_lift.openai_proxy",
    )
    for path in _python_files("definitions"):
        imports = _imported_modules(path)
        assert not any(
            module.startswith(forbidden)
            for module in imports
            for forbidden in forbidden_prefixes
        ), path


def test_workrr_protocol_depends_only_on_contracts() -> None:
    imports = _imported_modules(SOURCE_ROOT / "workrr" / "protocol.py")
    forbidden_prefixes = (
        "powdrr_lift.core",
        "powdrr_lift.definitions",
        "powdrr_lift.execution",
        "powdrr_lift.workflow_chat_agent",
        "powdrr_lift.workflow_task_agent",
    )
    assert not any(
        module.startswith(forbidden)
        for module in imports
        for forbidden in forbidden_prefixes
    )


def test_workrr_package_does_not_import_definition_or_execution_implementations() -> (
    None
):
    forbidden_prefixes = (
        "powdrr_lift.core",
        "powdrr_lift.definitions",
        "powdrr_lift.execution",
        "powdrr_lift.workflow_chat_agent",
        "powdrr_lift.workflow_task_agent",
    )
    for path in _python_files("workrr"):
        imports = _imported_modules(path)
        assert not any(
            module.startswith(forbidden)
            for module in imports
            for forbidden in forbidden_prefixes
        ), path


def test_proposal_round_keeps_kernel_as_transition_owner() -> None:
    from powdrr_lift.contracts import (
        AgentInput,
        AgentProposal,
        KernelResult,
        Observation,
        ProcedureView,
        StateProjection,
    )
    from powdrr_lift.workrr import run_proposal_round

    class FakeClient:
        def propose(self, agent_input: AgentInput) -> AgentProposal:
            assert agent_input.procedure.step_id == "step"
            return AgentProposal(action={"action": "inspect"})

    class FakeKernel:
        def process_proposal(self, proposal: AgentProposal) -> KernelResult:
            assert proposal.action == {"action": "inspect"}
            return KernelResult(
                operation_id="operation-1",
                operation_status="succeeded",
                observation=Observation("tool_result", {"ok": True}, "kernel"),
                transition="next",
            )

    result = run_proposal_round(
        FakeClient(),
        FakeKernel(),
        AgentInput(
            procedure=ProcedureView(
                "definition",
                "fingerprint",
                "step",
                "Inspect",
                ("inspect",),
                ("done",),
            ),
            state=StateProjection("execution", "activation", "step", {}, {}),
        ),
    )
    assert result.transition == "next"
