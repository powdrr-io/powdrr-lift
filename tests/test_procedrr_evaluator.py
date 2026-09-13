from collections.abc import Mapping
from pathlib import Path
from typing import Any

from procedrr_evaluator import Evaluator


class FakeLLM:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        self.messages = messages
        return {
            "added": [{"id": "req-1", "description": "Do the thing"}],
            "deleted": [],
        }


def test_evaluator_resolves_tool_output_into_declared_judge_context() -> None:
    llm = FakeLLM()
    calls: list[tuple[str, dict[str, Any]]] = []

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        calls.append((tool, dict(parameters)))
        return [{"id": "existing", "description": "Old requirement"}]

    result = Evaluator(llm, execute).evaluate(
        {
            "name": "demo",
            "inputs": [{"name": "feature_description"}],
            "limits": {"llm_activations": 2, "tool_calls": 2},
            "steps": [
                {
                    "operation": {
                        "tool": "gather_context",
                        "parameters": {"types": ["requirements"]},
                        "bind": "requirements_context",
                    }
                },
                {
                    "judge": {
                        "prompt_system": "Return JSON only.",
                        "instructions": ["Preserve existing items."],
                        "question": "What requirement edits are needed?",
                        "context": ["feature_description", "requirements_context"],
                        "output": {
                            "name": "requirements_edits",
                            "schema": {
                                "type": "object",
                                "required": ["added", "deleted"],
                            },
                        },
                    }
                },
            ],
        },
        {"feature_description": "Add a thing"},
    )

    assert calls == [("gather_context", {"types": ["requirements"]})]
    assert result.bindings["requirements_edits"]["added"][0]["id"] == "req-1"
    assert "requirements_context" in llm.messages[1]["content"]


def test_evaluator_resolves_embedded_references_in_operation_parameters() -> None:
    calls: list[dict[str, Any]] = []

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        calls.append(dict(parameters))
        return None

    Evaluator(FakeLLM(), execute).evaluate(
        {
            "name": "interpolation",
            "steps": [
                {
                    "operation": {
                        "tool": "internal",
                        "parameters": {
                            "command": [
                                "powdrr-lift",
                                "evaluate",
                                "docs/proposals/${work_item_name}/design.yaml",
                            ]
                        },
                    }
                }
            ],
        },
        {"work_item_name": "demo-feature"},
    )

    assert calls == [
        {
            "command": [
                "powdrr-lift",
                "evaluate",
                "docs/proposals/demo-feature/design.yaml",
            ]
        }
    ]


def test_evaluator_runs_checked_in_design_interview_definition() -> None:
    llm = FakeLLM()

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "gather_context":
            return [{"id": "existing", "description": "Existing evidence"}]
        if tool == "internal" and parameters.get("command", [None])[1] == "evaluate":
            return {"returncode": 0}
        return {"ok": True}

    source = Path("docs/procedrr/skill-definitions/design-interview.yaml").read_text()
    from procedrr import parse_and_validate

    document = parse_and_validate(source)
    result = Evaluator(llm, execute).evaluate(
        document,
        {
            "work_item_name": "demo",
            "feature_description": "Add a thing",
            "proposal_issues": [],
        },
    )
    assert result.bindings["final_proposal_evaluation"]["returncode"] == 0
    assert result.llm_activations == 20
