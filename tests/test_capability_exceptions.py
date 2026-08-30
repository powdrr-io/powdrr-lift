from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from powdrr_lift.core.capability_exception import CapabilityExceptionAuthority
from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityRequest,
    CapabilityResolutionKind,
    FileCapabilityExceptionStore,
)
from powdrr_lift.execution.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolValidationReport,
)


class WriteTool:
    manifest = ToolManifest("write-file", ("edit",), (ToolEffect.WORKSPACE_WRITE,))

    def validate(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> ToolValidationReport:
        return ToolValidationReport()

    def execute(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> ToolResult:
        return ToolResult()


def test_approved_exception_binds_exact_arguments(tmp_path: Path) -> None:
    broker = CapabilityBroker(
        ToolRegistry([WriteTool()]), CapabilityExceptionAuthority(b"secret")
    )
    context = ToolContext(
        tmp_path, tmp_path, frozenset({"edit"}), frozenset(), execution_id="run-1"
    )
    original = CapabilityRequest("write-file", "edit", {"path": "one.txt"})
    exception = broker.create_exception_request(
        context,
        original,
        "required external approval",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    assert exception is not None
    decision = broker.decide_exception(exception, approved=True, decided_by="human")
    approved = broker.resolve(
        context,
        CapabilityRequest("write-file", "edit", {"path": "one.txt"}, decision.token),
    )
    altered = broker.resolve(
        context,
        CapabilityRequest("write-file", "edit", {"path": "two.txt"}, decision.token),
    )
    assert approved.kind is CapabilityResolutionKind.EXECUTABLE
    assert altered.kind is CapabilityResolutionKind.EXCEPTION_REQUIRED


def test_exception_requires_execution_context(tmp_path: Path) -> None:
    broker = CapabilityBroker(
        ToolRegistry([WriteTool()]), CapabilityExceptionAuthority(b"secret")
    )
    context = ToolContext(tmp_path, tmp_path, frozenset({"edit"}), frozenset())
    assert (
        broker.create_exception_request(
            context,
            CapabilityRequest("write-file", "edit", {"path": "one.txt"}),
            "missing context",
            expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )
        is None
    )


def test_pending_and_denied_exception_decisions_are_durable(tmp_path: Path) -> None:
    store = FileCapabilityExceptionStore(tmp_path)
    broker = CapabilityBroker(
        ToolRegistry([WriteTool()]),
        CapabilityExceptionAuthority(b"secret"),
        store,
    )
    context = ToolContext(
        tmp_path, tmp_path, frozenset({"edit"}), frozenset(), execution_id="run-2"
    )
    request = CapabilityRequest("write-file", "edit", {"path": "one.txt"})
    exception = broker.create_exception_request(
        context,
        request,
        "needs review",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    assert exception is not None
    assert (
        tmp_path / "execution/exceptions/run-2_write-file_edit.request.json"
    ).exists()
    broker.decide_exception(exception, approved=False, decided_by="human")
    assert (tmp_path / "execution/exceptions/run-2_write-file_edit.json").exists()
    assert len(broker.decision_log) == 1
