from __future__ import annotations

from pathlib import Path
from typing import Any

from powdrr_lift.agent.provider_config import default_llm_mappings
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


def test_probe_marks_non_llm_step_passing_without_calling_model(tmp_path: Path) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: deterministic
when_to_use: [Run deterministic work.]
steps:
  - id: run-check
    description: Run the deterministic check.
    step_type: gate
    pre_step:
      action: invoke_tool
      template:
        tool: shell
        command: ["true"]
    gate:
      outcome: {path: returncode, equals: 0}
      goto_step: run-check
      retry_context: The check failed.
""",
        encoding="utf-8",
    )

    probe = build_workflow_prompt_probe(definition, repo_root=tmp_path, step_index=0)
    client = _Client({"action": "complete"})

    results = probe_workflow_step(client, probe, model="test-model")

    assert results[0].valid
    assert not probe.requires_llm
    assert results[0].deterministic_result == {"passed": True}
    assert client.messages == []


def test_probe_runs_predicated_deterministic_pre_step_into_prompt(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: predicated
when_to_use: [Run predicated work.]
steps:
  - id: collect
    description: Collect the result.
    step_type: predicated
    pre_step:
      action: invoke_tool
      template:
        tool: shell
        command: ["true"]
    completion:
      required_outputs: [result]
    outputs:
      - name: result
        type: object
        required_for_next_step: true
""",
        encoding="utf-8",
    )

    probe = build_workflow_prompt_probe(definition, repo_root=tmp_path, step_index=0)

    assert probe.requires_llm
    assert probe.deterministic_result is not None
    assert any(
        event.get("kind") == "deterministic_pre_step"
        for event in probe.execution_events
    )
    prompt = "\n".join(message["content"] for message in probe.messages)
    assert "deterministic_pre_step" in prompt
    assert "emit_outputs" in probe.response_schema["properties"]["action"]["enum"]


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
    assert model == default_llm_mappings("deepinfra-cheap")["high_reasoning"].model
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


def test_probe_allows_structured_basedpyright_tool_invocations(
    tmp_path: Path,
) -> None:
    definition = tmp_path / "skill.yaml"
    definition.write_text(
        """\
name: inspect-python
when_to_use: [Inspect Python structure.]
steps:
  - id: inspect
    description: Inspect the Python file.
    actions: [invoke_tool]
    tool_invocations:
      - tool: basedpyright-structure
        operation: inspect_structure
""",
        encoding="utf-8",
    )
    probe = build_workflow_prompt_probe(definition, repo_root=tmp_path, step_index=0)

    result = probe_workflow_step(
        _Client(
            {
                "action": "invoke_tool",
                "tool": "basedpyright-structure",
                "parameters": {
                    "operation": "inspect_structure",
                    "path": "src/example.py",
                },
            }
        ),
        probe,
        model="test-model",
    )[0]

    assert result.valid
    assert result.action is not None
    assert result.action["tool"] == "basedpyright-structure"


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
