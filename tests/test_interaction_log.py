from __future__ import annotations

import json
from pathlib import Path

import pytest

from powdrr_lift.interaction_log import InteractionLog


def test_interaction_log_appends_human_and_llm_records_atomically(
    tmp_path: Path,
) -> None:
    log = InteractionLog(tmp_path / ".powdrr" / "interaction-log.json")
    log.record(actor="human", input_value="Specify a feature", output_value="")
    log.record(
        actor="llm",
        input_value=[{"role": "user", "content": "Specify a feature"}],
        output_value={"action": "next_step"},
        metadata={"provider": "test"},
    )

    document = json.loads(
        (tmp_path / ".powdrr" / "interaction-log.json").read_text(encoding="utf-8")
    )
    assert document["schema_version"] == 1
    assert [item["actor"] for item in document["interactions"]] == ["human", "llm"]
    assert document["interactions"][1]["metadata"] == {"provider": "test"}


def test_interaction_log_rejects_corrupt_existing_document(tmp_path: Path) -> None:
    path = tmp_path / "interaction-log.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Could not read interaction log"):
        InteractionLog(path).record(actor="human", input_value="x", output_value="y")
