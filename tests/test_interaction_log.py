import 
import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

# This import will fail initially (RED phase)
from powdrr_lift.interaction_log import InteractionLogger


def test_directory_creation(tmp_path: Path):
    """Verify that the .powdrr directory is created if it does not already exist."""
    log_dir = tmp_path / ".powdrr"
    logger = InteractionLogger(log_dir=log_dir)
    logger.log_interaction(
        role="human",
        direction="input",
        content="test",
        content_type="text"
    )
    assert log_dir.exists()
    assert log_dir.is_dir()


def test_timestamped_filename(tmp_path: Path):
    """Verify that the log file name includes a timestamp and different sessions produce different files."""
    ts1 = datetime(2026, 8, 9, 12, 0, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 8, 9, 12, 0, 1, 0, tzinfo=timezone.utc)

    logger1 = InteractionLogger(log_dir=tmp_path, session_timestamp=ts1)
    path1 = logger1.log_file_path
    assert path1.name.startswith("interactions-")
    assert path1.name.endswith(".l")

    logger2 = InteractionLogger(log_dir=tmp_path, session_timestamp=ts2)
    path2 = logger2.log_file_path

    assert path1 != path2
    assert "20260809" in path1.name


def test_log_text_interaction(tmp_path: Path):
    """Log a text-based human interaction and verify it appears as a valid JSONL entry."""
    logger = InteractionLogger(log_dir=tmp_path)
    logger.log_interaction(
        role="human",
        direction="input",
        content="Hello, world!",
        content_type="text"
    )

    log_file = logger.log_file_path
    assert log_file.exists()

    with open(log_file, "r") as f:
        line = f.readline()
        entry = .loads(line)

    assert entry["role"] == "human"
    assert entry["direction"] == "input"
    assert entry["content_type"] == "text"
    assert entry["encoding"] == "utf-8"
    assert entry["content"] == "Hello, world!"
    assert "timestamp" in entry


def test_log_llm_response(tmp_path: Path):
    """Log an LLM output interaction and verify it appears as a valid JSONL entry."""
    logger = InteractionLogger(log_dir=tmp_path)
    logger.log_interaction(
        role="llm",
        direction="output",
        content="I can help with that.",
        content_type="text"
    )

    log_file = logger.log_file_path
    with open(log_file, "r") as f:
        entry = .loads(f.readline())

    assert entry["role"] == "llm"
    assert entry["direction"] == "output"
    assert entry["content"] == "I can help with that."


def test_l_validity(tmp_path: Path):
    """Log text and LLM interactions, verify each line is valid JSON with required fields."""
    logger = InteractionLogger(log_dir=tmp_path)
    logger.log_interaction(role="human", direction="input", content="Q1", content_type="text")
    logger.log_interaction(role="llm", direction="output", content="A1", content_type="text")

    log_file = logger.log_file_path
    with open(log_file, "r") as f:
        lines = f.readlines()

    assert len(lines) == 2
    for line in lines:
        entry = .loads(line)
        assert "timestamp" in entry
        assert "role" in entry
        assert "direction" in entry
        assert "content_type" in entry
        assert "encoding" in entry
        assert "content" in entry


def test_append_behavior(tmp_path: Path):
    """Log multiple interactions, verify they appear in order and file grows without overwriting."""
    logger = InteractionLogger(log_dir=tmp_path)
    logger.log_interaction(role="human", direction="input", content="First", content_type="text")
    logger.log_interaction(role="llm", direction="output", content="Second", content_type="text")

    log_file = logger.log_file_path
    with open(log_file, "r") as f:
        lines = f.readlines()

    assert len(lines) == 2
    entry1 = .loads(lines[0])
    entry2 = .loads(lines[1])
    assert entry1["content"] == "First"
    assert entry2["content"] == "Second"


def test_binary_content_encoding(tmp_path: Path):
    """Log binary content, verify base64 encoding in JSONL entry and round-trip decode without loss."""
    logger = InteractionLogger(log_dir=tmp_path)
    binary_data = b"\x89PNG\r\n\x1a\n"  # PNG header
    logger.log_interaction(
        role="human",
        direction="input",
        content=binary_data,
        content_type="binary"
    )

    log_file = logger.log_file_path
    with open(log_file, "r") as f:
        entry = .loads(f.readline())

    assert entry["content_type"] == "binary"
    assert entry["encoding"] == "base64"

    decoded_content = base64.b64decode(entry["content"])
    assert decoded_content == binary_data
