"""Discover repository validation commands before declaring a feature complete."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from powdrr_lift.structrr.python_topology import PythonTopology
from powdrr_lift.structrr.validation_models import (
    VALIDATION_INVENTORY_SCHEMA_VERSION,
    ValidationCheck,
    ValidationContext,
)
from powdrr_lift.structrr.validation_tasks import (
    MakeConfiguration,
    NoxConfiguration,
    ToxConfiguration,
    discover_make_configuration,
    discover_nox_configuration,
    discover_tox_configuration,
    likely_make_validation_targets,
    make_ci_invocations,
    make_target_closure,
    make_workflow_invocations,
    nox_ci_invocations,
    nox_workflow_invocations,
    tox_ci_invocations,
    tox_workflow_invocations,
)

VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION = VALIDATION_INVENTORY_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DiscoveredValidationProfile:
    """A validation command inferred from repository configuration."""

    name: str
    command: tuple[str, ...]
    source: str
    provider_name: str | None = None
    component: str | None = None
    environment_id: str | None = None
    purpose: str = ""
    roles: tuple[str, ...] = ()
    cwd: str = "."
    shell: str | None = None
    script: str | None = None
    execution_kind: str | None = None
    selectors: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    settings: dict[str, object] | None = None
    applicability: dict[str, object] | None = None
    ci_origins: tuple[dict[str, object], ...] = ()
    requiredness: dict[str, object] | None = None
    declaration: str = "inferred"
    evidence: tuple[str, ...] = ()
    confirmation: dict[str, object] | None = None
    depends_on: tuple[str, ...] = ()
    local_reproducibility: str = "unknown"
    baseline: dict[str, object] | None = None
    unresolved: tuple[str, ...] = ()

    @property
    def provider(self) -> str:
        """Return the explicitly assigned provider or a legacy fallback."""
        return self.provider_name or self.name.split("-", 1)[0]


def validation_inventory(
    profiles: Sequence[DiscoveredValidationProfile],
) -> tuple[dict[str, object], ...]:
    """Build the provider-neutral bootstrap inventory before Workrr collection."""
    return tuple(_profile_record(profile).to_data() for profile in profiles)


def validation_context(
    profiles: Sequence[DiscoveredValidationProfile],
    topology: PythonTopology | None = None,
) -> dict[str, object]:
    """Describe discovery coverage without claiming the search is exhaustive."""
    diagnostics = list(topology.diagnostics if topology else ())
    if not profiles:
        diagnostics.append(
            {
                "code": "discovery_scope_limited",
                "message": (
                    "Validation discovery is limited to the detectors currently "
                    "implemented; absence of a check is not proof that none exists."
                ),
            }
        )
    return ValidationContext(
        discovery_status="partial",
        coverage=(
            "currently implemented validation detectors",
            "Python component and environment manifests",
        ),
        components=topology.components if topology else (),
        environments=topology.environments if topology else (),
        evidence=(
            tuple(
                {"id": f"validation-input:{path}", "path": path}
                for path in topology.evidence_files
            )
            if topology
            else ()
        ),
        diagnostics=tuple(diagnostics),
        input_fingerprints=topology.input_fingerprints if topology else (),
    ).to_data()


def assign_python_topology(
    profiles: Sequence[DiscoveredValidationProfile], topology: PythonTopology
) -> tuple[DiscoveredValidationProfile, ...]:
    """Attach the repository Python component/environment to Python checks."""
    root_component = next(
        (item for item in topology.components if item.get("path") == "."), None
    )
    if root_component is None:
        return tuple(profiles)
    component_id = str(root_component["id"])
    environment = next(
        (
            item
            for item in topology.environments
            if item.get("component") == component_id
        ),
        None,
    )
    environment_id = str(environment["id"]) if environment else None
    python_providers = {
        "ruff",
        "mypy",
        "pytest",
        "pyright",
        "basedpyright",
        "flake8",
        "black",
    }
    return tuple(
        replace(
            profile,
            component=profile.component or component_id,
            environment_id=profile.environment_id or environment_id,
        )
        if profile.provider in python_providers
        else profile
        for profile in profiles
    )


def _profile_record(profile: DiscoveredValidationProfile) -> ValidationCheck:
    kind = profile.execution_kind or (
        "shell" if profile.shell or profile.script else "argv"
    )
    command = profile.command if kind == "argv" else ()
    return ValidationCheck(
        id=f"validation:{profile.name}",
        provider=profile.provider,
        profile=profile.name,
        command=command,
        source=profile.source,
        environment=profile.environment_id,
        execution={
            "kind": kind,
            "cwd": profile.cwd,
            "shell": profile.shell,
            "script": profile.script,
        },
        component=profile.component,
        purpose=profile.purpose,
        roles=profile.roles,
        selectors=profile.selectors,
        config_files=profile.config_files,
        settings=profile.settings or {},
        applicability=profile.applicability or {"local": True, "evaluation": "unknown"},
        ci_origins=profile.ci_origins,
        requiredness=profile.requiredness or {"status": "unknown", "evidence": []},
        provenance={
            "declaration": profile.declaration,
            "evidence": list(profile.evidence),
        },
        confirmation=profile.confirmation or {"level": "static", "observations": []},
        depends_on=profile.depends_on,
        local_reproducibility=profile.local_reproducibility,
        baseline=profile.baseline or {"status": "not_run", "observation": None},
        unresolved=profile.unresolved,
    )


def discover_validation_profiles(
    root: str | Path,
    *,
    explicit_command: tuple[str, ...] = (),
) -> tuple[DiscoveredValidationProfile, ...]:
    """Discover the checks a repository declares for local/CI validation.

    The explicit feature test remains useful, but it is not sufficient to prove
    that a change is ready for CI.  Project configuration and CI commands are
    inspected for the common Python checks used by this repository: Ruff format,
    Ruff lint, mypy, and pytest.
    """
    root_path = Path(root).resolve()
    profiles: list[DiscoveredValidationProfile] = []
    if explicit_command:
        profiles.append(
            DiscoveredValidationProfile(
                "feature-validation", explicit_command, "feature command"
            )
        )

    pyproject = _load_pyproject(root_path)
    ci_commands = _ci_commands(root_path)
    uv_prefix = ("uv", "run") if (root_path / "pyproject.toml").exists() else ()
    text = _repository_text(root_path)
    profiles.extend(_discover_tox_profiles(root_path, ci_commands))
    profiles.extend(_discover_nox_profiles(root_path, ci_commands))
    profiles.extend(_discover_make_profiles(root_path, ci_commands))
    tox_configuration = discover_tox_configuration(root_path)
    nox_configuration = discover_nox_configuration(root_path)
    make_configuration = discover_make_configuration(root_path)
    task_child_tools = {
        token
        for command_set in _task_runner_commands(
            tox_configuration, nox_configuration, make_configuration, profiles
        )
        for command in command_set
        for token in command
        if token in {"pytest", "ruff", "mypy", "black", "flake8", "pyright"}
    }
    has_task_aggregate = any(
        profile.provider == "aggregate"
        and (
            profile.name.startswith("tox-")
            or profile.name.startswith("nox-")
            or profile.name.startswith("make-")
        )
        for profile in profiles
    )

    if _mentions_tool("ruff", pyproject, text) and not (
        has_task_aggregate
        and "ruff" in task_child_tools
        and _find_command(ci_commands, ("ruff",)) is None
    ):
        profiles.extend(
            (
                _profile(
                    "ruff-format-check",
                    _find_command(ci_commands, ("ruff", "format", "--check"))
                    or (*uv_prefix, "ruff", "format", "--check", "."),
                    "ruff configuration/CI",
                ),
                _profile(
                    "ruff-check",
                    _find_command(ci_commands, ("ruff", "check"))
                    or (*uv_prefix, "ruff", "check", "."),
                    "ruff configuration/CI",
                ),
            )
        )
    if _mentions_tool("mypy", pyproject, text) and not (
        has_task_aggregate
        and "mypy" in task_child_tools
        and _find_command(ci_commands, ("mypy",)) is None
    ):
        profiles.append(
            _profile(
                "mypy",
                _find_command(ci_commands, ("mypy",))
                or (*uv_prefix, "mypy", "src", "tests"),
                "mypy configuration/CI",
            )
        )
    if (
        _mentions_tool("pytest", pyproject, text) or (root_path / "tests").is_dir()
    ) and not (
        has_task_aggregate
        and "pytest" in task_child_tools
        and _find_command(ci_commands, ("pytest",)) is None
    ):
        profiles.append(
            _profile(
                "pytest",
                _find_command(ci_commands, ("pytest",)) or (*uv_prefix, "pytest", "-q"),
                "pytest configuration/CI",
            )
        )

    package_json = _load_package_json(root_path)
    if package_json and _has_script(package_json, ("test", "check", "lint")):
        profiles.append(
            _profile(
                "npm-test",
                _find_command(ci_commands, ("npm", "test")) or ("npm", "test"),
                "package.json scripts/CI",
            )
        )
    if (root_path / "go.mod").exists():
        profiles.append(
            _profile(
                "go-test",
                _find_command(ci_commands, ("go", "test")) or ("go", "test", "./..."),
                "go.mod/CI",
            )
        )
    if (root_path / "Cargo.toml").exists():
        profiles.append(
            _profile(
                "cargo-test",
                _find_command(ci_commands, ("cargo", "test")) or ("cargo", "test"),
                "Cargo.toml/CI",
            )
        )
    if (root_path / "pom.xml").exists() or (root_path / "mvnw").exists():
        executable = "./mvnw" if (root_path / "mvnw").exists() else "mvn"
        profiles.append(_profile("maven-test", (executable, "test"), "Maven project"))
    if (root_path / "gradlew").exists() or (root_path / "build.gradle").exists():
        executable = "./gradlew" if (root_path / "gradlew").exists() else "gradle"
        profiles.append(_profile("gradle-test", (executable, "test"), "Gradle project"))

    unique: dict[str, DiscoveredValidationProfile] = {}
    for profile in profiles:
        unique.setdefault(profile.name, profile)
    return tuple(unique.values())


def _profile(
    name: str, command: tuple[str, ...], source: str
) -> DiscoveredValidationProfile:
    return DiscoveredValidationProfile(name, command, source)


def _discover_tox_profiles(
    root: Path, ci_commands: Sequence[tuple[str, ...]]
) -> tuple[DiscoveredValidationProfile, ...]:
    configuration = discover_tox_configuration(root)
    invocations = tox_ci_invocations(ci_commands)
    workflow_invocations = tox_workflow_invocations(root)
    if not invocations and configuration is None:
        return ()

    selections: tuple[tuple[tuple[str, ...], tuple[str, ...], str | None], ...]
    if invocations:
        if workflow_invocations:
            selections = tuple(
                (command, environments, path)
                for command, environments, path in workflow_invocations
            )
        else:
            selections = tuple(
                (command, environments, None) for command, environments in invocations
            )
        declaration = "declared"
    elif configuration and configuration.environments:
        env_arg = ",".join(configuration.environments)
        selections = ((("tox", "-e", env_arg), configuration.environments, None),)
        declaration = "inferred"
    elif configuration and configuration.commands:
        selections = ((("tox",), (), None),)
        declaration = "inferred"
    else:
        return ()

    config_files = configuration.config_files if configuration else ()
    commands_by_env = configuration.commands if configuration else {}
    unresolved = configuration.unresolved if configuration else ()
    profiles: list[DiscoveredValidationProfile] = []
    for index, (command, environments, workflow_path) in enumerate(selections, start=1):
        suffix = ",".join(environments) or "default"
        name = f"tox-{suffix}" if len(selections) == 1 else f"tox-{index}-{suffix}"
        configured_commands = {
            environment: [list(item) for item in commands]
            for environment, commands in commands_by_env.items()
            if environment == "*" or environment in environments
        }
        profiles.append(
            DiscoveredValidationProfile(
                name=name,
                command=command,
                source="tox configuration/CI",
                provider_name="aggregate",
                purpose="Run the project-declared tox validation environment(s).",
                roles=("validation",),
                selectors=environments,
                config_files=config_files,
                settings={
                    "environments": list(environments),
                    "configured_commands": configured_commands,
                },
                declaration=declaration,
                evidence=tuple(
                    dict.fromkeys(
                        (*config_files, *((workflow_path,) if workflow_path else ()))
                    )
                ),
                unresolved=unresolved,
            )
        )
    return tuple(profiles)


def _discover_nox_profiles(
    root: Path, ci_commands: Sequence[tuple[str, ...]]
) -> tuple[DiscoveredValidationProfile, ...]:
    configuration = discover_nox_configuration(root)
    invocations = nox_ci_invocations(ci_commands)
    workflow_invocations = nox_workflow_invocations(root)
    if configuration is None and not invocations:
        return ()

    session_by_name = (
        {session.name: session for session in configuration.sessions}
        if configuration
        else {}
    )
    selections: tuple[tuple[tuple[str, ...], tuple[str, ...], str | None], ...]
    if invocations:
        if workflow_invocations:
            selections = tuple(
                (command, sessions, path)
                for command, sessions, path in workflow_invocations
            )
        else:
            selections = tuple(
                (command, sessions, None) for command, sessions in invocations
            )
        declaration = "declared"
    elif configuration and configuration.sessions:
        selected_sessions = configuration.default_sessions or tuple(session_by_name)
        selections = tuple(
            (("nox", "-s", name), (name,), None) for name in selected_sessions
        )
        declaration = "inferred"
    elif configuration and configuration.unresolved:
        selections = (((), (), None),)
        declaration = "declared"
    else:
        return ()

    profiles: list[DiscoveredValidationProfile] = []
    for command, session_names, workflow_path in selections:
        if session_names:
            effective_session_names = session_names
        elif configuration:
            effective_session_names = configuration.default_sessions or tuple(
                session_by_name
            )
        else:
            effective_session_names = ()
        selected_definitions = [
            session_by_name[name]
            for name in effective_session_names
            if name in session_by_name
        ]
        unresolved = list(configuration.unresolved if configuration else ())
        unresolved.extend(
            f"Nox session {name!r} has no statically recognized definition."
            for name in effective_session_names
            if name not in session_by_name
        )
        session_settings: dict[str, dict[str, object]] = {}
        for session in selected_definitions:
            session_settings[session.name] = {
                "python": session.python,
                "parameters": dict(session.parameters or {}),
                "calls": [dict(call) for call in session.calls],
            }
            unresolved.extend(session.unresolved)
            if any(call.get("dynamic") for call in session.calls):
                unresolved.append(
                    f"Session {session.name} contains dynamic install/run arguments."
                )
        if command and not session_names and not session_by_name:
            unresolved.append(
                "Nox was invoked by CI, but no statically recognized session "
                "definitions were available."
            )
        session_label = ",".join(effective_session_names) or "all"
        profiles.append(
            DiscoveredValidationProfile(
                name=f"nox-{session_label}",
                command=command,
                source="noxfile.py/GitHub Actions",
                provider_name="aggregate" if command else "custom",
                purpose="Run Nox project validation session(s).",
                roles=("validation",),
                selectors=effective_session_names,
                config_files=(configuration.config_file,) if configuration else (),
                settings={
                    "sessions": session_settings,
                    "default_sessions": list(configuration.default_sessions)
                    if configuration
                    else [],
                },
                execution_kind="argv" if command else "unresolved",
                declaration=declaration,
                evidence=tuple(
                    dict.fromkeys(
                        (
                            *((configuration.config_file,) if configuration else ()),
                            *((workflow_path,) if workflow_path else ()),
                        )
                    )
                ),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _task_runner_commands(
    tox_configuration: object,
    nox_configuration: object,
    make_configuration: object,
    profiles: Sequence[DiscoveredValidationProfile],
) -> tuple[tuple[tuple[str, ...], ...], ...]:
    commands: list[tuple[tuple[str, ...], ...]] = []
    tox_config = (
        cast(ToxConfiguration, tox_configuration)
        if tox_configuration is not None
        else None
    )
    nox_config = (
        cast(NoxConfiguration, nox_configuration)
        if nox_configuration is not None
        else None
    )
    make_config = (
        cast(MakeConfiguration, make_configuration)
        if make_configuration is not None
        else None
    )
    for profile in profiles:
        if profile.name.startswith("tox-") and tox_config:
            for environment, command_set in tox_config.commands.items():
                if (
                    environment == "*"
                    or not profile.selectors
                    or environment in profile.selectors
                ):
                    commands.append(command_set)
        elif profile.name.startswith("nox-") and nox_config:
            selected = (
                profile.selectors
                or nox_config.default_sessions
                or tuple(session.name for session in nox_config.sessions)
            )
            for session in nox_config.sessions:
                if session.name not in selected:
                    continue
                for call in session.calls:
                    if call.get("method") != "run":
                        continue
                    args = call.get("arguments")
                    if (
                        isinstance(args, list)
                        and args
                        and all(isinstance(item, str) for item in args)
                    ):
                        commands.append((tuple(args),))
        elif profile.name.startswith("make-") and make_config and profile.selectors:
            closure, _ = make_target_closure(make_config, profile.selectors)
            for target in closure:
                for recipe in target.recipes:
                    try:
                        commands.append((tuple(shlex.split(recipe, posix=True)),))
                    except ValueError:
                        continue
    return tuple(commands)


def _discover_make_profiles(
    root: Path, ci_commands: Sequence[tuple[str, ...]]
) -> tuple[DiscoveredValidationProfile, ...]:
    configuration = discover_make_configuration(root)
    invocations = make_ci_invocations(ci_commands)
    workflow_invocations = make_workflow_invocations(root)
    if configuration is None and not invocations:
        return ()

    selections: tuple[tuple[tuple[str, ...], tuple[str, ...], str | None], ...]
    if invocations:
        if workflow_invocations:
            selections = tuple(
                (command, targets, path)
                for command, targets, path in workflow_invocations
            )
        else:
            selections = tuple(
                (command, targets, None) for command, targets in invocations
            )
        declaration = "declared"
    elif configuration:
        candidates = likely_make_validation_targets(configuration)
        selections = tuple((("make", target), (target,), None) for target in candidates)
        declaration = "inferred"
    else:
        return ()

    profiles: list[DiscoveredValidationProfile] = []
    for command, selected_targets, workflow_path in selections:
        if configuration and selected_targets:
            target_records, closure_diagnostics = make_target_closure(
                configuration, selected_targets
            )
        else:
            target_records = ()
            closure_diagnostics = ()
        unresolved = list(configuration.unresolved if configuration else ())
        unresolved.extend(closure_diagnostics)
        if not selected_targets:
            unresolved.append(
                "Make was invoked without explicit targets; default-goal behavior "
                "has not been resolved."
            )
        if configuration is None:
            unresolved.append(
                "No tracked Makefile was found to explain this native command."
            )
        for record in target_records:
            if not record.recipes and not record.prerequisites:
                unresolved.append(
                    f"Make target {record.name!r} has no statically visible commands."
                )
            for recipe in record.recipes:
                if any(marker in recipe for marker in ("$(", "${", "&&", "||", "|")):
                    unresolved.append(
                        f"Shell or variable expansion in {record.source} target "
                        f"{record.name!r} was preserved without expansion."
                    )
        target_names = ",".join(selected_targets) or "default"
        known_validation = bool(selected_targets) and all(
            target
            in {
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
            or target.startswith(
                ("check-", "lint-", "test-", "typecheck-", "validate-")
            )
            for target in selected_targets
        )
        if not known_validation:
            unresolved.append(
                "Make target names alone do not establish that the target validates "
                "code; inspect the recorded recipe and workflow context."
            )
        settings_targets = {
            record.name: {
                "prerequisites": list(record.prerequisites),
                "recipes": list(record.recipes),
                "source": record.source,
                "phony": record.phony,
            }
            for record in target_records
        }
        config_files = configuration.config_files if configuration else ()
        evidence = tuple(
            f"validation-input:{path}"
            for path in dict.fromkeys(
                (
                    *config_files,
                    *((workflow_path,) if workflow_path else ()),
                )
            )
        )
        profiles.append(
            DiscoveredValidationProfile(
                name=f"make-{target_names}",
                command=command,
                source="Makefile/GitHub Actions",
                provider_name="aggregate" if known_validation else "custom",
                purpose="Run native Make target(s) recorded in the project.",
                roles=("validation",) if known_validation else (),
                selectors=selected_targets,
                config_files=config_files,
                settings={"targets": settings_targets},
                declaration=declaration,
                evidence=evidence,
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _load_pyproject(root: Path) -> dict[str, object]:
    path = root / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_package_json(root: Path) -> dict[str, object]:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_script(package_json: dict[str, object], names: tuple[str, ...]) -> bool:
    scripts = package_json.get("scripts")
    return isinstance(scripts, dict) and any(name in scripts for name in names)


def _repository_text(root: Path) -> str:
    paths = ("Makefile", ".github/workflows/ci.yml", ".github/workflows/ci.yaml")
    chunks: list[str] = []
    for relative in paths:
        path = root / relative
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks).lower()


def _mentions_tool(tool: str, pyproject: dict[str, object], text: str) -> bool:
    if tool in text:
        return True
    serialized = repr(pyproject).lower()
    return tool in serialized


def _ci_commands(root: Path) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in content.splitlines():
            match = re.search(r"\brun:\s*(.+)$", line)
            if match:
                commands.append(_split_shell_arguments(match.group(1).strip()))
            else:
                stripped = line.strip()
                if stripped.startswith(
                    (
                        "uv run ",
                        "python -m ",
                        "pytest ",
                        "ruff ",
                        "mypy ",
                        "tox ",
                        "nox ",
                        "make ",
                        "gmake ",
                    )
                ):
                    commands.append(_split_shell_arguments(stripped))
    return tuple(commands)


def _split_shell_arguments(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError:
        return tuple(command.split())


def _find_command(
    commands: tuple[tuple[str, ...], ...], marker: tuple[str, ...]
) -> tuple[str, ...] | None:
    for command in commands:
        normalized = tuple(part.removeprefix("uv") for part in command)
        if _contains_marker(normalized, marker) and _is_locally_runnable(normalized):
            return command
    return None


def _is_locally_runnable(command: Sequence[str]) -> bool:
    """Reject CI commands containing unresolved template expressions.

    CI matrix/context expressions are useful in the originating workflow but
    are not executable command arguments in the task container.  Let the
    project-native fallback command be selected instead.
    """
    return not any(re.search(r"\$\{\{|\{\{|\}\}|\$\{[^}]+\}", part) for part in command)


def _contains_marker(command: tuple[str, ...], marker: tuple[str, ...]) -> bool:
    for index in range(len(command) - len(marker) + 1):
        if command[index : index + len(marker)] == marker:
            return True
    return False


__all__ = [
    "DiscoveredValidationProfile",
    "VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION",
    "discover_validation_profiles",
    "validation_inventory",
]
