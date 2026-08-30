from pathlib import Path

from powdrr_lift.core.tool_manifest import ToolEffect
from powdrr_lift.execution.builtin_tools import builtin_tool_registry
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityRequest,
    CapabilityResolutionKind,
)
from powdrr_lift.execution.tools import ToolContext, ToolResult


def test_builtin_repository_inspection_resolves_through_broker(tmp_path: Path) -> None:
    broker = CapabilityBroker(builtin_tool_registry())
    context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset({"inspect_repository"}),
        frozenset({ToolEffect.WORKSPACE_READ}),
    )
    result = broker.invoke(
        context,
        CapabilityRequest("repository", "inspect_repository", {"operation": "status"}),
    )
    assert isinstance(result, ToolResult)
    assert result.output["command"] == ["git", "status", "--short"]


def test_builtin_mutation_requires_the_declared_effect(tmp_path: Path) -> None:
    broker = CapabilityBroker(builtin_tool_registry())
    context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset({"mutate_repository"}),
        frozenset({ToolEffect.WORKSPACE_READ}),
    )
    result = broker.resolve(
        context,
        CapabilityRequest(
            "repository",
            "mutate_repository",
            {"operation": "add", "paths": ["file.txt"]},
        ),
    )
    assert result.kind is CapabilityResolutionKind.EXCEPTION_REQUIRED
