from __future__ import annotations

import pytest

from powdrr_lift.workflow_chat_agent import _parse_action_response


def test_action_contract_requires_the_canonical_action_field() -> None:
    with pytest.raises(RuntimeError, match='top-level "action" field'):
        _parse_action_response({"kind": "complete"})


def test_action_contract_preserves_declared_outputs() -> None:
    action = _parse_action_response(
        {
            "action": "complete",
            "output_state": {"result": "done"},
            "outputs": {"summary": "done"},
        }
    )

    assert action.kind == "complete"
    assert action.output_state == {"result": "done"}
    assert action.outputs == {"summary": "done"}


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "next_step"},
        {"action": "complete", "output_state": {"result": "done"}},
        {
            "action": "read_document",
            "file_path": "README.md",
            "start_line": 1,
            "end_line": 10,
        },
    ],
)
def test_action_contract_is_shared_by_workflow_modes(
    payload: dict[str, object],
) -> None:
    assert _parse_action_response(payload).kind == payload["action"]
