"""Intrinsic enrichment of deterministic tool results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.test_failure_packet import build_test_failure_packet

ENRICH_TOOL = "enrich"


def execute_enrich_tool(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a supported tool result into structured data."""
    enrichment = parameters.get("format")
    if enrichment != "pytest":
        raise RuntimeError("enrich requires format 'pytest' for pytest tool outputs.")
    tool_output = parameters.get("tool_output")
    if not isinstance(tool_output, Mapping):
        raise RuntimeError("enrich requires tool_output to be an object.")
    required = ("command", "returncode", "stdout", "stderr", "cwd")
    missing = [name for name in required if name not in tool_output]
    if missing:
        raise RuntimeError(
            "enrich tool_output is missing required fields: " + ", ".join(missing)
        )
    command = tool_output["command"]
    if not isinstance(command, (str, Sequence)) or isinstance(
        command, (bytes, bytearray)
    ):
        raise RuntimeError("enrich tool_output.command must be a string or array.")
    if isinstance(command, Sequence) and not all(
        isinstance(item, str) for item in command
    ):
        raise RuntimeError("enrich tool_output.command array must contain strings.")
    if not isinstance(tool_output["returncode"], int):
        raise RuntimeError("enrich tool_output.returncode must be an integer.")
    for name in ("stdout", "stderr", "cwd"):
        if not isinstance(tool_output[name], str):
            raise RuntimeError(f"enrich tool_output.{name} must be a string.")
    packet = build_test_failure_packet(
        command=command,
        returncode=tool_output["returncode"],
        stdout=tool_output["stdout"],
        stderr=tool_output["stderr"],
        cwd=tool_output["cwd"],
    )
    if packet is None:
        raise RuntimeError("enrich format 'pytest' requires a pytest command.")
    return {
        "tool": ENRICH_TOOL,
        "format": enrichment,
        "output": packet,
    }
