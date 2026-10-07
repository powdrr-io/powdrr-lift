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

import yaml


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


@dataclass(frozen=True, slots=True)
class HatchEnvironment:
    """One Hatch environment's named scripts and execution settings."""

    name: str
    scripts: Mapping[str, tuple[str, ...]]
    settings: Mapping[str, object]
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HatchConfiguration:
    """Hatch environments from pyproject.toml and/or hatch.toml."""

    config_files: tuple[str, ...]
    environments: Mapping[str, HatchEnvironment]
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PdmConfiguration:
    """PDM user scripts declared in pyproject.toml."""

    config_file: str
    scripts: Mapping[str, Mapping[str, object]]
    shared_options: Mapping[str, object]
    unresolved: tuple[str, ...] = ()


def discover_pdm_configuration(root: str | Path) -> PdmConfiguration | None:
    """Read PDM scripts while preserving their native type and execution settings."""
    path = Path(root).resolve() / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        return PdmConfiguration(
            "pyproject.toml",
            {},
            {},
            (f"Could not parse PDM input pyproject.toml: {error}.",),
        )
    raw = _nested(document, ("tool", "pdm", "scripts"))
    if not isinstance(raw, Mapping):
        return None
    scripts: dict[str, Mapping[str, object]] = {}
    shared = raw.get("_")
    unresolved: list[str] = []
    for name, value in raw.items():
        if name == "_":
            continue
        if not isinstance(name, str):
            unresolved.append(f"PDM script name is not a string: {name!r}.")
            continue
        if isinstance(value, str):
            scripts[name] = {"cmd": value}
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            scripts[name] = {"cmd": value}
        elif isinstance(value, Mapping):
            scripts[name] = dict(value)
        else:
            unresolved.append(
                f"PDM script {name!r} has unsupported declaration syntax."
            )
    return PdmConfiguration(
        "pyproject.toml",
        scripts,
        dict(shared) if isinstance(shared, Mapping) else {},
        tuple(unresolved),
    )


def pdm_script_closure(
    configuration: PdmConfiguration, name: str
) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]:
    """Expand literal PDM tasks; retain unsupported forms as diagnostics."""
    commands: list[tuple[str, ...]] = []
    diagnostics: list[str] = []
    visiting: list[str] = []

    def visit(script_name: str) -> None:
        if script_name in visiting:
            diagnostics.append(
                "PDM composite cycle detected: "
                f"{' -> '.join((*visiting, script_name))}."
            )
            return
        declaration = configuration.scripts.get(script_name)
        if declaration is None:
            diagnostics.append(
                f"PDM composite reference {script_name!r} has no static declaration."
            )
            return
        visiting.append(script_name)
        kind = next(
            (
                key
                for key in ("cmd", "shell", "call", "composite")
                if key in declaration
            ),
            None,
        )
        value = declaration.get(kind) if kind else None
        if kind == "cmd":
            if isinstance(value, str):
                try:
                    commands.append(tuple(shlex.split(value, posix=True)))
                except ValueError:
                    diagnostics.append(
                        f"PDM cmd script {script_name!r} has malformed shell quoting."
                    )
            elif isinstance(value, list) and all(
                isinstance(item, str) for item in value
            ):
                commands.append(tuple(value))
            else:
                diagnostics.append(
                    f"PDM cmd script {script_name!r} is not a literal command."
                )
        elif kind == "composite" and isinstance(value, list):
            for item in value:
                if not isinstance(item, str):
                    diagnostics.append(
                        f"PDM composite script {script_name!r} has a non-string task."
                    )
                    continue
                try:
                    parts = shlex.split(item, posix=True)
                except ValueError:
                    diagnostics.append(
                        f"PDM composite script {script_name!r} has malformed "
                        "shell quoting."
                    )
                    continue
                if parts:
                    if parts[0] in configuration.scripts:
                        visit(parts[0])
                        if len(parts) > 1:
                            diagnostics.append(
                                f"Arguments forwarded through PDM composite "
                                f"{script_name!r} are retained without expansion."
                            )
                    else:
                        commands.append(tuple(parts))
        elif kind in {"shell", "call"}:
            diagnostics.append(
                f"PDM {kind} script {script_name!r} was retained without "
                "interpretation."
            )
        else:
            diagnostics.append(
                f"PDM script {script_name!r} has no supported static command."
            )
        for key in ("env", "env_file", "working_dir", "site_packages", "keep_going"):
            if key in declaration:
                diagnostics.append(
                    f"PDM script {script_name!r} uses {key} settings retained "
                    "in configuration."
                )
        for command in commands:
            if any("{" in part or "}" in part or "${" in part for part in command):
                diagnostics.append(
                    f"PDM script {script_name!r} uses dynamic placeholders."
                )
                break
        visiting.pop()

    visit(name)
    return tuple(commands), tuple(dict.fromkeys(diagnostics))


def discover_hatch_configuration(root: str | Path) -> HatchConfiguration | None:
    """Read Hatch environments and named scripts without expanding or running them."""
    root_path = Path(root).resolve()
    sources: list[tuple[str, Mapping[str, Any]]] = []
    unresolved: list[str] = []
    pyproject = root_path / "pyproject.toml"
    hatch_toml = root_path / "hatch.toml"
    if pyproject.is_file():
        document, error = _read_hatch_toml(pyproject)
        if error:
            unresolved.append(error)
        hatch = _nested(document, ("tool", "hatch"))
        environments = _nested(document, ("tool", "hatch", "envs"))
        if isinstance(hatch, Mapping) and isinstance(environments, Mapping):
            sources.append(("pyproject.toml", environments))
        elif _contains_table(document, ("tool", "hatch")):
            unresolved.append(
                "Could not statically parse Hatch settings in pyproject.toml."
            )
    if hatch_toml.is_file():
        document, error = _read_hatch_toml(hatch_toml)
        if error:
            unresolved.append(error)
        environments = document.get("envs")
        if isinstance(environments, Mapping):
            sources.append(("hatch.toml", environments))
        else:
            unresolved.append(
                "Could not statically parse Hatch environments in hatch.toml."
            )
    if not sources and not unresolved:
        return None

    raw_environments: dict[str, tuple[str, Mapping[str, object]]] = {}
    for source, values in sources:
        for raw_name, raw_settings in values.items():
            if not isinstance(raw_name, str) or not isinstance(raw_settings, Mapping):
                unresolved.append(f"Invalid Hatch environment declaration in {source}.")
                continue
            if raw_name in raw_environments:
                _, previous_settings = raw_environments[raw_name]
                merged_settings = _merge_toml_mappings(previous_settings, raw_settings)
                raw_environments[raw_name] = (source, merged_settings)
            else:
                raw_environments[raw_name] = (source, raw_settings)

    parsed: dict[str, HatchEnvironment] = {}
    for name, (source, raw_settings) in raw_environments.items():
        scripts: dict[str, tuple[str, ...]] = {}
        environment_unresolved: list[str] = []
        raw_scripts = raw_settings.get("scripts", {})
        extra_scripts = raw_settings.get("extra-scripts", {})
        if not isinstance(raw_scripts, Mapping):
            environment_unresolved.append(
                f"Hatch scripts for environment {name!r} in {source} are not a mapping."
            )
            raw_scripts = {}
        if not isinstance(extra_scripts, Mapping):
            environment_unresolved.append(
                f"Hatch extra-scripts for environment {name!r} are not a mapping."
            )
            extra_scripts = {}
        for script_name, declaration in (*raw_scripts.items(), *extra_scripts.items()):
            if not isinstance(script_name, str):
                environment_unresolved.append(
                    f"Hatch script name in environment {name!r} is not a string."
                )
                continue
            commands = _hatch_script_commands(declaration)
            if commands is None:
                environment_unresolved.append(
                    f"Hatch script {name}:{script_name} has unsupported dynamic syntax."
                )
                continue
            if script_name in scripts:
                environment_unresolved.append(
                    f"Hatch extra-script {name}:{script_name} conflicts with scripts."
                )
            else:
                scripts[script_name] = commands
        if raw_settings.get("matrix"):
            environment_unresolved.append(
                f"Hatch matrix variants for environment {name!r} remain symbolic."
            )
        if raw_settings.get("extends"):
            environment_unresolved.append(
                f"Hatch environment inheritance for {name!r} was not expanded."
            )
        settings = {
            key: value
            for key, value in raw_settings.items()
            if key not in {"scripts", "extra-scripts"}
        }
        for script_name, commands in scripts.items():
            for command in commands:
                if "{" in command or "}" in command or "${" in command:
                    environment_unresolved.append(
                        f"Hatch script {name}:{script_name} uses context or shell "
                        "expansion that remains unresolved."
                    )
        parsed[name] = HatchEnvironment(
            name, scripts, settings, tuple(dict.fromkeys(environment_unresolved))
        )
    return HatchConfiguration(
        tuple(source for source, _ in sources),
        parsed,
        tuple(dict.fromkeys(unresolved)),
    )


def _hatch_script_commands(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _read_hatch_toml(path: Path) -> tuple[Mapping[str, Any], str | None]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        return {}, f"Could not parse Hatch input {path.name}: {error}."
    return (value, None) if isinstance(value, Mapping) else ({}, None)


def _merge_toml_mappings(
    base: Mapping[str, object], override: Mapping[str, object]
) -> Mapping[str, object]:
    """Apply Hatch's later hatch.toml precedence recursively."""
    merged: dict[str, object] = dict(base)
    for key, value in override.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_toml_mappings(previous, value)
        else:
            merged[key] = value
    return merged


def _contains_table(value: object, keys: Sequence[str]) -> bool:
    current = value
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return True


def hatch_script_closure(
    configuration: HatchConfiguration,
    environment: str,
    script: str,
    *,
    max_depth: int = 16,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Collect literal Hatch script commands and named-script references."""
    env = configuration.environments.get(environment)
    if env is None:
        return (), (f"Hatch environment {environment!r} is not declared.",)
    commands: list[str] = []
    diagnostics = list(configuration.unresolved)
    diagnostics.extend(env.unresolved)

    def visit(name: str, stack: tuple[str, ...], depth: int) -> None:
        if name in stack:
            diagnostics.append(
                f"Hatch script cycle detected: {' -> '.join((*stack, name))}."
            )
            return
        if depth > max_depth:
            diagnostics.append(f"Hatch script depth exceeded at {name!r}.")
            return
        script_commands = env.scripts.get(name)
        if script_commands is None:
            diagnostics.append(f"Hatch script {environment}:{name} is not declared.")
            return
        for command in script_commands:
            normalized = command.lstrip("-").strip()
            first_word = normalized.split(maxsplit=1)[0] if normalized else ""
            if first_word in env.scripts:
                if normalized != first_word:
                    diagnostics.append(
                        f"Arguments on Hatch script reference {environment}:{name} "
                        "remain unresolved."
                    )
                visit(first_word, (*stack, name), depth + 1)
            else:
                commands.append(command)

    visit(script, (), 0)
    return tuple(commands), tuple(dict.fromkeys(diagnostics))


_MAKE_VALIDATION_TARGETS = {
    "check",
    "ci",
    "format",
    "format-check",
    "lint",
    "pre-commit",
    "test",
    "tests",
    "typecheck",
    "type-check",
    "validate",
    "verify",
}


@dataclass(frozen=True, slots=True)
class MakeTargetDeclaration:
    """A Make target with source-backed prerequisites and recipe text."""

    name: str
    prerequisites: tuple[str, ...]
    recipes: tuple[str, ...]
    source: str
    phony: bool = False
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MakeConfiguration:
    """Literal targets collected from Makefiles and recursively included files."""

    config_files: tuple[str, ...]
    targets: Mapping[str, MakeTargetDeclaration]
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreCommitHook:
    """A hook declaration as written in a pre-commit configuration."""

    id: str
    name: str | None
    entry: str | None
    language: str | None
    args: tuple[str, ...]
    files: str | None
    exclude: str | None
    types: tuple[str, ...]
    stages: tuple[str, ...]
    additional_dependencies: tuple[str, ...]
    pass_filenames: bool | None
    always_run: bool | None
    require_serial: bool | None
    repo: str
    rev: str | None
    source: str


@dataclass(frozen=True, slots=True)
class PreCommitConfiguration:
    """Pre-commit hook configuration and repository defaults."""

    config_file: str
    hooks: tuple[PreCommitHook, ...]
    settings: Mapping[str, object]
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeclarativeTask:
    """One source-backed task with literal prerequisites and command text."""

    name: str
    prerequisites: tuple[str, ...]
    commands: tuple[str, ...]
    source: str
    unresolved: tuple[str, ...] = ()
    execution_settings: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TaskRunnerConfiguration:
    """Tasks collected from one Justfile or Taskfile configuration."""

    config_files: tuple[str, ...]
    tasks: Mapping[str, DeclarativeTask]
    unresolved: tuple[str, ...] = ()


_LIKELY_TASK_NAMES = {
    "check",
    "ci",
    "format",
    "format-check",
    "lint",
    "test",
    "tests",
    "typecheck",
    "type-check",
    "validate",
    "verify",
}


def discover_just_configuration(root: str | Path) -> TaskRunnerConfiguration | None:
    """Read literal Just recipes; keep interpolation and imports unresolved."""
    root_path = Path(root).resolve()
    config_file = next(
        (name for name in ("Justfile", "justfile") if (root_path / name).is_file()),
        None,
    )
    if config_file is None:
        return None
    tasks: dict[str, DeclarativeTask] = {}
    unresolved: list[str] = []
    active: str | None = None
    try:
        lines = (root_path / config_file).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return TaskRunnerConfiguration(
            (config_file,), {}, (f"Could not read {config_file}: {error}",)
        )
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^(import|mod)\s+", stripped):
            unresolved.append(
                f"Just import/module at {config_file}:{line_number} was not followed: "
                f"{stripped}."
            )
            active = None
            continue
        if line[:1].isspace():
            if active and stripped:
                previous_recipe = tasks[active]
                tasks[active] = DeclarativeTask(
                    previous_recipe.name,
                    previous_recipe.prerequisites,
                    (*previous_recipe.commands, stripped),
                    previous_recipe.source,
                    previous_recipe.unresolved,
                )
            continue
        if stripped.startswith("alias "):
            unresolved.append(
                f"Just alias at {config_file}:{line_number} was not resolved: "
                f"{stripped}."
            )
            active = None
            continue
        if stripped.startswith("set ") or ":=" in stripped:
            unresolved.append(
                f"Just setting/variable at {config_file}:{line_number} may affect "
                f"execution and was retained: {stripped}."
            )
            active = None
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)([^:]*)\s*:\s*(.*)$", line)
        if not match:
            active = None
            continue
        name, parameters, dependencies = match.groups()
        dynamic = bool(parameters.strip())
        prerequisites = tuple(
            token
            for token in dependencies.replace("(", " ").replace(")", " ").split()
            if not token.startswith("{{") and not token.endswith("}}")
        )
        task_unresolved = (
            (f"Parameterized Just recipe retained symbolically: {name}.",)
            if dynamic
            else ()
        )
        previous_task = tasks.get(name)
        tasks[name] = DeclarativeTask(
            name,
            (*previous_task.prerequisites, *prerequisites)
            if previous_task
            else prerequisites,
            previous_task.commands if previous_task else (),
            f"{config_file}:{line_number}",
            (*previous_task.unresolved, *task_unresolved)
            if previous_task
            else task_unresolved,
        )
        active = name
    return TaskRunnerConfiguration((config_file,), tasks, tuple(unresolved))


def discover_task_configuration(root: str | Path) -> TaskRunnerConfiguration | None:
    """Read root Taskfile tasks and dependencies without running Task."""
    root_path = Path(root).resolve()
    config_file = next(
        (
            name
            for name in (
                "Taskfile.yml",
                "Taskfile.yaml",
                "taskfile.yml",
                "taskfile.yaml",
            )
            if (root_path / name).is_file()
        ),
        None,
    )
    if config_file is None:
        return None
    unresolved: list[str] = []
    try:
        document = yaml.safe_load((root_path / config_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        return TaskRunnerConfiguration(
            (config_file,), {}, (f"Could not parse {config_file}: {error}",)
        )
    if not isinstance(document, dict):
        return TaskRunnerConfiguration(
            (config_file,), {}, (f"{config_file} must contain a YAML mapping.",)
        )
    raw_tasks = document.get("tasks", {})
    if not isinstance(raw_tasks, dict):
        return TaskRunnerConfiguration(
            (config_file,), {}, (f"{config_file} tasks must be a mapping.",)
        )
    if document.get("includes"):
        unresolved.append(
            f"Taskfile includes in {config_file} were retained but not followed."
        )
    tasks: dict[str, DeclarativeTask] = {}
    for name, declaration in raw_tasks.items():
        if not isinstance(name, str):
            unresolved.append(f"Taskfile task name is not a string: {name!r}.")
            continue
        if isinstance(declaration, str):
            unresolved.append(
                f"Task {name!r} in {config_file} uses shorthand string syntax."
            )
            tasks[name] = DeclarativeTask(name, (), (declaration,), config_file)
            continue
        if not isinstance(declaration, dict):
            unresolved.append(f"Task {name!r} in {config_file} is not a mapping.")
            continue
        raw_deps = declaration.get("deps", [])
        prerequisites: list[str] = []
        if isinstance(raw_deps, list):
            for dependency in raw_deps:
                if isinstance(dependency, str):
                    prerequisites.append(dependency)
                elif isinstance(dependency, dict) and isinstance(
                    dependency.get("task"), str
                ):
                    prerequisites.append(str(dependency["task"]))
                else:
                    unresolved.append(
                        f"Dynamic dependency retained for Task task {name!r}."
                    )
        elif raw_deps:
            unresolved.append(f"Task dependencies for {name!r} are not a list.")
        raw_cmds = declaration.get("cmds", [])
        commands: list[str] = []
        if isinstance(raw_cmds, list):
            for item in raw_cmds:
                if isinstance(item, str):
                    commands.append(item)
                elif isinstance(item, dict) and isinstance(item.get("cmd"), str):
                    commands.append(str(item["cmd"]))
                    if item.get("silent") or item.get("ignore_error"):
                        unresolved.append(
                            f"Task command modifiers for {name!r} affect "
                            "execution semantics."
                        )
                elif isinstance(item, dict) and isinstance(item.get("task"), str):
                    prerequisites.append(str(item["task"]))
                else:
                    unresolved.append(
                        f"Dynamic command retained for Task task {name!r}."
                    )
        elif raw_cmds:
            unresolved.append(f"Task commands for {name!r} are not a list.")
        task_unresolved: list[str] = []
        behavior_settings = {
            key: declaration[key]
            for key in (
                "dir",
                "vars",
                "env",
                "preconditions",
                "sources",
                "generates",
                "status",
                "platforms",
                "method",
                "interactive",
            )
            if key in declaration
        }
        if behavior_settings:
            task_unresolved.append(
                f"Task {name!r} has execution settings that require Task semantics: "
                + ", ".join(sorted(behavior_settings))
                + "."
            )
        tasks[name] = DeclarativeTask(
            name,
            tuple(prerequisites),
            tuple(commands),
            config_file,
            tuple(task_unresolved),
            behavior_settings,
        )
    return TaskRunnerConfiguration(
        (config_file,), tasks, tuple(dict.fromkeys(unresolved))
    )


def likely_task_targets(configuration: TaskRunnerConfiguration) -> tuple[str, ...]:
    """Select likely validation roots while retaining their inferred status."""
    candidates = {
        name
        for name in configuration.tasks
        if name in _LIKELY_TASK_NAMES
        or name.startswith(("check-", "lint-", "test-", "typecheck-", "validate-"))
    }
    nested = {
        dependency
        for name in candidates
        for dependency in configuration.tasks[name].prerequisites
        if dependency in candidates
    }
    return tuple(sorted(candidates - nested)) or tuple(sorted(candidates))


def task_runner_closure(
    configuration: TaskRunnerConfiguration,
    selected: Sequence[str],
    *,
    max_depth: int = 16,
) -> tuple[tuple[DeclarativeTask, ...], tuple[str, ...]]:
    """Collect selected tasks and literal task dependencies in declaration order."""
    collected: list[DeclarativeTask] = []
    diagnostics = list(configuration.unresolved)
    visited: set[str] = set()

    def visit(name: str, stack: tuple[str, ...], depth: int) -> None:
        if name in stack:
            diagnostics.append(
                f"Task dependency cycle detected: {' -> '.join((*stack, name))}."
            )
            return
        if name in visited:
            return
        if depth > max_depth:
            diagnostics.append(f"Task dependency depth exceeded at {name!r}.")
            return
        task = configuration.tasks.get(name)
        if task is None:
            diagnostics.append(f"Task dependency {name!r} has no static declaration.")
            return
        visited.add(name)
        collected.append(task)
        diagnostics.extend(task.unresolved)
        for prerequisite in task.prerequisites:
            if "{{" in prerequisite or "${" in prerequisite:
                diagnostics.append(
                    f"Dynamic dependency retained for task {name!r}: {prerequisite}."
                )
            else:
                visit(prerequisite, (*stack, name), depth + 1)

    for target in selected:
        visit(target, (), 0)
    return tuple(collected), tuple(dict.fromkeys(diagnostics))


def discover_pre_commit_configuration(
    root: str | Path, config_path: str | None = None
) -> PreCommitConfiguration | None:
    """Read pre-commit hook settings without installing or running hooks."""
    root_path = Path(root).resolve()
    if config_path is None:
        config_file = next(
            (
                name
                for name in (".pre-commit-config.yaml", ".pre-commit-config.yml")
                if (root_path / name).is_file()
            ),
            None,
        )
    else:
        candidate = (root_path / config_path).resolve()
        try:
            config_file = candidate.relative_to(root_path).as_posix()
        except ValueError:
            return PreCommitConfiguration(
                config_path,
                (),
                {},
                (f"Pre-commit config path escapes the repository: {config_path}.",),
            )
    if config_file is None:
        return None
    if not (root_path / config_file).is_file():
        return PreCommitConfiguration(
            config_file,
            (),
            {},
            (f"Pre-commit config file was not found: {config_file}.",),
        )
    unresolved: list[str] = []
    try:
        document = yaml.safe_load((root_path / config_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        return PreCommitConfiguration(
            config_file, (), {}, (f"Could not parse {config_file}: {error}",)
        )
    if not isinstance(document, dict):
        return PreCommitConfiguration(
            config_file, (), {}, (f"{config_file} must contain a YAML mapping.",)
        )
    raw_repositories = document.get("repos", [])
    if not isinstance(raw_repositories, list):
        return PreCommitConfiguration(
            config_file, (), {}, (f"{config_file} repos must be a list.",)
        )
    hooks: list[PreCommitHook] = []
    for repository_index, repository in enumerate(raw_repositories):
        if not isinstance(repository, dict):
            unresolved.append(
                f"{config_file} repos[{repository_index}] is not a mapping."
            )
            continue
        repo = repository.get("repo")
        rev = repository.get("rev")
        raw_hooks = repository.get("hooks", [])
        if not isinstance(repo, str) or not isinstance(raw_hooks, list):
            unresolved.append(
                f"{config_file} repos[{repository_index}] needs a literal repo "
                "and hooks list."
            )
            continue
        for hook_index, hook in enumerate(raw_hooks):
            if not isinstance(hook, dict) or not isinstance(hook.get("id"), str):
                unresolved.append(
                    f"{config_file} repos[{repository_index}].hooks[{hook_index}] "
                    "needs a mapping with a literal id."
                )
                continue
            if "entry" not in hook and repo == "local":
                unresolved.append(
                    f"Local hook {hook['id']!r} in {config_file} has no literal entry."
                )
            hooks.append(
                PreCommitHook(
                    id=hook["id"],
                    name=hook.get("name")
                    if isinstance(hook.get("name"), str)
                    else None,
                    entry=hook.get("entry")
                    if isinstance(hook.get("entry"), str)
                    else None,
                    language=(
                        hook.get("language")
                        if isinstance(hook.get("language"), str)
                        else None
                    ),
                    args=_string_tuple(hook.get("args")),
                    files=hook.get("files")
                    if isinstance(hook.get("files"), str)
                    else None,
                    exclude=(
                        hook.get("exclude")
                        if isinstance(hook.get("exclude"), str)
                        else None
                    ),
                    types=_string_tuple(hook.get("types")),
                    stages=_string_tuple(hook.get("stages")),
                    additional_dependencies=_string_tuple(
                        hook.get("additional_dependencies")
                    ),
                    pass_filenames=(
                        hook.get("pass_filenames")
                        if isinstance(hook.get("pass_filenames"), bool)
                        else None
                    ),
                    always_run=(
                        hook.get("always_run")
                        if isinstance(hook.get("always_run"), bool)
                        else None
                    ),
                    require_serial=(
                        hook.get("require_serial")
                        if isinstance(hook.get("require_serial"), bool)
                        else None
                    ),
                    repo=repo,
                    rev=rev if isinstance(rev, str) else None,
                    source=config_file,
                )
            )
    defaults = {
        key: document[key]
        for key in (
            "default_stages",
            "default_install_hook_types",
            "default_language_version",
            "fail_fast",
            "minimum_pre_commit_version",
        )
        if key in document
    }
    return PreCommitConfiguration(
        config_file, tuple(hooks), defaults, tuple(dict.fromkeys(unresolved))
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


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


def discover_make_configuration(root: str | Path) -> MakeConfiguration | None:
    """Parse Make rules and literal includes without expanding or running them."""
    root_path = Path(root).resolve()
    entry = next(
        (
            path
            for name in ("GNUmakefile", "Makefile", "makefile")
            if (path := root_path / name).is_file()
        ),
        None,
    )
    if entry is None:
        return None
    targets: dict[str, MakeTargetDeclaration] = {}
    config_files: list[str] = []
    unresolved: list[str] = []
    _read_makefile(root_path, entry, targets, config_files, unresolved, (), 0)
    return MakeConfiguration(
        tuple(dict.fromkeys(config_files)),
        targets,
        tuple(dict.fromkeys(unresolved)),
    )


def likely_make_validation_targets(
    configuration: MakeConfiguration,
) -> tuple[str, ...]:
    """Return likely validation entrypoints, leaving their inferred status intact."""
    candidates = {
        name
        for name in configuration.targets
        if name in _MAKE_VALIDATION_TARGETS
        or name.startswith(("check-", "lint-", "test-", "typecheck-", "validate-"))
    }
    nested = {
        prerequisite
        for name in candidates
        for prerequisite in configuration.targets[name].prerequisites
        if prerequisite in candidates
    }
    roots = tuple(sorted(candidates - nested))
    return roots or tuple(sorted(candidates))


def make_ci_invocations(
    commands: Sequence[tuple[str, ...]],
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Select literal Make command lines and explicit target arguments."""
    found: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for command in commands:
        executable_index = next(
            (
                index
                for index, token in enumerate(command)
                if Path(token).name in {"make", "gmake"}
            ),
            None,
        )
        if executable_index is None:
            continue
        targets: list[str] = []
        skip_next = False
        for token in command[executable_index + 1 :]:
            if skip_next:
                skip_next = False
                continue
            if token in {"-C", "--directory", "-f", "--file", "-j", "--jobs"}:
                skip_next = True
                continue
            if token.startswith("--target="):
                targets.extend(
                    part for part in token.split("=", 1)[1].split(",") if part
                )
                continue
            if token.startswith(("-", "--")) or "=" in token or token.startswith("${{"):
                continue
            targets.append(token)
        found.append((command, tuple(targets)))
    return tuple(found)


def make_target_closure(
    configuration: MakeConfiguration,
    selected: Sequence[str],
    *,
    max_depth: int = 16,
) -> tuple[tuple[MakeTargetDeclaration, ...], tuple[str, ...]]:
    """Return selected targets and literal prerequisite declarations in order."""
    collected: list[MakeTargetDeclaration] = []
    diagnostics: list[str] = []
    visited: set[str] = set()

    def visit(name: str, stack: tuple[str, ...], depth: int) -> None:
        if name in stack:
            diagnostics.append(
                f"Make prerequisite cycle detected: {' -> '.join((*stack, name))}."
            )
            return
        if name in visited:
            return
        if depth > max_depth:
            diagnostics.append(f"Make prerequisite depth exceeded at target {name!r}.")
            return
        declaration = configuration.targets.get(name)
        if declaration is None:
            diagnostics.append(
                f"Make prerequisite {name!r} has no static rule in discovered files."
            )
            return
        visited.add(name)
        collected.append(declaration)
        diagnostics.extend(declaration.unresolved)
        for prerequisite in declaration.prerequisites:
            if "$" in prerequisite or "%" in prerequisite:
                diagnostics.append(
                    f"Dynamic Make prerequisite retained for {name!r}: {prerequisite}."
                )
            else:
                visit(prerequisite, (*stack, name), depth + 1)

    for target in selected:
        visit(target, (), 0)
    diagnostics.extend(configuration.unresolved)
    return tuple(collected), tuple(dict.fromkeys(diagnostics))


def make_workflow_invocations(
    root: str | Path,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]:
    """Read inline Make run steps with their workflow paths."""
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
            for invocation, targets in make_ci_invocations((command,)):
                found.append(
                    (invocation, targets, path.relative_to(root_path).as_posix())
                )
    return tuple(found)


def _read_makefile(
    root: Path,
    path: Path,
    targets: dict[str, MakeTargetDeclaration],
    config_files: list[str],
    unresolved: list[str],
    stack: tuple[Path, ...],
    depth: int,
) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        unresolved.append(f"Make include escapes the repository: {path}.")
        return
    if depth > 16:
        unresolved.append(f"Make include depth exceeded at {path.relative_to(root)}.")
        return
    if resolved in stack:
        unresolved.append(f"Make include cycle detected at {path.relative_to(root)}.")
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        unresolved.append(f"Could not read Makefile {path.relative_to(root)}: {error}")
        return
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        unresolved.append(f"Make include escapes the repository: {path}.")
        return
    config_files.append(relative)
    lines = _make_logical_lines(content)
    phony: set[str] = set()
    include_paths: list[Path] = []
    active_targets: tuple[str, ...] = ()
    for line in lines:
        include_match = re.match(r"^\s*(-?include|sinclude)\s+(.+?)\s*$", line)
        if include_match:
            declaration = include_match.group(2)
            if "$" in declaration or "`" in declaration:
                unresolved.append(f"Dynamic Make include in {relative}: {declaration}.")
            else:
                for filename in shlex.split(declaration):
                    include_paths.extend(
                        _make_include_paths(root, path.parent, filename)
                    )
            continue
        phony_match = re.match(r"^\.PHONY\s*:\s*(.*)$", line)
        if phony_match:
            phony.update(phony_match.group(1).split())
            continue
        if line.startswith("\t") or not line.strip() or line.lstrip().startswith("#"):
            if line.startswith("\t") and active_targets:
                for target in active_targets:
                    previous = targets.get(target)
                    if previous:
                        targets[target] = MakeTargetDeclaration(
                            previous.name,
                            previous.prerequisites,
                            (*previous.recipes, line[1:]),
                            previous.source,
                            previous.phony,
                            previous.unresolved,
                        )
            continue
        rule = _parse_make_rule(line)
        if rule is None:
            active_targets = ()
            continue
        target_names, prerequisites, inline_recipe = rule
        active_targets = target_names
        for name in target_names:
            dynamic = "$" in name or "%" in name
            target_unresolved = (
                (f"Dynamic Make target or pattern retained: {name}.",)
                if dynamic
                else ()
            )
            previous = targets.get(name)
            targets[name] = MakeTargetDeclaration(
                name=name,
                prerequisites=(previous.prerequisites if previous else ())
                + prerequisites,
                recipes=(previous.recipes if previous else ())
                + ((inline_recipe,) if inline_recipe else ()),
                source=relative,
                phony=name in phony or bool(previous and previous.phony),
                unresolved=(previous.unresolved if previous else ())
                + target_unresolved,
            )
    for target_name, declaration in tuple(targets.items()):
        if declaration.source == relative:
            targets[target_name] = MakeTargetDeclaration(
                declaration.name,
                declaration.prerequisites,
                declaration.recipes,
                declaration.source,
                target_name in phony,
                declaration.unresolved,
            )
    for include_path in include_paths:
        _read_makefile(
            root,
            include_path,
            targets,
            config_files,
            unresolved,
            (*stack, resolved),
            depth + 1,
        )


def _make_logical_lines(content: str) -> tuple[str, ...]:
    result: list[str] = []
    pending = ""
    for line in content.splitlines():
        if pending:
            pending += line.lstrip()
        else:
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        result.append(pending)
        pending = ""
    if pending:
        result.append(pending)
    return tuple(result)


def _parse_make_rule(
    line: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None] | None:
    match = re.match(r"^([^#\t][^:]*?)\s*::?\s*([^;#]*)(?:;\s*(.*))?$", line)
    if not match or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:?+!]?=", line):
        return None
    target_names = tuple(name for name in match.group(1).split() if name)
    if not target_names:
        return None
    prerequisites = tuple(match.group(2).split())
    inline_recipe = match.group(3).strip() if match.group(3) else None
    return target_names, prerequisites, inline_recipe


def _make_include_paths(root: Path, parent: Path, declaration: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for base in (parent, root):
        candidate = base / declaration
        if any(character in declaration for character in "*?["):
            paths.extend(sorted(candidate.parent.glob(candidate.name)))
        elif candidate.is_file():
            paths.append(candidate)
    return tuple(dict.fromkeys(path.resolve() for path in paths))


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
