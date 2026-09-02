"""Deterministic normalization of pytest command results."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_FAILED_NODE_RE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+(.*))?$", re.MULTILINE)
_TRACEBACK_LOCATION_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_]+):(?P<line>\d+)"
)
_EXCEPTION_RE = re.compile(
    r"^\s*E\s+(?P<exception>[A-Za-z_][\w.]*(?:Error|Exception|Exit)):"
    r"\s*(?P<message>.*)$",
    re.MULTILINE,
)


def build_test_failure_packet(
    *,
    command: str | Sequence[str],
    returncode: int,
    stdout: str,
    stderr: str,
    cwd: str,
) -> dict[str, Any] | None:
    """Return a stable pytest result packet, or None for other commands."""
    if not is_pytest_command(command):
        return None

    output = f"{stdout}\n{stderr}"
    if returncode == 0:
        return {"status": "passed", "failures": []}

    failures = _failure_records(output, Path(cwd))
    if not failures:
        failures = [
            {
                "node_id": None,
                "exception": None,
                "message": "pytest exited with a non-zero return code.",
                "traceback_file": None,
                "traceback_line": None,
                "source_files": [],
            }
        ]
    return {"status": "failed", "failures": failures}


def is_pytest_command(command: str | Sequence[str]) -> bool:
    """Return whether a command invokes pytest."""
    return "pytest" in _command_items(command)


def _command_items(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    return list(command)


def _failure_records(output: str, cwd: Path) -> list[dict[str, Any]]:
    locations = _locations(output, cwd)
    exceptions = list(_EXCEPTION_RE.finditer(output))
    records: list[dict[str, Any]] = []
    for match in _FAILED_NODE_RE.finditer(output):
        node_id = match.group(1)
        summary = match.group(2) or ""
        location = _location_for_node(node_id, locations)
        exception = exceptions[len(records)] if len(records) < len(exceptions) else None
        records.append(
            {
                "node_id": node_id,
                "exception": exception.group("exception") if exception else None,
                "message": (
                    exception.group("message")
                    if exception
                    else summary or "pytest test failed."
                ),
                "traceback_file": location[0] if location else None,
                "traceback_line": location[1] if location else None,
                "source_files": _source_files_for_node(node_id, locations),
            }
        )
    return records


def _locations(output: str, cwd: Path) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for match in _TRACEBACK_LOCATION_RE.finditer(output):
        path = _relative_path(match.group("path"), cwd)
        location = (path, int(match.group("line")))
        if location not in result:
            result.append(location)
    return result


def _relative_path(path_text: str, cwd: Path) -> str:
    path = Path(path_text)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(cwd.resolve()))
        except ValueError:
            return str(path)
    return path_text


def _location_for_node(
    node_id: str,
    locations: list[tuple[str, int]],
) -> tuple[str, int] | None:
    node_path = node_id.split("::", 1)[0]
    return next(
        (location for location in locations if location[0] == node_path),
        locations[0] if locations else None,
    )


def _source_files_for_node(
    node_id: str,
    locations: list[tuple[str, int]],
) -> list[str]:
    node_path = node_id.split("::", 1)[0]
    files = [node_path]
    for path, _ in locations:
        if path != node_path and path not in files:
            files.append(path)
    return files
