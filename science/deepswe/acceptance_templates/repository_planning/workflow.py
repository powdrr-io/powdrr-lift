"""Load the explicit science Procedrr declaration."""

from pathlib import Path
from typing import Any

from procedrr import parse_and_validate


def definition() -> dict[str, Any]:
    return parse_and_validate(Path(__file__).with_name("pipeline.yaml").read_text())
