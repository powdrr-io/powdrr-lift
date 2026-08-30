from pathlib import Path
from typing import Any

from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityRequest,
    CapabilityResolutionKind,
)
from powdrr_lift.execution.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolValidationReport,
)


class FakeTool:
    manifest = ToolManifest(
        tool_name="read-file",
        semantic_actions=("inspect",),
        effects=(ToolEffect.WORKSPACE_READ,),
    )

    def validate(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolValidationReport:
        return ToolValidationReport()

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            output=arguments["path"],
            observed_effects=frozenset({ToolEffect.WORKSPACE_READ}),
        )


def context(
    root: Path, actions: frozenset[str] = frozenset({"inspect"})
) -> ToolContext:
    return ToolContext(root, root, actions, frozenset({ToolEffect.WORKSPACE_READ}))


def test_manifest_fingerprint_is_stable() -> None:
    manifest = FakeTool.manifest
    assert manifest.fingerprint() == manifest.fingerprint()
    assert manifest.to_data()["schema_version"] == "tool-manifest-v1"


def test_registry_rejects_duplicate_tools() -> None:
    registry = ToolRegistry([FakeTool()])
    try:
        registry.register(FakeTool())
    except ValueError as error:
        assert "already registered" in str(error)
    else:
        raise AssertionError("duplicate registration should fail")


def test_broker_executes_in_scope_request(tmp_path: Path) -> None:
    broker = CapabilityBroker(ToolRegistry([FakeTool()]))
    request = CapabilityRequest("read-file", "inspect", {"path": "src/main.py"})
    resolution = broker.resolve(context(tmp_path), request)
    assert resolution.kind is CapabilityResolutionKind.EXECUTABLE
    result = broker.invoke(context(tmp_path), request)
    assert isinstance(result, ToolResult)


def test_broker_corrects_path_escape(tmp_path: Path) -> None:
    broker = CapabilityBroker(ToolRegistry([FakeTool()]))
    request = CapabilityRequest("read-file", "inspect", {"path": "../secrets.txt"})
    resolution = broker.resolve(context(tmp_path), request)
    assert resolution.kind is CapabilityResolutionKind.CORRECTABLE


def test_broker_rejects_action_not_allowed_by_step(tmp_path: Path) -> None:
    broker = CapabilityBroker(ToolRegistry([FakeTool()]))
    request = CapabilityRequest("read-file", "inspect", {"path": "file.txt"})
    resolution = broker.resolve(context(tmp_path, frozenset()), request)
    assert resolution.kind is CapabilityResolutionKind.DENIED


def test_broker_requires_exception_for_unavailable_effect(tmp_path: Path) -> None:
    class MutatingTool(FakeTool):
        manifest = ToolManifest("write-file", ("edit",), (ToolEffect.WORKSPACE_WRITE,))

    broker = CapabilityBroker(ToolRegistry([MutatingTool()]))
    request = CapabilityRequest("write-file", "edit", {"path": "file.txt"})
    resolution = broker.resolve(context(tmp_path, frozenset({"edit"})), request)
    assert resolution.kind is CapabilityResolutionKind.EXCEPTION_REQUIRED
