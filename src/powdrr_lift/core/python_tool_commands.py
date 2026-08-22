"""Infer Python tool invocations from project dependency metadata."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MISSING_EXECUTABLE_PATTERNS = (
    re.compile(r"(?:command|executable).*not found", re.IGNORECASE),
    re.compile(r"failed to spawn", re.IGNORECASE),
    re.compile(r"no such file or directory", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class PythonCommandVariant:
    command: tuple[str, ...]
    reason: str


def missing_executable_output(*, stdout: str, stderr: str) -> bool:
    """Return whether output indicates that the requested tool is unavailable."""
    output = f"{stdout}\n{stderr}"
    return any(pattern.search(output) for pattern in _MISSING_EXECUTABLE_PATTERNS)


def dependency_backed_command_variants(
    command: list[str],
    *,
    project_root: Path,
) -> tuple[PythonCommandVariant, ...]:
    """Return package-runner variants for a missing Python command.

    The command is deliberately not rewritten based on tool names.  Instead,
    the executable is matched against dependency names declared by the project,
    so a repository can use any test, formatter, linter, or type checker.
    """
    if len(command) < 3 or command[:2] != ["uv", "run"]:
        return ()
    if any(option in command[2:] for option in ("--extra", "--group")):
        return ()
    executable = command[2]
    if executable.startswith("-"):
        return ()

    metadata_path = _find_pyproject(project_root)
    if metadata_path is None:
        return ()
    try:
        metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ()

    variants: list[PythonCommandVariant] = []
    project = _mapping(metadata.get("project"))
    optional_dependencies = _mapping(project.get("optional-dependencies"))
    for group_name, dependencies in optional_dependencies.items():
        if _contains_package(dependencies, executable):
            variants.append(
                _uv_variant(
                    command,
                    flag="--extra",
                    group_name=group_name,
                    reason=f"{executable} is declared by project extra {group_name!r}",
                )
            )

    dependency_groups = _mapping(metadata.get("dependency-groups"))
    for group_name, dependencies in dependency_groups.items():
        if _contains_package(dependencies, executable):
            variants.append(
                _uv_variant(
                    command,
                    flag="--group",
                    group_name=group_name,
                    reason=(
                        f"{executable} is declared by dependency group {group_name!r}"
                    ),
                )
            )
    return tuple(variants)


def _find_pyproject(project_root: Path) -> Path | None:
    for directory in (project_root, *project_root.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _contains_package(value: Any, executable: str) -> bool:
    if not isinstance(value, list):
        return False
    normalized_executable = _normalize_package_name(executable)
    return any(
        _normalize_package_name(_dependency_name(item)) == normalized_executable
        for item in value
        if isinstance(item, str)
    )


def _dependency_name(dependency: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", dependency)
    return match.group(1) if match else ""


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _uv_variant(
    command: list[str],
    *,
    flag: str,
    group_name: str,
    reason: str,
) -> PythonCommandVariant:
    return PythonCommandVariant(
        command=tuple(["uv", "run", flag, group_name, *command[2:]]),
        reason=reason,
    )
