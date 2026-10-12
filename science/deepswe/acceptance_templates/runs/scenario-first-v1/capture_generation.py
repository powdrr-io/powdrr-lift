"""Replay one malformed scenario batch with raw pre-parser streaming capture."""

import json
import os
import re

from run import DEFAULT_MODEL, HERE, _client, checked

from science.deepswe.acceptance_templates.common import Recorder, load_json, write_json


def main() -> None:
    original = load_json(
        HERE
        / "calls/discovery/gql-incremental-graphql-delivery/batch-02/scenarios-0.json"
    )
    directory = HERE / "calls/parser-diagnostic"
    directory.mkdir(parents=True, exist_ok=True)
    capture = directory / "raw.txt"
    previous = os.environ.get("POWDRR_STREAM_CAPTURE_PATH")
    os.environ["POWDRR_STREAM_CAPTURE_PATH"] = str(capture)
    request = original["request"]
    rec = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 3500),
        directory,
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=0,
    )
    try:
        result = rec.call(
            "capture",
            request["messages"][0]["content"],
            json.loads(request["messages"][1]["content"]),
            request["schema"],
            lambda r: checked(r, request["schema"]),
        )
    finally:
        if previous is None:
            os.environ.pop("POWDRR_STREAM_CAPTURE_PATH", None)
        else:
            os.environ["POWDRR_STREAM_CAPTURE_PATH"] = previous
    raw = re.sub(
        r"\n?---STREAM-(?:BEGIN|END) id=\d+---\n?", "", capture.read_text()
    ).strip()
    try:
        parsed = json.loads(raw)
        conclusion = {"raw_json_valid": True, "parsed_matches_raw": parsed == result}
    except json.JSONDecodeError as exc:
        conclusion = {
            "raw_json_valid": False,
            "json_error": str(exc),
            "post_repair_schema_valid": True,
        }
    conclusion["same_request_as_original"] = (
        original["request_sha256"]
        == load_json(directory / "capture-0.json")["request_sha256"]
    )
    conclusion["first_given_after_parser"] = result["scenarios"][0]["given"]
    write_json(HERE / "parser-diagnostic.json", conclusion)
    print(json.dumps(conclusion, indent=2))


if __name__ == "__main__":
    main()
