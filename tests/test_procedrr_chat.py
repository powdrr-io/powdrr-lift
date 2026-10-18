from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.procedrr_chat import (
    _operation_to_action,
    _select_process,
)
from procedrr import ProcessCatalog


class _SelectingClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        _ = (messages, response_schema)
        return dict(self.response)


def test_process_selector_returns_only_a_catalogued_process() -> None:
    process_dir = Path("docs/procedrr/processes")
    catalog = ProcessCatalog.load(process_dir)

    selected = _select_process(
        catalog,
        _SelectingClient(
            {"process_name": "handle-ad-hoc", "reason": "General follow-up."}
        ),  # type: ignore[arg-type]
        "Please explain this change.",
        {},
    )
    unknown = _select_process(
        catalog,
        _SelectingClient({"process_name": "not-a-process", "reason": "Unknown."}),  # type: ignore[arg-type]
        "Please explain this change.",
        {},
    )

    assert selected == "handle-ad-hoc"
    assert unknown is None


def test_all_supported_workflow_names_have_procedrr_processes() -> None:
    catalog = ProcessCatalog.load(Path("docs/procedrr/processes"))
    names = {process.name for process in catalog.list()}
    assert {
        "address-review-comments",
        "adversarial-pr-review",
        "bootstrap-code-structure",
        "create-pull-request",
        "dead-code-review",
        "design-interview",
        "feature-functionality-review",
        "feature-test-coverage-review",
        "finish-pr-prep",
        "fix-ci-failures",
        "fix-merge-conflicts",
        "handle-ad-hoc",
        "independent-skill-workflow-review",
        "review-architecture",
        "review-skill-workflow",
        "review-system",
        "run-tests-and-fix",
        "security-review",
        "spec-v1-design-review",
        "specify-a-feature",
        "specify-architecture",
        "specify-implementation",
        "specify-system",
        "start-implementing-feature",
    } <= names


def test_chat_operation_adapter_converts_line_edits() -> None:
    action = _operation_to_action(
        {
            "kind": "edit",
            "operation": "replace_lines",
            "file_path": "src/example.py",
            "start_line": 4,
            "end_line": 4,
            "replacement": "return 42",
        }
    )

    assert action.kind == "edit"
    assert action.file_path == "src/example.py"
    assert len(action.edits) == 1
    assert action.edits[0].start_line == 4
    assert action.edits[0].text == "return 42"
