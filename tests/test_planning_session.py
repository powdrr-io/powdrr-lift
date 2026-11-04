from __future__ import annotations

from types import SimpleNamespace

from powdrr_lift.workrr.planning_session import _planning_terminal_status_message


def test_blocked_planning_status_includes_pending_question_failure() -> None:
    event = SimpleNamespace(
        event_type="needs_answer",
        payload={
            "question": {
                "question_id": "work-units-req-001",
                "kind": "propose_work_units",
            },
            "validation_result": {
                "json_pointer": "/records",
                "message": "must contain at most 3 entries",
            },
        },
    )

    message = _planning_terminal_status_message("blocked", (event,), issue_number=98)

    assert "blocked while waiting for propose_work_units question" in message
    assert "work-units-req-001" in message
    assert "/records: must contain at most 3 entries" in message
    assert "planning issue #98" in message


def test_blocked_planning_status_without_pending_answer_stays_concise() -> None:
    message = _planning_terminal_status_message("blocked", (), issue_number=98)

    assert message == "Procedrr planning process ended blocked"
