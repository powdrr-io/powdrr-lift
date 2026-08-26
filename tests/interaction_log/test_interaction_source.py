from pathlib import Path

from powdrr_lift.interaction_log.interaction_source import InteractionSource
from powdrr_lift.interaction_log.log_writer import LogWriter


def test_interaction_source_feeds_writer(tmp_path: Path) -> None:
    log_dir = tmp_path / ".powdrr"
    writer = LogWriter(log_dir)
    source = InteractionSource(writer)
    source.record(input="hello", output="world")
    assert log_dir.joinpath("interaction-log.json").is_file()
