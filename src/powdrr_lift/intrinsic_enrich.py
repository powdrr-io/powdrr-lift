"""Intrinsic enrichment of deterministic tool results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_lift.test_failure_packet import build_test_failure_packet
from powdrr_lift.workflow_llm import PowdrrExecutionError

ENRICH_TOOL = "enrich"


def execute_enrich_tool(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a supported tool result into structured data."""
    enrichment = parameters.get("format")
    if enrichment != "pytest":
        raise PowdrrExecutionError(
            "enrich requires format 'pytest' for pytest tool outputs.",
            error_code="invalid_enrich_format",
            action_kind=ENRICH_TOOL,
        )
    tool_output = parameters.get("tool_output")
    if not isinstance(tool_output, Mapping):
        raise PowdrrExecutionError(
            "enrich requires tool_output to be an object.",
            error_code="invalid_enrich_output",
            action_kind=ENRICH_TOOL,
        )
    required = ("command", "returncode", "stdout", "stderr", "cwd")
    missing = [name for name in required if name not in tool_output]
    if missing:
        raise PowdrrExecutionError(
            "enrich tool_output is missing required fields: " + ", ".join(missing)
        )
    command = tool_output["command"]
    if not isinstance(command, (str, Sequence)) or isinstance(
        command, (bytes, bytearray)
    ):
        raise PowdrrExecutionError(
            "enrich tool_output.command must be a string or array.",
            error_code="invalid_enrich_command",
            action_kind=ENRICH_TOOL,
        )
    if isinstance(command, Sequence) and not all(
        isinstance(item, str) for item in command
    ):
        raise PowdrrExecutionError(
            "enrich tool_output.command array must contain strings.",
            error_code="invalid_enrich_command",
            action_kind=ENRICH_TOOL,
        )
    if not isinstance(tool_output["returncode"], int):
        raise PowdrrExecutionError(
            "enrich tool_output.returncode must be an integer.",
            error_code="invalid_enrich_returncode",
            action_kind=ENRICH_TOOL,
        )
    for name in ("stdout", "stderr", "cwd"):
        if not isinstance(tool_output[name], str):
            raise PowdrrExecutionError(
                f"enrich tool_output.{name} must be a string.",
                error_code="invalid_enrich_field",
                action_kind=ENRICH_TOOL,
            )
    packet = build_test_failure_packet(
        command=command,
        returncode=tool_output["returncode"],
        stdout=tool_output["stdout"],
        stderr=tool_output["stderr"],
        cwd=tool_output["cwd"],
    )
    if packet is None:
        raise PowdrrExecutionError(
            "enrich format 'pytest' requires a pytest command.",
            error_code="invalid_enrich_command",
            action_kind=ENRICH_TOOL,
        )
    return {
        "tool": ENRICH_TOOL,
        "format": enrichment,
        "output": packet,
    }
