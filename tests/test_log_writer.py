import json
import os
import tempfile
from pathlib import Path

import pytest

from log_writer.writer import append_interaction


def test_writer_writes_json_log(tmp_path):
    """Verify the writer creates .powdrr/interaction-log.json with a valid JSON entry."""
    log_dir = tmp_path / ".powdrr"
    log_path = log_dir / "interaction-log.json"

    append_interaction(log_dir, input_text="Hello", output_text="Hi there")

    assert log_path.exists()
    data = json.loads(log_path.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    entry = data[0]
    assert entry["input"] == "Hello"
    assert entry["output"] == "Hi there"


def test_writer_appends_to_existing_log(tmp_path):
    """Verify the writer appends a new entry to an existing log file."""
    log_dir = tmp_path / ".powdrr"
    log_path = log_dir / "interaction-log.json"
    log_dir.mkdir()
    log_path.write_text(json.dumps([{"input": "first", "output": "one"}]))

    append_interaction(log_dir, input_text="second", output_text="two")

    data = json.loads(log_path.read_text())
    assert len(data) == 2
    assert data[1]["input"] == "second"
    assert data[1]["output"] == "two"
