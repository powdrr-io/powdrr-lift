import json
import os
from pathlib import Path

import pytest

from powdrr.interaction_log.entities import InteractionEntry
from powdrr.interaction_log.writer import LogWriter


def test_log_writer_writes_json(tmp_path):
    writer = LogWriter(tmp_path)
    entry = InteractionEntry(input="hello", output="world")
    writer.append(entry)

    log_path = tmp_path / ".powdrr" / "interaction-log.json"
    assert log_path.exists()
    with open(log_path) as f:
        records = json.load(f)
    assert len(records) == 1
    assert records[0]["input"] == "hello"
    assert records[0]["output"] == "world"


def test_log_writer_appends_entries(tmp_path):
    writer = LogWriter(tmp_path)
    writer.append(InteractionEntry(input="first", output="one"))
    writer.append(InteractionEntry(input="second", output="two"))

    log_path = tmp_path / ".powdrr" / "interaction-log.json"
    with open(log_path) as f:
        records = json.load(f)
    assert [r["input"] for r in records] == ["first", "second"]
    assert [r["output"] for r in records] == ["one", "two"]
