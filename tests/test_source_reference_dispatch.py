from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_service.structrr.reference_definitions import ReferenceDefinition

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr import source_reference_dispatch as dispatch
from powdrr_lift.workrr import task_agent
from powdrr_lift.workrr.coding_agent_validation import ValidationProfile

BASELINE = "# structrr: feature:twice\ndef twice(value):\n    return value\n"
CANDIDATE = BASELINE.replace("return value", "return value * 2")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, str, dict[str, Any]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "twice.py").write_text(BASELINE)
    (repo / ".gitignore").write_text(".powdrr/\n")
    document = {
        "schema_version": dispatch.DESIGN_SCHEMA,
        "plan_id": "plan-1",
        "item_id": "item-1",
        "design": {
            "reference_definitions": [
                ReferenceDefinition(
                    "feature:twice", "feature", "Return twice the input.", "spec:1"
                ).to_data()
            ],
            "source_reference_placements": [
                {
                    "obligation_id": "place:twice",
                    "ref": "feature:twice",
                    "relationship": "implements",
                    "requirement": "Return twice the input.",
                    "owner": {
                        "path": "twice.py",
                        "kind": "function",
                        "qualified_name": "twice",
                    },
                    "cardinality": "exactly_one",
                    "verification_refs": ["case:twice"],
                }
            ],
        },
    }
    (repo / "design.yaml").write_text(yaml.safe_dump(document))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "reviewed design")
    proposed = {
        "plan_id": "plan-1",
        "item_id": "item-1",
        "source_reference_design_file": "design.yaml",
        "objective": "Implement twice.",
        "scope": ["Implement twice."],
        "allowed_paths": ["twice.py", "added.py"],
        "implementation_steps": ["Double the input."],
        "acceptance_criteria": ["The result is twice the input."],
        "verification_command": (
            "python -c 'from twice import twice; assert twice(3)==6'"
        ),
    }
    return repo, git(repo, "rev-parse", "HEAD"), proposed


class Reviewer:
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        packet = json.loads(messages[-1]["content"])
        return {
            "classification": "satisfied",
            "explanation": "The function doubles its input.",
            "evidence_locations": packet["available_evidence"],
        }


def test_design_is_loaded_from_exact_committed_regular_blob(tmp_path: Path) -> None:
    repo, base, proposed = fixture(tmp_path)
    (repo / "design.yaml").write_text("unreviewed candidate changes")
    path, design = dispatch.load_reviewed_source_design(
        repo, proposed, base_commit=base
    )
    assert path == "design.yaml"
    assert design["source_reference_placements"][0]["ref"] == "feature:twice"
    with pytest.raises(ValueError, match="reviewed plan item"):
        dispatch.load_reviewed_source_design(
            repo, {**proposed, "item_id": "other"}, base_commit=base
        )
    with pytest.raises(ValueError, match="repository-relative"):
        dispatch.load_reviewed_source_design(
            repo,
            {**proposed, "source_reference_design_file": "../design.yaml"},
            base_commit=base,
        )
    (repo / "link.yaml").symlink_to("design.yaml")
    git(repo, "add", "link.yaml")
    git(repo, "commit", "-m", "link")
    with pytest.raises(ValueError, match="regular committed file"):
        dispatch.load_reviewed_source_design(
            repo,
            {**proposed, "source_reference_design_file": "link.yaml"},
            base_commit=git(repo, "rev-parse", "HEAD"),
        )


def test_task_routes_before_legacy_authorship_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, proposed = fixture(tmp_path)
    calls = []
    monkeypatch.setattr(task_agent, "_workflow_base_commit", lambda *a, **k: base)
    monkeypatch.setattr(
        task_agent, "_preflight_reviewed_file_creation_conflicts", lambda *a, **k: ()
    )
    monkeypatch.setattr(
        task_agent,
        "_issue_worker_structrr_context",
        lambda *a, **k: pytest.fail("legacy dispatch"),
    )
    expected = {"passed": False, "retryable": False, "continuations": 0}

    def execute(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(dispatch, "dispatch_source_reference_task", execute)
    strategy = SimpleNamespace(
        task=SimpleNamespace(
            task_id="implementation",
            input_state={
                "proposed_pr_context": proposed,
                "proposed_pr_id": "workflow-1",
            },
        ),
        workflow=SimpleNamespace(directory=repo / ".powdrr"),
        repo_root=repo,
        config=task_agent.WorkflowTaskAgentConfig(workflow_dir=repo / ".powdrr"),
        client=Reviewer(),
        events=[],
    )
    typed_strategy = cast(task_agent._TaskWorkflowExecutionStrategy, strategy)
    result = task_agent._TaskWorkflowExecutionStrategy._run_coding_agent(typed_strategy)
    assert result is expected
    assert len(calls) == 1
    assert calls[0]["allowed_paths"] == ("twice.py", "added.py")
    assert strategy.events[-1]["result"] is expected
    del proposed["source_reference_design_file"]
    proposed["source_reference_placements"] = []
    with pytest.raises(PowdrrExecutionError, match="reviewed design file"):
        task_agent._TaskWorkflowExecutionStrategy._run_coding_agent(typed_strategy)


def test_story_implementation_dispatch_requires_validated_context(
    tmp_path: Path,
) -> None:
    repo, base, proposed = fixture(tmp_path)
    proposed["story_refs"] = [{"story_id": "story:resume"}]
    arguments: dict[str, Any] = {
        "repo_root": repo,
        "proposed": proposed,
        "base_commit": base,
        "workflow_id": "workflow-1",
        "task_id": "implementation",
        "objective": "Implement the pinned behavior.",
        "allowed_paths": ("twice.py",),
        "profile": ValidationProfile("behavior", (sys.executable, "-c", "pass")),
        "code_agent": "minisweagent",
        "model": "test",
        "worker_timeout_seconds": 30,
        "validation_timeout_seconds": 30,
        "review_client": Reviewer(),
    }

    with pytest.raises(PowdrrExecutionError, match="validated pinned story context"):
        dispatch.dispatch_source_reference_task(**arguments)

    with pytest.raises(
        PowdrrExecutionError, match="requires independently authored tests"
    ):
        full_validation: dict[str, Any] = dict(arguments)
        full_validation["proposed"] = {**proposed, "story_validation": True}
        dispatch.dispatch_source_reference_task(
            **full_validation,
            story_context={"story_id": "story:resume"},
        )


def test_story_context_is_bound_into_protected_source_objective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, proposed = fixture(tmp_path)
    proposed["story_refs"] = [{"story_id": "story:resume"}]
    captured: dict[str, Any] = {}

    class Run:
        private_root = tmp_path / "private"
        public_root = tmp_path / "public"

        def __init__(self) -> None:
            self.public_root.mkdir()

        def execute(self, _handler: Any) -> Any:
            return SimpleNamespace(
                accepted=True,
                to_data=lambda: {
                    "outcome": "accepted",
                    "attempt": {
                        "attempt_id": "run-1",
                        "status": "completed",
                        "changed_paths": [],
                    },
                    "validation": {"status": "passed"},
                    "error": None,
                },
            )

        def verify_candidate(self) -> None:
            return None

        def protect_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
            return command

        def validation_environment(self) -> dict[str, str]:
            return {}

    def prepare(**kwargs: Any) -> Run:
        captured.update(kwargs)
        return Run()

    def fake_git(_root: Path, *arguments: str, data: bytes | None = None) -> bytes:
        if arguments[:2] == ("rev-parse", "HEAD"):
            return base.encode()
        if arguments[0] in {"diff", "ls-files", "status", "apply", "worktree"}:
            return b""
        pytest.fail(f"unexpected git command: {arguments}")

    monkeypatch.setattr(
        dispatch, "load_reviewed_source_design", lambda *_a, **_k: ("design.yaml", {})
    )
    monkeypatch.setattr(dispatch, "_git", fake_git)
    monkeypatch.setattr(dispatch.SourceReferenceRun, "prepare", prepare)
    monkeypatch.setattr(dispatch, "create_miniswe_inference_handler", lambda _t: None)
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    monkeypatch.setattr(
        dispatch,
        "execute_story_validation",
        lambda *_a, **kwargs: {
            "passed": True,
            "story_coverage": {"verified": True},
            "coverage_evidence_error": None,
        },
    )
    story_context = {
        "stories": [{"id": "story:resume", "contract_digest": "sha256:abc"}],
        "cases": [{"id": "story:resume:case:1"}],
    }

    result = dispatch.dispatch_source_reference_task(
        repo_root=repo,
        proposed={**proposed, "story_validation": True},
        base_commit=base,
        workflow_id="workflow-1",
        task_id="implementation",
        objective="Implement the pinned behavior.",
        allowed_paths=("twice.py",),
        profile=ValidationProfile("behavior", (sys.executable, "-c", "pass")),
        code_agent="minisweagent",
        model="test",
        worker_timeout_seconds=30,
        validation_timeout_seconds=30,
        review_client=Reviewer(),
        story_context=story_context,
        story_validation_bundle={
            "snapshot": {"user_stories": []},
            "test_sources": {"tests/test_story.py": "def test_story(): pass\n"},
            "authoring": {
                "story_test_links": {"links": [{"selector": "test_story"}]},
                "accepted_story_review": {},
                "story_review_evidence": {},
                "test_sha256": dispatch.fingerprint_test_files(
                    {"tests/test_story.py": "def test_story(): pass\n"}
                ),
            },
        },
    )

    assert result["passed"] is True, result.get("error")
    assert "Pinned customer story contract and selected cases" in captured["objective"]
    assert "story:resume:case:1" in captured["objective"]
    assert captured["readonly_support_files"] == {
        "tests/test_story.py": "def test_story(): pass\n"
    }
    assert "story-validation" in captured["profiles"]


@pytest.mark.parametrize("scenario", ["success", "behavior_failure", "parent_conflict"])
def test_native_dispatch_validates_before_applying_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    if sys.platform == "linux" and not Path("/usr/bin/bwrap").is_file():
        pytest.skip("Native Linux source dispatch requires bubblewrap")
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("Native source dispatch requires macOS or Linux")
    repo, base, proposed = fixture(tmp_path)
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("MSWEA_COST_TRACKING", "ignore_errors")
    runtime = repo / ".powdrr" / "state.json"
    runtime.parent.mkdir()
    runtime.write_text("workflow state")
    requests = []

    def infer(request: ChatCompletionRequest) -> ChatCompletionResponse:
        requests.append(request)
        candidate = next((tmp_path / "powdrr-source-tasks").glob("*/candidate"))
        if scenario == "parent_conflict":
            (repo / "twice.py").write_text("concurrent parent edit\n")
        completion = {
            "schema_version": "structrr-reference-completion-v1",
            "reference_owners": [
                {
                    "ref": "feature:twice",
                    "path": "twice.py",
                    "kind": "function",
                    "qualified_name": "twice",
                }
            ],
            "design_gaps": [],
        }
        edit = (
            "from pathlib import Path; Path('twice.py').write_text("
            + repr(CANDIDATE)
            + "); Path('added.py').write_text('VALUE = 1\\n')"
        )
        commands = [
            "pwd; git rev-parse --show-toplevel",
            "python -m powdrr_agent.execution.source_reader read twice.py",
            "python -c " + shlex.quote(edit),
            "printf '%s\\n' COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT "
            + shlex.quote(json.dumps(completion)),
        ]
        assert str(candidate) in request.messages[1]["content"]
        call = {
            "id": str(len(requests)),
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": commands[len(requests) - 1]}),
            },
        }
        return ChatCompletionResponse(
            content=None, tool_calls=(call,), finish_reason="tool_calls"
        )

    monkeypatch.setattr(
        dispatch, "create_miniswe_inference_handler", lambda timeout: infer
    )
    parameters: dict[str, Any] = dict(
        repo_root=repo,
        proposed=proposed,
        base_commit=base,
        workflow_id="workflow-1",
        task_id="implementation",
        objective="Double the input.",
        allowed_paths=("twice.py", "added.py"),
        profile=ValidationProfile(
            "behavior",
            (
                sys.executable,
                "-c",
                "from twice import twice; assert twice(3)=="
                + ("7" if scenario == "behavior_failure" else "6"),
            ),
        ),
        code_agent="minisweagent",
        model="test",
        worker_timeout_seconds=30,
        validation_timeout_seconds=30,
        review_client=Reviewer(),
    )
    result = dispatch.dispatch_source_reference_task(**parameters)
    if any(
        message in (result.get("error") or "")
        for message in (
            "sandbox_apply: Operation not permitted",
            "No permissions to create a new namespace",
        )
    ):
        pytest.skip("Outer environment prevents nested native sandbox")
    assert result["passed"] is (scenario == "success"), result
    assert result["continuations"] == 0
    assert not result["retryable"]
    assert len(requests) == 4, result
    assert runtime.read_text() == "workflow state"
    assert git(repo, "rev-parse", "HEAD") == base
    if scenario == "success":
        assert (repo / "twice.py").read_text() == CANDIDATE
        assert (repo / "added.py").read_text() == "VALUE = 1\n"
        assert set(result["changed_paths"]) == {"twice.py", "added.py"}
    elif scenario == "parent_conflict":
        assert (repo / "twice.py").read_text() == "concurrent parent edit\n"
        assert not (repo / "added.py").exists()
    else:
        assert (repo / "twice.py").read_text() == BASELINE
        assert not (repo / "added.py").exists()
    with pytest.raises(PowdrrExecutionError, match="already dispatched"):
        dispatch.dispatch_source_reference_task(**parameters)
