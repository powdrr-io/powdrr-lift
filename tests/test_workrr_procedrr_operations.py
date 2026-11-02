from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from powdrr_service.procedrr.command_catalog import (
    CommandCatalog,
    CommandSpec,
    object_schema,
)

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.procedrr_operations import WorkrrOperationExecutor


def test_executor_dispatches_only_to_caller_authorized_handler() -> None:
    calls: list[dict[str, object]] = []

    def read_document(parameters: Mapping[str, Any]) -> str:
        calls.append(dict(parameters))
        return "ok"

    executor = WorkrrOperationExecutor({"read_document": read_document})

    assert executor("read_document", {"file_path": "README.md"}) == "ok"
    assert calls == [{"file_path": "README.md"}]
    with pytest.raises(PowdrrExecutionError, match="unsupported operation"):
        executor("shell", {"command": ["echo", "unsafe"]})


def test_executor_validates_internal_command_before_dispatch() -> None:
    calls: list[dict[str, object]] = []
    catalog = CommandCatalog(
        (
            CommandSpec(
                "approved-command",
                object_schema({"value": {"type": "string"}}, required=("value",)),
                object_schema({}, additional_properties=True),
            ),
        )
    )

    def dispatch_internal(parameters: Mapping[str, Any]) -> dict[str, bool]:
        calls.append(dict(parameters))
        return {"ok": True}

    executor = WorkrrOperationExecutor(
        {"internal": dispatch_internal},
        command_catalog=catalog,
    )

    with pytest.raises(PowdrrExecutionError, match="parameters do not match"):
        executor("internal", {"command": ["approved-command"], "value": 7})
    assert calls == []

    assert executor("internal", {"command": ["approved-command"], "value": "safe"}) == {
        "ok": True
    }
    assert len(calls) == 1


def test_executor_rejects_malformed_internal_command() -> None:
    executor = WorkrrOperationExecutor({"internal": lambda _parameters: None})

    with pytest.raises(PowdrrExecutionError, match="non-empty list"):
        executor("internal", {"command": []})
