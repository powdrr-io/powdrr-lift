from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from powdrr_lift.core.capability_exception import CapabilityExceptionAuthority
from powdrr_lift.core.execution_state import ExecutionEventType
from powdrr_lift.core.tool_manifest import ToolEffect, ToolManifest
from powdrr_lift.execution.capabilities import (
    CapabilityBroker,
    CapabilityRequest,
    CapabilityResolutionKind,
    FileCapabilityExceptionStore,
)
from powdrr_lift.execution.runtime import ExecutionRuntime
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


def test_file_store_lists_pending_requests_and_decision_packet(tmp_path: Path) -> None:
    from powdrr_lift.execution.capabilities import FileCapabilityExceptionStore

    store = FileCapabilityExceptionStore(tmp_path)
    broker = CapabilityBroker(
        ToolRegistry([WriteTool()]),
        CapabilityExceptionAuthority(b"secret"),
        store,
    )
    context = ToolContext(
        tmp_path, tmp_path, frozenset({"edit"}), frozenset(), execution_id="run-3"
    )
    request = broker.create_exception_request(
        context,
        CapabilityRequest("write-file", "edit", {"path": "one.txt"}),
        "needs review",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    assert request is not None
    pending = store.pending()
    assert [item.exception_id for item in pending] == [request.exception_id]
    assert pending[0].decision_packet()["arguments"] == {"path": "one.txt"}


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


def test_exception_use_count_survives_broker_restart(tmp_path: Path) -> None:
    store = FileCapabilityExceptionStore(tmp_path)
    authority = CapabilityExceptionAuthority(b"secret")
    context = ToolContext(
        tmp_path, tmp_path, frozenset({"edit"}), frozenset(), execution_id="run-4"
    )
    request = CapabilityRequest("write-file", "edit", {"path": "one.txt"})
    broker = CapabilityBroker(ToolRegistry([WriteTool()]), authority, store)
    exception = broker.create_exception_request(
        context,
        request,
        "needs review",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    assert exception is not None
    decision = broker.decide_exception(exception, approved=True, decided_by="human")
    approved = broker.resolve(
        context,
        CapabilityRequest(
            request.tool_name,
            request.semantic_action,
            request.arguments,
            decision.token,
        ),
    )
    assert approved.kind is CapabilityResolutionKind.EXECUTABLE

    restarted = CapabilityBroker(ToolRegistry([WriteTool()]), authority, store)
    replay = restarted.resolve(
        context,
        CapabilityRequest(
            request.tool_name,
            request.semantic_action,
            request.arguments,
            decision.token,
        ),
    )
    assert replay.kind is CapabilityResolutionKind.EXCEPTION_REQUIRED
    stored = store.load(exception.exception_id)
    assert stored is not None and stored[1].uses == 1


def test_runtime_exception_flow_resumes_exact_request(tmp_path: Path) -> None:
    store = FileCapabilityExceptionStore(tmp_path / "workflow")
    runtime = ExecutionRuntime(
        "run-runtime-exception",
        profile_id="default",
        workflow_directory=tmp_path / "workflow",
        repo_root=tmp_path,
        registry=ToolRegistry([WriteTool()]),
        exception_authority=CapabilityExceptionAuthority(b"secret"),
        exception_store=store,
    )
    context = ToolContext(
        tmp_path,
        tmp_path,
        frozenset({"edit"}),
        frozenset(),
        execution_id="run-runtime-exception",
    )
    request = CapabilityRequest("write-file", "edit", {"path": "one.txt"})
    exception = runtime.request_capability_exception(
        context,
        request,
        "needs human approval",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    decision = runtime.decide_capability_exception(
        exception, approved=True, decided_by="human"
    )

    result = runtime.invoke_approved_exception(context, request, decision)

    assert isinstance(result, ToolResult)
    assert any(
        event.event_type is ExecutionEventType.CAPABILITY_DECISION
        for event in runtime.state_store.load_events("run-runtime-exception")
    )


def test_exception_decision_is_idempotent_and_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    store = FileCapabilityExceptionStore(tmp_path)
    authority = CapabilityExceptionAuthority(b"secret")
    context = ToolContext(
        tmp_path, tmp_path, frozenset({"edit"}), frozenset(), execution_id="run-5"
    )
    request = CapabilityRequest("write-file", "edit", {"path": "one.txt"})
    broker = CapabilityBroker(ToolRegistry([WriteTool()]), authority, store)
    exception = broker.create_exception_request(
        context,
        request,
        "needs review",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    assert exception is not None
    first = broker.decide_exception(exception, approved=True, decided_by="human")
    repeated = broker.decide_exception(exception, approved=True, decided_by="human")
    assert repeated == first
    with pytest.raises(ValueError, match="already has a decision"):
        broker.decide_exception(exception, approved=False, decided_by="human")
