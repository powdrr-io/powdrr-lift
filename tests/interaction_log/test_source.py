import json

from powdrr.interaction_log.entities import InteractionEntry
from powdrr.interaction_log.source import InteractionSource
from powdrr.interaction_log.writer import LogWriter


def test_interaction_source_feeds_writer(tmp_path):
    writer = LogWriter(tmp_path)
    source = InteractionSource(writer)
    source.emit(InteractionEntry(input="prompt", output="response"))

    log_path = tmp_path / ".powdrr" / "interaction-log.json"
    with open(log_path) as f:
        records = json.load(f)
    assert len(records) == 1
    assert records[0]["input"] == "prompt"
    assert records[0]["output"] == "response"
