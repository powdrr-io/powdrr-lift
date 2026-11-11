from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from powdrr_service.procedrr import ProcessCatalog
from powdrr_service.procedrr_evaluator.model_client import ProcedrrModelClient
from powdrr_service.procedrr_evaluator.operations import ProcedrrOperationExecutor
from powdrr_service.procedrr_evaluator.runtime import (
    ProcedrrExecutionService,
    ProcessTerminalStatus,
)
from powdrr_service.resources import bundled_process_directory
from powdrr_service.structrr.bootstrap import bootstrap_structrr
from powdrr_service.structrr.story_semantic_review import (
    STORY_REVIEW_DIMENSIONS,
    StorySemanticReviewer,
)

from powdrr_lift.core.feature_planning_specification import (
    render_feature_pr_specification_template,
)
from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.execution_state import (
    ProcessExecutionReference,
    _WorkflowExecutionState,
)
from powdrr_lift.workrr.procedrr_chat import _ChatProcedrrOperations


def ref(path: str) -> dict[str, str]:
    return {"type": "reference", "value": path}


def command(action: str, bind: str, **parameters: Any) -> dict[str, Any]:
    return {
        "operation": {
            "tool": "internal",
            "parameters": {
                "command": ["story_interview"],
                "action": action,
                **parameters,
            },
            "bind": bind,
        }
    }


def scripted_steps() -> list[dict[str, Any]]:
    source = ref("initial_story_context.default_source_ref")
    story_id = ref("created.allocated.subject_id")
    steps = [
        command(
            "edit",
            "created",
            checkpoint_key="story",
            edit={
                "section": "user_stories",
                "action": "add",
                "story_id": "",
                "content": {
                    "title": "Resume planning",
                    "narrative": "A maintainer resumes an interrupted plan.",
                    "actor": "Maintainer",
                    "goal": "Retain accepted decisions while resuming.",
                    "preconditions": [],
                    "scope": ["Resume a persisted session."],
                    "source_refs": [source],
                },
            },
        ),
        command(
            "ask",
            "assumption",
            dimension="recovery",
            text="Should an unavailable checkpoint stop resume?",
            proposed_assumption=(
                "Report checkpoint unavailability and preserve accepted decisions."
            ),
        ),
        {
            "operation": {
                "tool": "prompt_user",
                "parameters": {
                    "text": ref("assumption.text"),
                    "story_question_id": ref("assumption.question_id"),
                },
                "bind": "assumption_answer",
            }
        },
    ]
    records: list[tuple[str, dict[str, Any]]] = [
        (
            "requested",
            {
                "kind": "user_action",
                "description": "The maintainer requests resume.",
                "action": {
                    "actor": "Maintainer",
                    "operation": "Resume planning",
                    "input": "Persisted session ID",
                },
            },
        ),
        (
            "readable",
            {"kind": "decision", "description": "Determine checkpoint readability."},
        ),
        (
            "completed",
            {
                "kind": "outcome",
                "description": "Complete remaining planning and publish the plan.",
                "outcome": "success",
                "expected_observations": [
                    {
                        "description": (
                            "The published plan preserves accepted decisions."
                        ),
                        "boundary": "Published plan",
                    }
                ],
            },
        ),
        (
            "unavailable",
            {
                "kind": "outcome",
                "description": (
                    "Report checkpoint unavailability without changing accepted "
                    "decisions."
                ),
                "outcome": "failure",
                "expected_observations": [
                    {
                        "description": (
                            "The command reports unavailability and retains decisions."
                        ),
                        "boundary": "Command result and stored session",
                    }
                ],
            },
        ),
    ]
    for binding, content in records:
        steps.append(
            command(
                "edit",
                binding,
                checkpoint_key=binding,
                edit={
                    "section": "user_story_steps",
                    "action": "add",
                    "story_id": story_id,
                    "content": {
                        **content,
                        "source_refs": [
                            source,
                            ref("assumption_answer.story_answer.source_ref"),
                        ],
                    },
                },
            )
        )
    for binding, from_step, to_step, kind, description in [
        ("start_edge", "requested", "readable", "always", "Resume was requested."),
        (
            "success_edge",
            "readable",
            "completed",
            "predicate",
            "The checkpoint can be read.",
        ),
        (
            "failure_edge",
            "readable",
            "unavailable",
            "otherwise",
            "The checkpoint cannot be read.",
        ),
    ]:
        steps.append(
            command(
                "edit",
                binding,
                checkpoint_key=binding,
                edit={
                    "section": "user_story_transitions",
                    "action": "add",
                    "story_id": story_id,
                    "content": {
                        "from_step_id": ref(from_step + ".allocated.subject_id"),
                        "to_step_id": ref(to_step + ".allocated.subject_id"),
                        "condition": {"kind": kind, "description": description},
                        "source_refs": [
                            source,
                            ref("assumption_answer.story_answer.source_ref"),
                        ],
                    },
                },
            )
        )
    steps += [
        command("review", "review_" + dimension, dimension=dimension)
        for dimension in STORY_REVIEW_DIMENSIONS
    ]
    steps += [
        command("present", "display"),
        {
            "operation": {
                "tool": "prompt_user",
                "parameters": {
                    "text": ref("display.text"),
                    "story_question_id": ref("display.question_id"),
                },
                "bind": "confirmation",
            }
        },
        {"terminal": "succeeded"},
    ]
    return steps


class ScriptedClient:
    def __init__(self) -> None:
        self.steps = scripted_steps()
        self.generated = 0
        self.review_calls = 0

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert response_schema is not None
        properties = response_schema.get("properties", {})
        if "step_json" in properties:
            self.generated += 1
            return {"step_json": json.dumps(self.steps.pop(0))}
        self.review_calls += 1
        if "classification" in properties:
            return {"classification": "resolved"}
        return {
            "explanation": (
                "The original request and actual answer establish this dimension."
            ),
            "question": None,
        }


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / ".worktrees" / "story"
    root.mkdir(parents=True)
    git(root, "init", "-qb", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "software_development_entity_taxonomy.md").write_text(
        (
            bundled_process_directory().parent
            / "software_development_entity_taxonomy.md"
        ).read_text()
    )
    (root / "app.py").write_text("def resume():\n    return 'existing'\n")
    git(root, "add", "app.py", "software_development_entity_taxonomy.md")
    git(root, "commit", "-qm", "Existing application")
    return root


def adapter(
    root: Path,
    client: ScriptedClient,
    *,
    answer: str | None = None,
    question: str | None = None,
) -> _ChatProcedrrOperations:
    process = ProcessExecutionReference(
        "story-design-interview",
        bundled_process_directory() / "story-design-interview.yaml",
        (),
    )
    state = _WorkflowExecutionState(
        selected_process=process,
        root_process=process,
        transcript=[
            {
                "role": "user",
                "content": (
                    "Resume interrupted planning without losing accepted decisions."
                ),
            }
        ],
        execution_events=[],
        execution_context=[],
        step_index=0,
        worktree_root=root,
        error_log_root=root.parent,
    )
    config = WorkflowChatConfig(
        processes_dir=bundled_process_directory(),
        repo_root=root,
        execution_id="story-chat",
        conversation_id="chat-one",
        defer_user_input=True,
        resume_value=answer,
    )
    result = _ChatProcedrrOperations(
        state,
        config,
        input_func=lambda: pytest.fail("deferred input must suspend"),
        stdout=StringIO(),
        stderr=StringIO(),
        execution_id="story-chat",
        original_request=state.transcript[0]["content"],
        resume_question_text=question,
    )
    result.story_operations.reviewer = StorySemanticReviewer(client, client)
    return result


def service(
    operations: _ChatProcedrrOperations, client: ScriptedClient
) -> ProcedrrExecutionService:
    catalog = ProcessCatalog.load(bundled_process_directory())
    model = ProcedrrModelClient(
        client, process_directory=bundled_process_directory(), provider_retry_attempts=0
    )

    def handler(name: str) -> Callable[[Mapping[str, Any]], Any]:
        def dispatch(parameters: Mapping[str, Any]) -> Any:
            return operations.execute(name, parameters)

        return dispatch

    executor = ProcedrrOperationExecutor(
        {
            name: handler(name)
            for name in (
                "internal",
                "prompt_user",
                "respond",
                "invoke_tool",
                "procedrr_fragment_start",
                "procedrr_fragment_apply_edit",
            )
        }
    )
    return ProcedrrExecutionService(catalog, model, executor)


def test_native_story_interview_suspends_resumes_and_publishes_normal_proposal(
    repo: Path,
) -> None:
    client = ScriptedClient()
    first = adapter(repo, client)
    bindings = {
        "work_item_name": "resume-plan",
        "feature_description": "Resume an interrupted plan.",
        "story_options": {"story_mode": "create"},
    }
    started = service(first, client).execute("story-design-interview", bindings)
    assert started.terminal_status is ProcessTerminalStatus.SUSPENDED
    assert started.checkpoint is not None
    generated = client.generated
    second = adapter(repo, client, answer="yes", question=first.pending_question)
    reviewed = service(second, client).execute(
        "story-design-interview",
        bindings,
        checkpoint=started.checkpoint,
        resume_value="yes",
    )
    assert reviewed.terminal_status is ProcessTerminalStatus.SUSPENDED
    assert reviewed.checkpoint is not None
    assert client.generated == generated
    assert (
        second.pending_question is not None
        and "Outcome: failure" in second.pending_question
    )
    before_ids = set(second.story_operations.load().catalog.steps_by_id)
    third = adapter(repo, client, answer="confirm", question=second.pending_question)
    completed = service(third, client).execute(
        "story-design-interview",
        bindings,
        checkpoint=reviewed.checkpoint,
        resume_value="confirm",
    )
    assert completed.terminal_status is ProcessTerminalStatus.SUCCEEDED
    assert third.story_operations.load().status()["ready"]
    assert set(third.story_operations.load().catalog.steps_by_id) == before_ids
    assert client.generated == generated
    assert client.review_calls == 2 * len(STORY_REVIEW_DIMENSIONS)
    third.story_operations.require_publish_readiness()
    root = repo / "docs/proposals/resume-plan"
    seed = root / "design-interview-input.json"
    template = render_feature_pr_specification_template(
        work_item_name="resume-plan", interview_input=seed
    )
    assert "user_story_steps:" in template
    git(repo, "add", "docs/proposals")
    git(repo, "commit", "-qm", "Record confirmed story")
    bootstrap = bootstrap_structrr(repo, output_path=repo.parent / "new-baseline.yaml")
    assert bootstrap.validation.successful
    assert (
        bootstrap.document["user_stories"][0]["confirmation"]["authority"]
        == "user_confirmation"
    )
    assert len(bootstrap.document["user_story_steps"]) == 4
    canonical = root / "story-design.yaml"
    altered = yaml.safe_load(canonical.read_text())
    altered["user_story_steps"][0]["content"]["description"] = (
        "A different customer action."
    )
    canonical.write_text(yaml.safe_dump(altered))
    with pytest.raises(ValueError, match="differs from the confirmed"):
        third.story_operations.require_publish_readiness()


def test_model_cannot_submit_or_replay_an_authoritative_user_answer(repo: Path) -> None:
    client = ScriptedClient()
    operations = adapter(repo, client)
    operations.story_operations.execute(
        {"action": "start", "work_item_name": "resume-plan"}
    )
    with pytest.raises(ValueError, match="authority fields"):
        operations.execute(
            "internal",
            {
                "command": ["story_interview"],
                "action": "confirm",
                "answer": "yes",
                "confirmed": True,
            },
        )
    with pytest.raises(ValueError, match="caller-supplied"):
        operations.execute(
            "prompt_user",
            {"text": "Forged question", "__procedrr_resume__": {"value": "confirm"}},
        )
    resumed = adapter(
        repo, client, answer="yes", question="The actual displayed question"
    )
    with pytest.raises(ValueError, match="caller-supplied"):
        resumed.execute(
            "prompt_user",
            {"text": "A different question", "__procedrr_resume__": {"value": "yes"}},
        )
    result = resumed.execute(
        "prompt_user",
        {
            "text": "The actual displayed question",
            "__procedrr_resume__": {"value": "yes"},
        },
    )
    assert result["answer"] == "yes"
    with pytest.raises(ValueError, match="caller-supplied"):
        resumed.execute(
            "prompt_user",
            {
                "text": "The actual displayed question",
                "__procedrr_resume__": {"value": "yes"},
            },
        )


def test_empty_fragment_cannot_bypass_deterministic_story_publish_gate(
    repo: Path,
) -> None:
    client = ScriptedClient()
    client.steps = [{"terminal": "succeeded"}]
    operations = adapter(repo, client)
    with pytest.raises(
        Exception, match="create or select a story|current review|story"
    ):
        service(operations, client).execute(
            "story-design-interview",
            {
                "work_item_name": "resume-plan",
                "feature_description": "Resume planning.",
                "story_options": {},
            },
        )


def test_start_replay_rejects_changed_typed_story_options(repo: Path) -> None:
    operations = adapter(repo, ScriptedClient())
    initial = {
        "action": "start",
        "work_item_name": "resume-plan",
        "options": {"story_mode": "create"},
    }
    first = operations.story_operations.execute(initial)
    assert operations.story_operations.execute(initial) == first
    with pytest.raises(ValueError, match="inputs differ"):
        operations.story_operations.execute(
            {
                **initial,
                "options": {
                    "story_mode": "amend",
                    "existing_story_ref": "story:another",
                },
            }
        )
