"""Exact, code-owned fixes that can run before a source correction model call."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

_RUFF_FORMAT_COMMANDS = (
    ("ruff", "format", "--check"),
    ("python", "-m", "ruff", "format", "--check"),
)
_RUFF_FORMAT_OUTPUT = re.compile(r"Would reformat: (.+)\Z")


@dataclass(frozen=True, slots=True)
class RegisteredCorrection:
    """One deterministic correction selected from typed validation evidence."""

    correction_id: str
    path: str
    check_command: tuple[str, ...]


def select_registered_correction(
    validation: Mapping[str, Any],
    *,
    changed_paths: Sequence[str],
    allowed_paths: Sequence[str],
) -> RegisteredCorrection | None:
    """Select only a Ruff format check with one exact, changed Python file."""
    results = validation.get("results")
    if (
        not isinstance(results, list)
        or not results
        or any(not isinstance(result, Mapping) for result in results)
    ):
        return None
    failures = [result for result in results if result.get("status") == "failed"]
    if len(failures) != 1 or any(
        result.get("status") not in {"passed", "not_applicable", "failed"}
        for result in results
    ):
        return None
    failed = failures[0]
    if failed.get("returncode") != 1 or failed.get("stderr", ""):
        return None
    command_value = failed.get("command")
    if not isinstance(command_value, list) or not all(
        isinstance(part, str) and part for part in command_value
    ):
        return None
    command = tuple(command_value)
    prefix = next(
        (item for item in _RUFF_FORMAT_COMMANDS if command[: len(item)] == item),
        None,
    )
    if prefix is None or len(command) != len(prefix) + 1:
        return None
    path = command[-1]
    if not _safe_python_path(path):
        return None
    if path not in set(changed_paths) or not any(
        _path_is_within(path, scope) for scope in allowed_paths
    ):
        return None
    output = failed.get("stdout")
    if not isinstance(output, str):
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or _RUFF_FORMAT_OUTPUT.fullmatch(lines[0]) is None:
        return None
    if lines[0] != f"Would reformat: {path}":
        return None
    if len(lines) > 2 or (
        len(lines) == 2 and lines[1] != "1 file would be reformatted"
    ):
        return None
    return RegisteredCorrection(
        correction_id="ruff-format-v1",
        path=path,
        check_command=command,
    )


def _safe_python_path(path: str) -> bool:
    if not path or "\\" in path or ":" in path or any(ord(char) < 32 for char in path):
        return False
    candidate = PurePosixPath(path)
    return (
        path.endswith(".py")
        and not candidate.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _path_is_within(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")


__all__ = ["RegisteredCorrection", "select_registered_correction"]
