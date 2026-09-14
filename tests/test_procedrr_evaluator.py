# ruff: noqa: I001

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.provider_config import DEEPINFRA_CHEAP_MODEL
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


class HelloWorldLLM:
    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        question = messages[1]["content"]
        if "Which exact repository files" in question:
            return {
                "requests": [{"file_path": "hello.py", "reason": "read the program"}]
            }
        if "Which exact files must be read" in question:
            return {
                "requests": [
                    {"file_path": "hello.py", "reason": "confirm current output"}
                ]
            }
        if "smallest ordered implementation plan" in question:
            return {
                "actions": [
                    {
                        "id": "add-second-line",
                        "file_path": "hello.py",
                        "intent": "Print Here I Am after Hello, World.",
                    }
                ],
                "validation_commands": [
                    [sys.executable, "-m", "pytest", "-q"],
                    ["ruff", "check", "hello.py"],
                ],
                "acceptance_conditions": [
                    "hello.py prints Hello, World and Here I Am on separate lines",
                    "the test asserting both lines passes",
                    "ruff reports no issues",
                ],
            }
        if "What one file edit implements" in question:
            return {
                "file_path": "hello.py",
                "edits": [
                    {
                        "old_text": 'print("Hello, World")\n',
                        "new_text": 'print("Hello, World")\nprint("Here I Am")\n',
                    }
                ],
            }
        if "Which validation failures" in question:
            return {"actions": []}
        if "Does the implemented tree" in question:
            return {"complete": True, "missing": []}
        if "Are all declared invariants" in question:
            return {"preserved": True, "violations": []}
        if "security regression" in question:
            return {"safe": True, "findings": []}
        raise AssertionError(f"unexpected judge question: {question}")


def test_execute_proposed_pr_hello_world_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text('print("Hello, World")\n', encoding="utf-8")
    proposal_dir = tmp_path / "docs" / "proposals" / "hello-world"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "proposed-pr-specification.yaml").write_text(
        "id: hello-world\nfeatures: [{id: hello-world, action: added}]\n",
        encoding="utf-8",
    )
    (proposal_dir / "implementation-specification.yaml").write_text(
        "id: hello-world\nmodules: [{id: hello, action: added}]\n",
        encoding="utf-8",
    )
    (tmp_path / "test_hello.py").write_text(
        "import subprocess\n"
        "import sys\n\n"
        "def test_hello_has_two_lines():\n"
        "    result = subprocess.run(\n"
        "        [sys.executable, 'hello.py'], capture_output=True, text=True,\n"
        "        check=True,\n"
        "    )\n"
        "    assert result.stdout.splitlines() == ['Hello, World', 'Here I Am']\n",
        encoding="utf-8",
    )
    source = Path(
        "docs/procedrr/skill-definitions/execute-proposed-pr.yaml"
    ).read_text()
    from procedrr import parse_and_validate

    document = parse_and_validate(source)

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "internal":
            command = parameters.get("command", [])
            if command[:2] == ["powdrr-lift", "show-proposed-pr"]:
                return {"id": "hello-world", "intent": "Add a second output line."}
            completed = subprocess.run(
                command, cwd=tmp_path, capture_output=True, text=True
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        if tool == "gather_context":
            return {
                "types": parameters["types"],
                "feature_id": parameters.get("feature_id"),
            }
        if tool == "read_document":
            return (tmp_path / parameters["file_path"]).read_text(encoding="utf-8")
        if tool == "invoke_tool":
            return {"tool": parameters["tool"], "returncode": 0}
        if tool == "edit":
            path = tmp_path / parameters["file_path"]
            text = path.read_text(encoding="utf-8")
            for edit in parameters["edits"]:
                text = text.replace(edit["old_text"], edit["new_text"], 1)
            path.write_text(text, encoding="utf-8")
            return {"changed": True, "path": parameters["file_path"]}
        raise AssertionError(f"unexpected operation: {tool}")

    result = Evaluator(HelloWorldLLM(), execute).evaluate(
        document,
        {
            "work_item_name": "hello-world",
            "proposed_pr_id": "hello-world",
            "feature_description": "Print an additional line: Here I Am.",
        },
    )

    assert result.bindings["completeness_review"]["complete"] is True
    assert result.bindings["invariant_review"]["preserved"] is True
    assert result.bindings["security_review"]["safe"] is True
    assert (
        tmp_path / "hello.py"
    ).read_text() == 'print("Hello, World")\nprint("Here I Am")\n'


def test_execute_proposed_pr_hello_world_with_live_llm(tmp_path: Path) -> None:
    """Exercise the same flow with a real provider when explicitly enabled."""
    if os.environ.get("POWDRR_LIVE_LLM") != "1":
        import pytest

        pytest.skip("set POWDRR_LIVE_LLM=1 to run the paid live-provider test")

    (tmp_path / "hello.py").write_text('print("Hello, World")\n', encoding="utf-8")
    (tmp_path / "test_hello.py").write_text(
        "import subprocess\nimport sys\n\n"
        "def test_hello_has_two_lines():\n"
        "    result = subprocess.run(\n"
        "        [sys.executable, 'hello.py'], capture_output=True, text=True,\n"
        "        check=True,\n"
        "    )\n"
        "    assert result.stdout.splitlines() == ['Hello, World', 'Here I Am']\n",
        encoding="utf-8",
    )
    proposal_dir = tmp_path / "docs" / "proposals" / "hello-world-live"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / "proposed-pr-specification.yaml").write_text(
        "id: hello-world-live\nfeatures: [{id: hello-world, action: added}]\n",
        encoding="utf-8",
    )
    (proposal_dir / "implementation-specification.yaml").write_text(
        "id: hello-world-live\nmodules: [{id: hello, action: added}]\n",
        encoding="utf-8",
    )
    from procedrr import parse_and_validate

    from importlib import import_module

    build_probe_client = import_module(
        "powdrr_lift.workflow_prompt_probe"
    ).build_probe_client
    document = parse_and_validate(
        Path("docs/procedrr/skill-definitions/execute-proposed-pr.yaml").read_text()
    )
    llm = build_probe_client(
        provider="deepinfra-cheap",
        model=DEEPINFRA_CHEAP_MODEL,
        api_key=None,
        base_url=None,
        repo_root=tmp_path,
    )

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool == "internal":
            command = parameters.get("command", [])
            if command[:2] == ["powdrr-lift", "show-proposed-pr"]:
                return {"id": "hello-world-live", "intent": "Add a second output line."}
            if command[:2] not in ([sys.executable, "-m"], ["ruff", "check"]):
                return {"returncode": 1, "stderr": "unsupported command", "stdout": ""}
            completed = subprocess.run(
                command, cwd=tmp_path, capture_output=True, text=True
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        if tool == "gather_context":
            return {
                "types": parameters["types"],
                "feature_id": parameters.get("feature_id"),
            }
        if tool == "read_document":
            return (tmp_path / parameters["file_path"]).read_text(encoding="utf-8")
        if tool == "invoke_tool":
            return {"tool": parameters["tool"], "returncode": 0}
        if tool == "edit":
            path = tmp_path / parameters["file_path"]
            text = path.read_text(encoding="utf-8")
            for edit in parameters["edits"]:
                text = text.replace(edit["old_text"], edit["new_text"], 1)
            path.write_text(text, encoding="utf-8")
            return {"changed": True, "path": parameters["file_path"]}
        raise AssertionError(f"unexpected operation: {tool}")

    Evaluator(llm, execute).evaluate(
        document,
        {
            "work_item_name": "hello-world-live",
            "proposed_pr_id": "hello-world-live",
            "feature_description": "Print an additional line: Here I Am.",
        },
    )
    assert (
        tmp_path / "hello.py"
    ).read_text() == 'print("Hello, World")\nprint("Here I Am")\n'
