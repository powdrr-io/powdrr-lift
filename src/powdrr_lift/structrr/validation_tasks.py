"""Static discovery helpers for native validation task runners."""

from __future__ import annotations

import configparser
import re
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ToxConfiguration:
    """Tox declarations needed to preserve native environment semantics."""

    config_files: tuple[str, ...]
    environments: tuple[str, ...]
    commands: Mapping[str, tuple[tuple[str, ...], ...]]
    unresolved: tuple[str, ...] = ()


def discover_tox_configuration(root: str | Path) -> ToxConfiguration | None:
    """Read tox 3/4 configuration without importing or executing project code."""
    root_path = Path(root).resolve()
    present = tuple(
        name
        for name in ("tox.ini", "tox.toml", "pyproject.toml")
        if (root_path / name).is_file()
    )
    tox_configs = tuple(name for name in present if name != "pyproject.toml")
    pyproject_has_tox = False
    if "pyproject.toml" in present:
        pyproject = _read_toml(root_path / "pyproject.toml")
        pyproject_has_tox = isinstance(_nested(pyproject, ("tool", "tox")), Mapping)
    relevant_configs = (
        *tox_configs,
        *(("pyproject.toml",) if pyproject_has_tox else ()),
    )
    selected = tox_configs[:1] or (("pyproject.toml",) if pyproject_has_tox else ())
    if not selected:
        return None
    config_file = selected[0]
    if config_file == "tox.ini":
        environments, commands = _read_ini(root_path / config_file)
    else:
        document = _read_toml(root_path / config_file)
        tox_document: object = (
            _nested(document, ("tool", "tox"))
            if config_file == "pyproject.toml"
            else document
        )
        environments, commands = _read_toml_tox(tox_document)
    ambiguous = len(relevant_configs) > 1
    unresolved: list[str] = []
    if ambiguous:
        unresolved.append(
            "Multiple tox configuration files are present: "
            f"{', '.join(relevant_configs)}."
        )
    if any("{" in env or "}" in env for env in environments):
        unresolved.append(
            "Tox factor expressions are retained symbolically; concrete environment "
            "variants were not expanded."
        )
    return ToxConfiguration(relevant_configs, environments, commands, tuple(unresolved))


def tox_ci_invocations(
    commands: Sequence[tuple[str, ...]],
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Select literal CI command lines invoking tox and their env selectors."""
    invocations: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for command in commands:
        if "tox" not in command and not any(
            command[index : index + 3] == ("python", "-m", "tox")
            for index in range(max(0, len(command) - 2))
        ):
            continue
        envs: list[str] = []
        for index, value in enumerate(command):
            if value in {"-e", "--env", "--envlist"} and index + 1 < len(command):
                envs.extend(part for part in command[index + 1].split(",") if part)
            elif value.startswith(("--env=", "--envlist=")):
                envs.extend(part for part in value.split("=", 1)[1].split(",") if part)
        invocations.append((command, tuple(dict.fromkeys(envs))))
    return tuple(invocations)


def tox_workflow_invocations(
    root: str | Path,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]:
    """Read inline tox run steps with their workflow source paths."""
    root_path = Path(root).resolve()
    found: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []
    for path in sorted((root_path / ".github" / "workflows").glob("*.y*ml")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            match = re.search(r"\brun:\s*(.+)$", line)
            if not match:
                continue
            try:
                command = tuple(shlex.split(match.group(1).strip(), posix=True))
            except ValueError:
                continue
            for invocation, environments in tox_ci_invocations((command,)):
                found.append(
                    (invocation, environments, path.relative_to(root_path).as_posix())
                )
    return tuple(found)


def _read_ini(
    path: Path,
) -> tuple[tuple[str, ...], dict[str, tuple[tuple[str, ...], ...]]]:
    parser = configparser.RawConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return (), {}
    tox = parser["tox"] if parser.has_section("tox") else {}
    environments = _split_envs(tox.get("envlist", tox.get("env_list", "")))
    commands: dict[str, tuple[tuple[str, ...], ...]] = {}
    for section in parser.sections():
        if section == "testenv":
            environment = "*"
        elif section.startswith("testenv:"):
            environment = section.partition(":")[2]
        else:
            continue
        if parser.has_option(section, "commands"):
            commands[environment] = _split_commands(
                parser.get(section, "commands", raw=True)
            )
    return environments, commands


def _read_toml_tox(
    value: object,
) -> tuple[tuple[str, ...], dict[str, tuple[tuple[str, ...], ...]]]:
    if not isinstance(value, Mapping):
        return (), {}
    env_value = value.get("env_list", value.get("envlist", []))
    if isinstance(env_value, str):
        environments = _split_envs(env_value)
    elif isinstance(env_value, Sequence):
        environments = tuple(str(item) for item in env_value if str(item).strip())
    else:
        environments = ()
    commands: dict[str, tuple[tuple[str, ...], ...]] = {}
    env_configs = value.get("env")
    if isinstance(env_configs, Mapping):
        for name, config in env_configs.items():
            if isinstance(config, Mapping) and "commands" in config:
                commands[str(name)] = _as_commands(config.get("commands"))
    base = value.get("env_run_base")
    if isinstance(base, Mapping) and "commands" in base:
        commands["*"] = _as_commands(base.get("commands"))
    return environments, commands


def _read_toml(path: Path) -> Mapping[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _nested(value: object, keys: Sequence[str]) -> object:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _split_envs(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    parts: list[str] = []
    current: list[str] = []
    brace_depth = 0
    for character in value.strip():
        if character == "{":
            brace_depth += 1
        elif character == "}" and brace_depth:
            brace_depth -= 1
        if character in {",", " ", "\t", "\n"} and brace_depth == 0:
            if current:
                parts.append("".join(current))
                current.clear()
            continue
        current.append(character)
    if current:
        parts.append("".join(current))
    return tuple(parts)


def _split_commands(value: str) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            commands.append(tuple(shlex.split(stripped, posix=True)))
        except ValueError:
            commands.append((stripped,))
    return tuple(commands)


def _as_commands(value: object) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, str):
        return _split_commands(value)
    if not isinstance(value, Sequence):
        return ()
    commands: list[tuple[str, ...]] = []
    for command in value:
        if isinstance(command, str):
            try:
                commands.append(tuple(shlex.split(command, posix=True)))
            except ValueError:
                commands.append((command,))
        elif isinstance(command, Sequence):
            commands.append(tuple(str(item) for item in command))
    return tuple(commands)


__all__ = [
    "ToxConfiguration",
    "discover_tox_configuration",
    "tox_ci_invocations",
    "tox_workflow_invocations",
]
