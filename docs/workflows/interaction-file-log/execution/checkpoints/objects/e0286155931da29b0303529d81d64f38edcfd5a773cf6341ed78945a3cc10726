from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workflow_ambiguity_review import (
    WorkflowAmbiguityReviewError,
    build_ambiguity_review_messages,
    review_workflow_definition_step,
)


class _FakeClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        return self.response


def _definition(tmp_path: Path) -> Path:
    path = tmp_path / "skill.yaml"
    path.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect-files
    description: Inspect the files.
    details: Read the current files, then report the result.
""",
        encoding="utf-8",
    )
    return path


def test_ambiguity_review_uses_compact_single_step_prompt(tmp_path: Path) -> None:
    path = _definition(tmp_path)
    client = _FakeClient(
        {
            "first_action": {"action": "read_document", "parameters": {}},
            "completion_condition": "The relevant files have been inspected.",
            "allowed_actions": ["read_document", "complete"],
            "missing_information": [],
            "conflicts": [],
            "ambiguous_phrases": [],
            "source_sentences": [],
            "suggested_wording": [],
            "confidence": 0.9,
        }
    )

    review = review_workflow_definition_step(client, path, step_id="inspect-files")

    assert review.step_id == "inspect-files"
    assert review.first_action["action"] == "read_document"
    assert len(client.messages) == 1
    assert "Inspect the files." in client.messages[0][1]["content"]
    assert "read-only" in client.messages[0][0]["content"]


def test_ambiguity_review_rejects_incomplete_reviewer_response(tmp_path: Path) -> None:
    path = _definition(tmp_path)
    client = _FakeClient({"first_action": {"action": "complete"}})

    with pytest.raises(WorkflowAmbiguityReviewError, match="completion_condition"):
        review_workflow_definition_step(client, path, step_index=0)


def test_build_ambiguity_messages_requires_exactly_one_step_selector(
    tmp_path: Path,
) -> None:
    path = _definition(tmp_path)

    with pytest.raises(
        WorkflowAmbiguityReviewError, match="either step_id or step_index"
    ):
        build_ambiguity_review_messages(path, step_id="inspect-files", step_index=0)
