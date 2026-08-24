from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from powdrr_lift.workflow_error_logging import WORKFLOW_OBSERVER_LOG
from powdrr_lift.workflow_llm import ProgressDecision, WorkflowActionObservation
from powdrr_lift.workflow_observer import (
    ObserverActionSummary,
    ObserverDecision,
    ObserverExecutionContext,
    ObserverPacket,
    ObserverProgressState,
    ObserverTrigger,
    ShadowWorkflowObserver,
    build_observer_messages,
    observer_packet_fingerprint,
    parse_observer_decision,
)


class _Client:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.messages: list[list[dict[str, str]]] = []

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        self.messages.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _context() -> ObserverExecutionContext:
    return ObserverExecutionContext(
        execution_mode="execute_selected_skill",
        root_intent="Implement the requested feature.",
        skill_or_workflow="start-implementing-feature",
        current_step_id="validate-specifications",
        current_step_intent="Validate every generated specification.",
        validation_state={"issue_count": 2},
        handoff_state={"feature_id": "feature-1"},
    )


def _packet(tmp_path: Path) -> ObserverPacket:
    (tmp_path / "changed.txt").write_text("one", encoding="utf-8")
    return ObserverPacket(
        execution_mode="execute_selected_skill",
        trigger=ObserverTrigger("repeated_action", "Repeated without progress."),
        root_intent="Implement the requested feature.",
        skill_or_workflow="start-implementing-feature",
        current_step_id="validate-specifications",
        current_step_intent="Validate every generated specification.",
        recent_actions=(ObserverActionSummary(2, "edit", False, "continue"),),
        recent_failures=(),
        changed_files=("changed.txt",),
        validation_state={"issue_count": 2},
        handoff_state={},
        progress_state=ObserverProgressState(2, 1, 2, 0),
    )


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _decision_payload() -> dict[str, Any]:
    return {
        "verdict": "coach",
        "reason": "The action is repeating.",
        "guidance": ["Choose a materially different action."],
        "expected_progress": "The next action changes material state.",
        "target_step_id": None,
    }


def test_observer_prompt_is_compact_and_includes_complete_example(
    tmp_path: Path,
) -> None:
    messages = build_observer_messages(_packet(tmp_path))

    assert len(messages) == 2
    assert "read-only workflow observer" in messages[0]["content"]
    assert '"verdict": "coach"' in messages[0]["content"]
    assert "complete agent prompt" not in messages[1]["content"]
    assert len(messages[1]["content"]) < 3_000


def test_parse_observer_decision_requires_declared_shape() -> None:
    decision = parse_observer_decision(_decision_payload())

    assert decision == ObserverDecision(
        verdict="coach",
        reason="The action is repeating.",
        guidance=("Choose a materially different action.",),
        expected_progress="The next action changes material state.",
    )


def test_material_fingerprint_changes_with_changed_file_contents(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path)
    first = observer_packet_fingerprint(packet, tmp_path)
    (tmp_path / "changed.txt").write_text("two", encoding="utf-8")

    assert observer_packet_fingerprint(packet, tmp_path) != first


def test_repeated_action_triggers_once_for_identical_material_state(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    client = _Client(_decision_payload())
    observer = ShadowWorkflowObserver(
        client=client,
        model="high-model",
        provider="configured-provider",
        worktree_root=tmp_path,
        log_root=tmp_path,
        context_provider=_context,
    )
    progress = WorkflowActionObservation(
        signature='{"kind":"edit"}',
        made_progress=True,
        decision=ProgressDecision.CONTINUE,
    )
    stalled = replace(progress, made_progress=False)

    observer.action_completed({"kind": "edit"}, progress)
    observer.action_completed({"kind": "edit"}, stalled)
    observer.action_completed({"kind": "edit"}, stalled)

    assert len(client.messages) == 1
    records = [
        json.loads(line)
        for line in (tmp_path / WORKFLOW_OBSERVER_LOG)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[-1]["trigger"] == "repeated_action"
    assert records[-1]["context"]["model"] == "high-model"
    assert records[-1]["observer_decision"]["verdict"] == "coach"


def test_repeated_failure_is_deterministic_and_observer_failure_is_nonfatal(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    client = _Client(RuntimeError("observer unavailable"))
    observer = ShadowWorkflowObserver(
        client=client,
        model="high-model",
        provider="configured-provider",
        worktree_root=tmp_path,
        log_root=tmp_path,
        context_provider=_context,
    )

    observer.action_failed({"kind": "edit"}, ValueError("invalid range"))
    observer.action_failed({"kind": "edit"}, ValueError("invalid range"))

    records = [
        json.loads(line)
        for line in (tmp_path / WORKFLOW_OBSERVER_LOG)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(client.messages) == 1
    assert records[-1]["trigger"] == "repeated_failure"
    assert records[-1]["error"] == "observer unavailable"


def test_completion_trigger_is_logged_without_calling_observer(tmp_path: Path) -> None:
    _git_init(tmp_path)
    client = _Client(_decision_payload())
    observer = ShadowWorkflowObserver(
        client=client,
        model="high-model",
        provider="configured-provider",
        worktree_root=tmp_path,
        log_root=tmp_path,
        context_provider=_context,
    )
    observation = WorkflowActionObservation(
        signature='{"kind":"complete"}',
        made_progress=True,
        decision=ProgressDecision.CONTINUE,
    )

    observer.action_completed({"kind": "complete"}, observation)

    assert client.messages == []
    record = json.loads(
        (tmp_path / WORKFLOW_OBSERVER_LOG).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert record["trigger"] == "completion"
    assert record["context"]["llm_invoked"] is False


def test_human_prompt_review_can_bypass_cooldown_after_stall_diagnosis(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    client = _Client(_decision_payload())
    observer = ShadowWorkflowObserver(
        client=client,
        model="high-model",
        provider="configured-provider",
        worktree_root=tmp_path,
        log_root=tmp_path,
        context_provider=_context,
    )

    observer.action_failed({"kind": "edit"}, ValueError("invalid edit"))
    observer.action_failed({"kind": "edit"}, ValueError("invalid edit"))
    decision = observer.action_proposed(
        {"kind": "prompt_user", "text": "Which choice should I make?"}
    )

    assert decision is not None
    assert decision.verdict == "coach"
    assert len(client.messages) == 2
