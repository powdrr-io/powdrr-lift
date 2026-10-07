"""Static discovery helpers for native validation task runners."""

from __future__ import annotations

import ast
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


@dataclass(frozen=True, slots=True)
class NoxSessionDeclaration:
    """Literal, source-backed facts about one decorated Nox session."""

    name: str
    python: object = None
    parameters: Mapping[str, object] | None = None
    calls: tuple[Mapping[str, object], ...] = ()
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NoxConfiguration:
    """Static Nox sessions and optional default session selection."""

    config_file: str
    sessions: tuple[NoxSessionDeclaration, ...]
    default_sessions: tuple[str, ...] = ()
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


def discover_nox_configuration(root: str | Path) -> NoxConfiguration | None:
    """Inspect noxfile.py with AST; never import or execute it."""
    root_path = Path(root).resolve()
    path = root_path / "noxfile.py"
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename="noxfile.py")
    except (OSError, SyntaxError, UnicodeError) as error:
        return NoxConfiguration(
            "noxfile.py",
            (),
            unresolved=(f"Could not statically parse noxfile.py: {error}",),
        )
    sessions: list[NoxSessionDeclaration] = []
    default_sessions: tuple[str, ...] = ()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value_node = node.value
            if value_node is not None and any(
                _is_nox_sessions_target(t) for t in targets
            ):
                values = _literal_strings(value_node)
                if values is not None:
                    default_sessions = values
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            session = _session_from_function(node)
            if session is not None:
                sessions.append(session)
    diagnostics: tuple[str, ...] = ()
    if not sessions:
        diagnostics = (
            "noxfile.py exists but no statically recognizable "
            "@nox.session functions were found.",
        )
    return NoxConfiguration(
        "noxfile.py",
        tuple(sorted(sessions, key=lambda session: session.name)),
        default_sessions,
        diagnostics,
    )


def nox_ci_invocations(
    commands: Sequence[tuple[str, ...]],
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Select literal CI command lines invoking Nox and selected sessions."""
    found: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for command in commands:
        if "nox" not in command and not any(
            command[index : index + 3] == ("python", "-m", "nox")
            for index in range(max(0, len(command) - 2))
        ):
            continue
        sessions: list[str] = []
        for index, token in enumerate(command):
            if token in {"-s", "--session"} and index + 1 < len(command):
                sessions.extend(part for part in command[index + 1].split(",") if part)
            elif token.startswith("--session="):
                sessions.extend(
                    part for part in token.split("=", 1)[1].split(",") if part
                )
        found.append((command, tuple(dict.fromkeys(sessions))))
    return tuple(found)


def nox_workflow_invocations(
    root: str | Path,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]:
    """Read inline Nox run steps with their workflow paths."""
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
            for invocation, sessions in nox_ci_invocations((command,)):
                found.append(
                    (invocation, sessions, path.relative_to(root_path).as_posix())
                )
    return tuple(found)


def _session_from_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> NoxSessionDeclaration | None:
    session_decorator: ast.expr | None = None
    parameters: dict[str, object] = {}
    unresolved: list[str] = []
    for decorator in function.decorator_list:
        expression = decorator.func if isinstance(decorator, ast.Call) else decorator
        expression_name = _ast_name(expression)
        if expression_name in {"nox.session", "session"}:
            session_decorator = decorator
        if expression_name in {"nox.parametrize", "parametrize"}:
            if isinstance(decorator, ast.Call) and len(decorator.args) >= 2:
                names = _literal_strings(decorator.args[0])
                values = _literal_value(decorator.args[1])
                if names and values is not _DYNAMIC:
                    parameters[names[0]] = values
                else:
                    unresolved.append(
                        "Dynamic nox.parametrize declaration on session "
                        f"{function.name}."
                    )
    if session_decorator is None:
        return None
    name = function.name
    python: object = None
    if isinstance(session_decorator, ast.Call):
        for keyword in session_decorator.keywords:
            value = _literal_value(keyword.value)
            if keyword.arg == "name" and isinstance(value, str):
                name = value
            elif keyword.arg == "python":
                python = value if value is not _DYNAMIC else ast.unparse(keyword.value)
                if value is _DYNAMIC:
                    unresolved.append(
                        f"Dynamic python selector for Nox session {name}."
                    )
    calls = tuple(_session_calls(function))
    return NoxSessionDeclaration(name, python, parameters, calls, tuple(unresolved))


def _session_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[Mapping[str, object]]:
    calls: list[Mapping[str, object]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "session":
            continue
        method = node.func.attr
        if method not in {"install", "run", "run_install", "notify", "chdir"}:
            continue
        arguments = [_literal_value(argument) for argument in node.args]
        keyword_sources = {
            keyword.arg or "**": keyword.value for keyword in node.keywords
        }
        keyword_arguments = {
            name: _literal_value(value) for name, value in keyword_sources.items()
        }
        dynamic = any(value is _DYNAMIC for value in arguments) or any(
            value is _DYNAMIC for value in keyword_arguments.values()
        )
        calls.append(
            {
                "method": method,
                "arguments": [
                    ast.unparse(argument) if value is _DYNAMIC else value
                    for argument, value in zip(node.args, arguments, strict=True)
                ],
                "keyword_arguments": {
                    key: ast.unparse(keyword_sources[key])
                    if value is _DYNAMIC
                    else value
                    for key, value in keyword_arguments.items()
                },
                "line": node.lineno,
                "dynamic": dynamic,
            }
        )
    return sorted(calls, key=_call_line)


def _call_line(item: Mapping[str, object]) -> int:
    line = item.get("line")
    return line if isinstance(line, int) else 0


class _DynamicValue:
    pass


_DYNAMIC = _DynamicValue()


def _literal_value(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return _DYNAMIC


def _literal_strings(node: ast.AST) -> tuple[str, ...] | None:
    value = _literal_value(node)
    if isinstance(value, str):
        return tuple(item for item in value.replace(",", " ").split() if item)
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        return tuple(value)
    return None


def _is_nox_sessions_target(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "sessions"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "options"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "nox"
    )


def _ast_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


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
