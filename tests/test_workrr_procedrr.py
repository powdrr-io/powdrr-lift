from pathlib import Path
from typing import Any

import pytest

from powdrr_lift.workrr.procedrr import (
    ProcedrrResponseError,
    WorkrrProcedrrClient,
)


class RepairingClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        del messages
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


SCHEMA = {
    "type": "object",
    "required": ["complete"],
    "properties": {"complete": {"type": "boolean"}},
}


def test_procedrr_uses_workrr_schema_repair_transport(tmp_path: Path) -> None:
    provider = RepairingClient([{}, {"complete": True}])
    client = WorkrrProcedrrClient(provider, skills_dir=tmp_path)

    result = client.complete_json(
        [{"role": "user", "content": "judge this"}],
        response_schema=SCHEMA,
    )

    assert result == {"complete": True}
    assert provider.calls == 2


def test_procedrr_requires_a_schema() -> None:
    provider = RepairingClient([{"complete": True}])
    client = WorkrrProcedrrClient(provider, skills_dir=Path("."))

    with pytest.raises(ProcedrrResponseError, match="output schema"):
        client.complete_json([{"role": "user", "content": "judge this"}])


def test_evaluator_can_be_constructed_with_workrr_transport(tmp_path: Path) -> None:
    from procedrr_evaluator import Evaluator

    evaluator = Evaluator.with_workrr(
        RepairingClient([{"complete": True}]),
        lambda _tool, _parameters: None,
        skills_dir=tmp_path,
    )

    assert evaluator.llm.__class__.__name__ == "WorkrrProcedrrClient"
