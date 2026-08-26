import json
import os
from pathlib import Path

import pytest

from powdrr_lift.interaction_log.entities import InteractionEntry
from powdrr_lift.interaction_log.log_writer import LogWriter


def test_log_writer_creates_directory(tmp_path):
    log_dir = tmp_path / ".powdrr"
    writer = LogWriter(log_dir)
    writer.append(InteractionEntry(input="hello", output="world"))
    assert log_dir.is_dir()


def test_log_writer_json_format(tmp_path):
    log_dir = tmp_path / ".powdrr"
    writer = LogWriter(log_dir)
    writer.append(InteractionEntry(input="hello", output="world"))
    log_file = log_dir / "interaction-log.json"
    assert log_file.is_file()
    data = json.loads(log_file.read_text())
    assert data == [{"input": "hello", "output": "world"}]


def test_log_writer_appends_entry(tmp_path):
    log_dir = tmp_path / ".powdrr"
    writer = LogWriter(log_dir)
    writer.append(InteractionEntry(input="first", output="one"))
    writer.append(InteractionEntry(input="second", output="two"))
    log_file = log_dir / "interaction-log.json"
    data = json.loads(log_file.read_text())
    assert len(data) == 2
    assert data[0] == {"input": "first", "output": "one"}
    assert data[1] == {"input": "second", "output": "two"}
