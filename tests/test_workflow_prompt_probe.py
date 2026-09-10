from __future__ import annotations

from pathlib import Path
from typing import Any

from powdrr_lift.workflow_chat_agent import _default_llm_mappings
from powdrr_lift.workflow_prompt_probe import (
    build_workflow_prompt_probe,
    probe_workflow_step,
    resolve_probe_model,
)


class _Client:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        **_: object,
    ) -> dict[str, Any]:
        self.messages.append(messages)
        return self.response


def test_probe_builds_skill_prompt_and_validates_action(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    details: Report when inspection is complete.
""",
        encoding="utf-8",
    )

    probe = build_workflow_prompt_probe(
        definition,
        repo_root=tmp_path,
        root_intent="Inspect the current feature.",
        step_id="inspect",
    )
    client = _Client({"action": "complete", "text": "Inspected."})

    results = probe_workflow_step(client, probe, model="test-model")

    assert results[0].valid
    assert results[0].action is not None
    assert results[0].action["kind"] == "complete"
    assert client.messages == [probe.messages]
    assert probe.response_schema["properties"]["action"]["enum"]


def test_probe_uses_target_step_llm_type_for_model_selection(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    llm_type: high_reasoning
""",
        encoding="utf-8",
    )

    probe = build_workflow_prompt_probe(definition, repo_root=tmp_path, step_index=0)

    provider, model, llm_type = resolve_probe_model(probe, provider="deepinfra-cheap")

    assert provider == "deepinfra-cheap"
    assert model == _default_llm_mappings("deepinfra-cheap")["high_reasoning"].model
    assert llm_type == "high_reasoning"


def test_probe_reports_invalid_action_without_executing_it(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect
when_to_use: [Inspect files.]
steps:
  - id: inspect
    description: Inspect the feature.
    actions: [read_document]
""",
        encoding="utf-8",
    )
    probe = build_workflow_prompt_probe(definition, repo_root=tmp_path, step_index=0)

    result = probe_workflow_step(
        _Client({"action": "edit", "file_path": "x"}), probe, model="test-model"
    )[0]

    assert not result.valid
    assert result.error_code == "action_error"
    assert "does not match" in (result.error or "")


def test_probe_builds_workflow_template_task_prompt(tmp_path: Path) -> None:
    definition = tmp_path / "workflow.yaml"
    definition.write_text(
        """\
id: inspect-workflow
when_to_use: [Inspect features.]
how_to_fill_this_out: [Provide the feature context.]
task_templates:
  - description: Inspect the feature.
    complexity: low
    input_state: {}
    assignee_type: agent
    assignee_role: coder
    dependent_state: []
    output_state_type: inspection-state
""",
        encoding="utf-8",
    )

    probe = build_workflow_prompt_probe(definition, repo_root=tmp_path, step_index=0)

    assert probe.definition_kind == "workflow_template"
    assert probe.step_id == "probe-task-001"
    assert probe.messages[0]["role"] == "system"
    assert '"execution_mode":"process_workflow_task"' in probe.messages[1]["content"]
