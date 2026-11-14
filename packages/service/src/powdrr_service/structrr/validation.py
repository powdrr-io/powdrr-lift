"""Discover repository validation commands before declaring a feature complete."""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import yaml

from powdrr_service.structrr.python_topology import PythonTopology
from powdrr_service.structrr.validation_models import (
    VALIDATION_INVENTORY_SCHEMA_VERSION,
    ValidationCheck,
    ValidationContext,
)
from powdrr_service.structrr.validation_tasks import (
    HatchConfiguration,
    MakeConfiguration,
    NoxConfiguration,
    PdmConfiguration,
    PipenvConfiguration,
    PoeConfiguration,
    TaskRunnerConfiguration,
    ToxConfiguration,
    discover_hatch_configuration,
    discover_just_configuration,
    discover_make_configuration,
    discover_nox_configuration,
    discover_pdm_configuration,
    discover_pipenv_configuration,
    discover_poe_configuration,
    discover_pre_commit_configuration,
    discover_task_configuration,
    discover_tox_configuration,
    hatch_script_closure,
    likely_make_validation_targets,
    likely_task_targets,
    make_ci_invocations,
    make_target_closure,
    make_workflow_invocations,
    nox_ci_invocations,
    nox_workflow_invocations,
    pdm_script_closure,
    pipenv_script_closure,
    poe_task_closure,
    task_runner_closure,
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
                "code": "validation_discovery_unknown",
                "message": (
                    "No validation obligation was discovered. Validation readiness "
                    "is unknown until a check is declared or discovered."
                ),
            }
        )
    return ValidationContext(
        discovery_status="unknown" if not profiles else "partial",
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


def validation_profiles_from_inventory(
    inventory: object,
) -> tuple[DiscoveredValidationProfile, ...]:
    """Load Workrr profiles from the typed Structrr validation inventory."""
    if not isinstance(inventory, Sequence) or isinstance(inventory, (str, bytes)):
        raise ValueError("validation inventory must be a sequence")
    profiles: list[DiscoveredValidationProfile] = []
    for index, record in enumerate(inventory):
        if not isinstance(record, Mapping):
            raise ValueError(f"validation inventory entry {index} must be a mapping")
        try:
            check = ValidationCheck.from_data(record)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"validation inventory entry {index} is invalid: {error}"
            ) from error
        execution = check.execution
        declaration = check.provenance.get("declaration", "unknown")
        evidence = check.provenance.get("evidence", ())
        profiles.append(
            DiscoveredValidationProfile(
                name=check.profile,
                command=check.command,
                source=check.source,
                provider_name=check.provider,
                component=check.component,
                environment_id=check.environment,
                purpose=check.purpose,
                roles=check.roles,
                cwd=str(execution.get("cwd", ".")),
                shell=_optional_string(execution.get("shell")),
                script=_optional_string(execution.get("script")),
                execution_kind=str(execution.get("kind", "unresolved")),
                selectors=check.selectors,
                config_files=check.config_files,
                settings=dict(check.settings),
                applicability=dict(check.applicability),
                ci_origins=tuple(dict(origin) for origin in check.ci_origins),
                requiredness=dict(check.requiredness),
                declaration=(
                    declaration if isinstance(declaration, str) else "unknown"
                ),
                evidence=(
                    tuple(item for item in evidence if isinstance(item, str))
                    if isinstance(evidence, Sequence)
                    and not isinstance(evidence, (str, bytes))
                    else ()
                ),
                confirmation=dict(check.confirmation),
                depends_on=check.depends_on,
                local_reproducibility=check.local_reproducibility,
                baseline=dict(check.baseline),
                unresolved=check.unresolved,
            )
        )
    return tuple(profiles)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def assign_python_topology(
    profiles: Sequence[DiscoveredValidationProfile], topology: PythonTopology
) -> tuple[DiscoveredValidationProfile, ...]:
    """Attach Python topology and canonical validation evidence references."""
    root_component = next(
        (item for item in topology.components if item.get("path") == "."), None
    )
    component_id = str(root_component["id"]) if root_component else None
    environment_id: str | None = None
    if component_id:
        environment = next(
            (
                item
                for item in topology.environments
                if item.get("component") == component_id
            ),
            None,
        )
        environment_id = str(environment["id"]) if environment else None
    evidence_ids = {f"validation-input:{path}" for path in topology.evidence_files}
    python_providers = {
        "ruff",
        "mypy",
        "pytest",
        "pyright",
        "basedpyright",
        "flake8",
        "black",
    }
    normalized_profiles: list[DiscoveredValidationProfile] = []
    for profile in profiles:
        normalized_evidence = tuple(
            evidence
            if evidence in evidence_ids
            else f"validation-input:{evidence}"
            if f"validation-input:{evidence}" in evidence_ids
            else evidence
            for evidence in profile.evidence
        )
        python_profile = profile.provider in python_providers and component_id
        normalized_profiles.append(
            replace(
                profile,
                evidence=normalized_evidence,
                component=(profile.component or component_id)
                if python_profile
                else profile.component,
                environment_id=(profile.environment_id or environment_id)
                if python_profile
                else profile.environment_id,
            )
        )
    return tuple(normalized_profiles)


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
    _include_nested: bool = True,
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
    ci_origins = _github_validation_origins(root_path)
    ci_commands = tuple(
        command
        for origin in ci_origins
        for command in _commands_from_script(str(origin.get("run", "")))
    )
    uv_prefix = ("uv", "run") if (root_path / "pyproject.toml").exists() else ()
    text = _repository_text(root_path)
    profiles.extend(_discover_tox_profiles(root_path, ci_commands))
    profiles.extend(_discover_nox_profiles(root_path, ci_commands))
    profiles.extend(_discover_make_profiles(root_path, ci_commands))
    pipenv_configuration = discover_pipenv_configuration(root_path)
    profiles.extend(
        _discover_pipenv_profiles(root_path, ci_commands, pipenv_configuration)
    )
    poe_configuration = discover_poe_configuration(root_path)
    profiles.extend(_discover_poe_profiles(root_path, ci_commands, poe_configuration))
    pdm_configuration = discover_pdm_configuration(root_path)
    profiles.extend(_discover_pdm_profiles(root_path, ci_commands, pdm_configuration))
    hatch_configuration = discover_hatch_configuration(root_path)
    profiles.extend(
        _discover_hatch_profiles(root_path, ci_commands, hatch_configuration)
    )
    just_configuration = discover_just_configuration(root_path)
    task_configuration = discover_task_configuration(root_path)
    profiles.extend(
        _discover_task_runner_profiles(
            root_path, ci_commands, "just", just_configuration
        )
    )
    profiles.extend(
        _discover_task_runner_profiles(
            root_path, ci_commands, "task", task_configuration
        )
    )
    profiles.extend(_discover_pre_commit_profiles(root_path, ci_commands))
    tox_configuration = discover_tox_configuration(root_path)
    nox_configuration = discover_nox_configuration(root_path)
    make_configuration = discover_make_configuration(root_path)
    task_child_tools = {
        token
        for command_set in _task_runner_commands(
            tox_configuration,
            nox_configuration,
            make_configuration,
            hatch_configuration,
            pdm_configuration,
            pipenv_configuration,
            poe_configuration,
            just_configuration,
            task_configuration,
            profiles,
        )
        for command in command_set
        for token in command
        if token in {"pytest", "ruff", "mypy", "black", "flake8", "pyright"}
    }
    task_child_tools.update(_pre_commit_child_tools(profiles))
    has_task_aggregate = any(
        profile.provider == "aggregate"
        and (
            profile.name.startswith("tox-")
            or profile.name.startswith("nox-")
            or profile.name.startswith("make-")
            or profile.name.startswith("pre-commit-")
            or profile.name.startswith("just-")
            or profile.name.startswith("task-")
            or profile.name.startswith("hatch-")
            or profile.name.startswith("pdm-")
            or profile.name.startswith("pipenv-")
            or profile.name.startswith("poe-")
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
    stestr_commands = tuple(
        command
        for command in ci_commands
        if any(Path(part).name.casefold() == "stestr" for part in command)
    )
    if (root_path / ".stestr.conf").is_file() or stestr_commands:
        profiles.append(
            DiscoveredValidationProfile(
                "stestr",
                stestr_commands[0]
                if stestr_commands
                else ("stestr", "run", "--concurrency", "1"),
                "stestr configuration/CI",
                provider_name="stestr",
                declaration="declared" if stestr_commands else "inferred",
            )
        )

    package_json = _load_package_json(root_path)
    if package_json and _has_script(package_json, ("test", "check", "lint")):
        package_manager = _package_manager(root_path, package_json)
        profiles.append(
            _profile(
                f"{package_manager}-test",
                _find_command(ci_commands, (package_manager, "test"))
                or (package_manager, "test"),
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
    profiles = list(unique.values())
    if _include_nested:
        profiles.extend(_discover_nested_validation_profiles(root_path))
        profiles.extend(_discover_reusable_workflow_profiles(root_path))
        # Use the exact declared command for a detected check when a workflow
        # provides one, including package selectors and nested working dirs.
        for origin in ci_origins:
            base_cwd = str(origin.get("working_directory", ".")).strip() or "."
            for index, (command, cwd) in enumerate(
                _commands_with_working_directory(str(origin.get("run", "")), base_cwd),
                start=1,
            ):
                if (
                    cwd.startswith(("/", "~"))
                    or ".." in Path(cwd).parts
                    or not (root_path / cwd).is_dir()
                ):
                    continue
                if not _is_locally_runnable(command):
                    continue
                kind = _validation_command_kind(command)
                if kind is None:
                    continue
                unresolved_script = _unresolved_script_profile(
                    root_path, origin, command, cwd
                )
                if unresolved_script is not None:
                    profiles.append(unresolved_script)
                    continue
                exact_match_index = next(
                    (
                        index
                        for index, profile in enumerate(profiles)
                        if profile.cwd == cwd
                        and profile.command == command
                        and _validation_command_kind(profile.command) == kind
                    ),
                    None,
                )
                if exact_match_index is not None:
                    profiles[exact_match_index] = replace(
                        profiles[exact_match_index],
                        source="GitHub Actions workflow",
                        declaration="declared",
                        ci_origins=(
                            *profiles[exact_match_index].ci_origins,
                            origin,
                        ),
                    )
                else:
                    fallback_match_index = next(
                        (
                            index
                            for index, profile in enumerate(profiles)
                            if profile.cwd == cwd
                            and profile.source != "GitHub Actions workflow"
                            and _validation_command_kind(profile.command) == kind
                            and profile.provider == _validation_provider(command)
                        ),
                        None,
                    )
                    if fallback_match_index is not None:
                        profiles[fallback_match_index] = replace(
                            profiles[fallback_match_index],
                            command=command,
                            source="GitHub Actions workflow",
                            declaration="declared",
                            ci_origins=(origin,),
                        )
                        continue
                    variant = (
                        sum(
                            profile.cwd == cwd
                            and _validation_command_kind(profile.command) == kind
                            for profile in profiles
                        )
                        + 1
                    )
                    cwd_name = cwd.strip("./").replace("/", "-") or "root"
                    provider = _validation_provider(command)
                    name = _validation_profile_name(
                        command,
                        provider=provider,
                        cwd_name=cwd_name,
                        variant=variant,
                    )
                    evidence_paths = [str(origin.get("workflow", ""))]
                    for token in command:
                        normalized = token.removeprefix("./")
                        if (
                            normalized.startswith("scripts/")
                            and (root_path / cwd / normalized).is_file()
                        ):
                            evidence_paths.append((Path(cwd) / normalized).as_posix())
                    profiles.append(
                        DiscoveredValidationProfile(
                            name=name,
                            command=command,
                            source="GitHub Actions workflow",
                            provider_name=provider,
                            cwd=cwd,
                            declaration="declared",
                            evidence=tuple(
                                path for path in dict.fromkeys(evidence_paths) if path
                            ),
                            ci_origins=(origin,),
                        )
                    )
        unique = {}
        for profile in profiles:
            unique.setdefault(profile.name, profile)
        profiles = list(unique.values())
    enriched: list[DiscoveredValidationProfile] = []
    for profile in profiles:
        markers = _profile_markers(profile)
        matches = tuple(
            origin
            for origin in ci_origins
            if markers
            and any(
                _contains_marker(command, markers)
                for command in _commands_from_script(str(origin.get("run", "")))
            )
            and any(
                _validation_provider(command) == profile.provider
                for command in _commands_from_script(str(origin.get("run", "")))
            )
        )
        evidence_items = list(profile.evidence)
        known_evidence = {
            item.removeprefix("validation-input:") for item in evidence_items
        }
        if matches:
            for origin in matches:
                workflow = str(origin.get("workflow", ""))
                if workflow and workflow not in known_evidence:
                    evidence_items.append(workflow)
                    known_evidence.add(workflow)
        for path in _profile_configuration_evidence(root_path, profile, pyproject):
            if path not in known_evidence:
                evidence_items.append(path)
                known_evidence.add(path)
        if matches or tuple(evidence_items) != profile.evidence:
            enriched.append(
                replace(
                    profile,
                    ci_origins=matches or profile.ci_origins,
                    evidence=tuple(evidence_items),
                )
            )
        else:
            enriched.append(profile)
    return tuple(enriched)


def _profile_configuration_evidence(
    root: Path,
    profile: DiscoveredValidationProfile,
    pyproject: Mapping[str, object],
) -> tuple[str, ...]:
    """Attach project files that establish a discovered provider or task."""
    evidence: list[str] = []
    serialized_pyproject = repr(pyproject).lower()
    pyproject_path = root / "pyproject.toml"
    if (
        profile.provider in {"pytest", "mypy", "ruff"}
        and pyproject_path.is_file()
        and profile.provider in serialized_pyproject
    ):
        evidence.append("pyproject.toml")
    if profile.provider == "pytest":
        requirements = tuple(
            path.name
            for path in sorted(root.glob("requirements*.txt"))
            if "pytest" in path.read_text(encoding="utf-8", errors="replace").lower()
        )
        evidence.extend(requirements)
    if profile.provider == "npm" and (root / "package.json").is_file():
        evidence.append("package.json")
    if profile.provider == "go" and (root / "go.mod").is_file():
        evidence.append("go.mod")
    if profile.provider == "cargo" and (root / "Cargo.toml").is_file():
        evidence.append("Cargo.toml")
    if profile.provider == "maven" and (root / "pom.xml").is_file():
        evidence.append("pom.xml")
    return tuple(dict.fromkeys(evidence))


def _unresolved_script_profile(
    root: Path,
    origin: Mapping[str, object],
    command: tuple[str, ...],
    cwd: str,
) -> DiscoveredValidationProfile | None:
    """Keep native scripts whose shell behavior cannot be reduced to argv."""
    script = next(
        (
            token.removeprefix("./")
            for token in command
            if Path(token).name in {"check_migrations.py", "check", "test"}
            and (root / cwd / token).is_file()
        ),
        None,
    )
    if script is None:
        return None
    script_path = (Path(cwd) / script).as_posix()
    if script_path.endswith("check_migrations.py"):
        name = "django-migration-check"
        reason = (
            "The migration script depends on workflow-created settings and a "
            "PostgreSQL service that static discovery does not provision."
        )
    elif Path(script_path).name == "check":
        name = "httpx-check-script"
        reason = (
            "The native check script contains shell variables and multiple "
            "commands; its child invocations are preserved without expansion."
        )
    else:
        name = "httpx-test-script"
        reason = (
            "The native test script contains shell conditions and forwards "
            "arguments; its child invocations are preserved without expansion."
        )
    workflow = str(origin.get("workflow", ""))
    return DiscoveredValidationProfile(
        name=name,
        command=(),
        source="GitHub Actions workflow/native script",
        provider_name="custom",
        purpose="Run the repository-declared validation script.",
        cwd=cwd,
        execution_kind="unresolved",
        settings={"declaration": list(command), "script": script_path},
        applicability={"local": True, "evaluation": "unknown"},
        ci_origins=(dict(origin),),
        declaration="declared",
        evidence=tuple(path for path in (workflow, script_path) if path),
        unresolved=(reason,),
    )


def _discover_nested_validation_profiles(
    root: Path,
) -> tuple[DiscoveredValidationProfile, ...]:
    """Discover checks declared by nested packages and workspace members."""
    manifests = {
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "noxfile.py",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "gradlew",
    }
    ignored = {
        ".git",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "fixtures",
        "test",
        "tests",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "vendor",
        "venv",
    }
    project_roots: set[Path] = set()
    for directory, child_dirs, filenames in os.walk(root):
        child_dirs[:] = [name for name in child_dirs if name not in ignored]
        if Path(directory) == root:
            continue
        if manifests.intersection(filenames):
            project_roots.add(Path(directory))

    discovered: list[DiscoveredValidationProfile] = []
    for project_root in sorted(project_roots):
        relative = project_root.relative_to(root).as_posix()
        for profile in discover_validation_profiles(
            project_root, _include_nested=False
        ):
            discovered.append(
                replace(
                    profile,
                    name=f"{relative.replace('/', '-')}-{profile.name}",
                    provider_name=profile.provider,
                    cwd=(
                        relative if profile.cwd == "." else f"{relative}/{profile.cwd}"
                    ),
                    evidence=tuple(f"{relative}/{item}" for item in profile.evidence),
                )
            )
    return tuple(discovered)


def _discover_reusable_workflow_profiles(
    root: Path,
) -> tuple[DiscoveredValidationProfile, ...]:
    """Retain validation jobs delegated to reusable GitHub workflows."""
    workflow_root = root / ".github" / "workflows"
    discovered: list[DiscoveredValidationProfile] = []
    for path in sorted(workflow_root.glob("*.y*ml")):
        try:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if not isinstance(uses, str) or "/.github/workflows/" not in uses:
                continue
            target = uses.split("@", 1)[0]
            label = " ".join((str(job_id), str(job.get("name", "")), target)).lower()
            if not any(
                marker in label
                for marker in ("check", "test", "lint", "format", "validate", "tox")
            ):
                continue
            target_path = target.removeprefix("./")
            target_name = Path(target_path).stem
            local_target = target.startswith("./")
            workflow_name = path.relative_to(root).as_posix()
            if "tox-dev/github-workflows" in target:
                name = f"reusable-{job_id}-tox-workflow"
            elif str(job_id) == "main-linux" and "tox" in target:
                name = "reusable-main-linux-tox"
            else:
                name = f"reusable-{target_name}"
            evidence = [workflow_name]
            if local_target:
                evidence.append(target_path)
            origin = {
                "workflow": workflow_name,
                "job_id": str(job_id),
                "job_name": str(job.get("name", job_id)),
                "uses": uses,
                "evidence": "GitHub Actions reusable workflow job uses",
            }
            discovered.append(
                DiscoveredValidationProfile(
                    name=name,
                    command=(),
                    source="GitHub Actions reusable workflow",
                    provider_name="aggregate",
                    purpose="Delegate validation to a reusable GitHub workflow.",
                    roles=("validation",),
                    execution_kind="unresolved",
                    settings={"uses": uses, "local_target": local_target},
                    applicability={"local": False, "evaluation": "unknown"},
                    ci_origins=(origin,),
                    declaration="declared",
                    evidence=tuple(evidence),
                    unresolved=(
                        "The reusable workflow invocation is recorded, but its "
                        "job matrix and effective validation commands are not "
                        "expanded into local executable profiles.",
                    ),
                )
            )
    return tuple(discovered)


def _validation_command_kind(command: tuple[str, ...]) -> str | None:
    tokens = tuple(token.lower() for token in command)
    if _validation_provider(command) == "custom":
        return "custom"
    if (
        any(tool in tokens for tool in ("pytest", "stestr", "vitest", "unittest"))
        or "test" in tokens
        or any(Path(token).name == "runtests.py" for token in tokens)
    ):
        return "test"
    if "ruff" in tokens and "format" in tokens:
        return "format"
    if any(tool in tokens for tool in ("ruff", "eslint", "flake8")):
        return "lint"
    if any(tool in tokens for tool in ("mypy", "pyright", "tsc")):
        return "typecheck"
    if any(tool in tokens for tool in ("go", "cargo")) and any(
        token in tokens for token in ("test", "check")
    ):
        return "test"
    return None


def _validation_provider(command: Sequence[str]) -> str | None:
    """Identify the native validation provider without guessing from profile names."""
    tokens = tuple(token.lower() for token in command)
    if "pytest" in tokens:
        return "pytest"
    if "stestr" in tokens:
        return "stestr"
    if "vitest" in tokens:
        return "vitest"
    if "unittest" in tokens or any(
        Path(token).name == "runtests.py" for token in tokens
    ):
        return "unittest"
    if "ruff" in tokens:
        return "ruff"
    if "mypy" in tokens:
        return "mypy"
    if "pyright" in tokens:
        return "pyright"
    if "basedpyright" in tokens:
        return "basedpyright"
    if "eslint" in tokens or "flake8" in tokens:
        return "lint"
    if "npm" in tokens or "pnpm" in tokens:
        return next(token for token in tokens if token in {"npm", "pnpm"})
    if "go" in tokens:
        return "go"
    if "cargo" in tokens:
        return "cargo"
    if "tox" in tokens:
        return "aggregate"
    if "nox" in tokens:
        return "aggregate"
    if any(
        Path(token).name in {"check_migrations.py"}
        or Path(token).as_posix().lstrip("./") in {"scripts/check", "scripts/test"}
        or Path(token).name == "run-deterministic-workflow-suite.sh"
        for token in tokens
    ) or any(
        tokens[index : index + 2] == ("powdrr-lift", "validate-procedrr-processes")
        or tokens[index : index + 2] == ("powdrr-lift", "workflow-scenario-suite")
        for index in range(max(0, len(tokens) - 1))
    ):
        return "custom"
    if "test" in tokens:
        return "custom"
    return None


def _validation_profile_name(
    command: Sequence[str], *, provider: str, cwd_name: str, variant: int
) -> str:
    tokens = tuple(token.lower() for token in command)
    if "validate-procedrr-processes" in tokens:
        return "validate-procedrr-processes"
    if "workflow-scenario-suite" in tokens or any(
        Path(token).name == "run-deterministic-workflow-suite.sh" for token in tokens
    ):
        return "workflow-scenario-suite"
    if any(Path(token).name == "check_migrations.py" for token in tokens):
        return "django-migration-check"
    if any(Path(token).as_posix().lstrip("./") == "scripts/check" for token in tokens):
        return "httpx-check-script"
    if any(Path(token).as_posix().lstrip("./") == "scripts/test" for token in tokens):
        return "httpx-test-script"
    if provider == "unittest" and any(
        Path(token).name == "runtests.py" for token in tokens
    ):
        return "django-unittest"
    if provider == "unittest":
        return f"ci-{cwd_name}-unittest-{variant}"
    kind = _validation_command_kind(tuple(command)) or "custom"
    return f"ci-{cwd_name}-{kind}-{variant}"


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
        dynamic_invocation = not _is_locally_runnable(command)
        name = (
            "tox-dynamic-matrix"
            if dynamic_invocation
            else f"tox-{suffix}"
            if len(selections) == 1
            else f"tox-{index}-{suffix}"
        )
        configured_commands = {
            environment: [list(item) for item in commands]
            for environment, commands in commands_by_env.items()
            if environment == "*" or environment in environments
        }
        settings: dict[str, object] = {
            "environments": list(environments),
            "configured_commands": configured_commands,
        }
        if dynamic_invocation:
            settings["declared_command"] = list(command)
        profiles.append(
            DiscoveredValidationProfile(
                name=name,
                command=() if dynamic_invocation else command,
                source="tox configuration/CI",
                provider_name="aggregate",
                purpose="Run the project-declared tox validation environment(s).",
                roles=("validation",),
                selectors=environments,
                config_files=config_files,
                settings=settings,
                execution_kind="unresolved" if dynamic_invocation else "argv",
                declaration=declaration,
                evidence=tuple(
                    dict.fromkeys(
                        (*config_files, *((workflow_path,) if workflow_path else ()))
                    )
                ),
                unresolved=(
                    tuple(
                        dict.fromkeys(
                            (
                                *unresolved,
                                "The tox environment selector contains a GitHub "
                                "matrix expression and cannot be resolved statically.",
                            )
                        )
                    )
                    if dynamic_invocation
                    else unresolved
                ),
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
    hatch_configuration: object,
    pdm_configuration: object,
    pipenv_configuration: object,
    poe_configuration: object,
    just_configuration: object,
    task_configuration: object,
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
    just_config = (
        cast(TaskRunnerConfiguration, just_configuration)
        if just_configuration is not None
        else None
    )
    hatch_config = (
        cast(HatchConfiguration, hatch_configuration)
        if hatch_configuration is not None
        else None
    )
    pdm_config = (
        cast(PdmConfiguration, pdm_configuration)
        if pdm_configuration is not None
        else None
    )
    pipenv_config = (
        cast(PipenvConfiguration, pipenv_configuration)
        if pipenv_configuration is not None
        else None
    )
    poe_config = (
        cast(PoeConfiguration, poe_configuration)
        if poe_configuration is not None
        else None
    )
    task_config = (
        cast(TaskRunnerConfiguration, task_configuration)
        if task_configuration is not None
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
            make_targets, _ = make_target_closure(make_config, profile.selectors)
            for target in make_targets:
                for recipe in target.recipes:
                    try:
                        commands.append((tuple(shlex.split(recipe, posix=True)),))
                    except ValueError:
                        continue
        elif profile.name.startswith("just-") and just_config and profile.selectors:
            just_tasks, _ = task_runner_closure(just_config, profile.selectors)
            for task in just_tasks:
                for command in task.commands:
                    try:
                        commands.append((tuple(shlex.split(command, posix=True)),))
                    except ValueError:
                        continue
        elif (
            profile.name.startswith("hatch-")
            and hatch_config
            and len(profile.selectors) == 2
        ):
            environment, script = profile.selectors
            script_commands, _ = hatch_script_closure(hatch_config, environment, script)
            for command in script_commands:
                try:
                    commands.append((tuple(shlex.split(command, posix=True)),))
                except ValueError:
                    continue
        elif profile.name.startswith("pdm-") and pdm_config and profile.selectors:
            pdm_commands, _ = pdm_script_closure(pdm_config, profile.selectors[0])
            commands.extend((command,) for command in pdm_commands)
        elif profile.name.startswith("pipenv-") and pipenv_config and profile.selectors:
            pipenv_commands, _ = pipenv_script_closure(
                pipenv_config, profile.selectors[0]
            )
            commands.extend((command,) for command in pipenv_commands)
        elif profile.name.startswith("poe-") and poe_config and profile.selectors:
            poe_commands, _ = poe_task_closure(poe_config, profile.selectors[0])
            commands.extend((command,) for command in poe_commands)
        elif profile.name.startswith("task-") and task_config and profile.selectors:
            task_tasks, _ = task_runner_closure(task_config, profile.selectors)
            for task in task_tasks:
                for command in task.commands:
                    try:
                        commands.append((tuple(shlex.split(command, posix=True)),))
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
                "stestr",
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


def _discover_task_runner_profiles(
    root: Path,
    ci_commands: Sequence[tuple[str, ...]],
    runner: str,
    configuration: TaskRunnerConfiguration | None,
) -> tuple[DiscoveredValidationProfile, ...]:
    invocations = tuple(
        command
        for command in ci_commands
        if any(Path(token).name == runner for token in command)
    )
    if configuration is None and not invocations:
        return ()
    if invocations:
        selections = tuple(
            (command, _task_runner_selectors(command, runner))
            for command in invocations
        )
        declaration = "declared"
    elif configuration:
        selections = tuple(
            ((runner, target), (target,))
            for target in likely_task_targets(configuration)
        )
        declaration = "inferred"
    else:
        return ()
    config_files = configuration.config_files if configuration else ()
    validation_names = {
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
    workflow_paths = tuple(
        path
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if re.search(rf"\b{re.escape(runner)}\b", _read_text(path))
    )
    profiles: list[DiscoveredValidationProfile] = []
    for index, (command, selectors) in enumerate(selections, start=1):
        if configuration and selectors:
            tasks, diagnostics = task_runner_closure(configuration, selectors)
        else:
            tasks, diagnostics = (), ()
        unresolved = list(configuration.unresolved if configuration else ())
        unresolved.extend(diagnostics)
        if not selectors:
            unresolved.append(
                f"{runner} was invoked without a static task selector; default task "
                "behavior is unresolved."
            )
        explicit_config = _task_runner_config_argument(command, runner)
        if explicit_config and configuration and explicit_config not in config_files:
            unresolved.append(
                f"{runner} selects {explicit_config!r}, which differs from the "
                "discovered root task file."
            )
        if configuration is None:
            unresolved.append(f"No {runner} task file was found for this invocation.")
        for task in tasks:
            unresolved.extend(task.unresolved)
            for recipe in task.commands:
                if any(marker in recipe for marker in ("{{", "${", "&&", "||", "|")):
                    unresolved.append(
                        f"Dynamic expansion or shell composition in {task.source} "
                        f"task {task.name!r} is preserved without interpretation."
                    )
        known_validation = bool(selectors) and all(
            name in validation_names
            or name.startswith(("check-", "lint-", "test-", "typecheck-", "validate-"))
            for name in selectors
        )
        target_settings = {
            task.name: {
                "prerequisites": list(task.prerequisites),
                "commands": list(task.commands),
                "source": task.source,
                "execution_settings": dict(task.execution_settings or {}),
            }
            for task in tasks
        }
        target_names = ",".join(selectors) or "default"
        evidence_paths = [*config_files]
        evidence_paths.extend(
            path.relative_to(root).as_posix() for path in workflow_paths
        )
        profiles.append(
            DiscoveredValidationProfile(
                name=f"{runner}-{target_names}"
                if len(selections) == 1
                else f"{runner}-{index}-{target_names}",
                command=command,
                source=f"{runner} task file/GitHub Actions",
                provider_name="aggregate" if known_validation else "custom",
                purpose=f"Run native {runner} task(s) recorded in the project.",
                roles=("validation",) if known_validation else (),
                selectors=selectors,
                config_files=config_files,
                settings={"tasks": target_settings},
                declaration=declaration,
                evidence=tuple(
                    f"validation-input:{path}" for path in dict.fromkeys(evidence_paths)
                ),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _discover_poe_profiles(
    root: Path,
    ci_commands: Sequence[tuple[str, ...]],
    configuration: PoeConfiguration | None,
) -> tuple[DiscoveredValidationProfile, ...]:
    invocations = tuple(
        (command, _poe_task_selector(command))
        for command in ci_commands
        if _is_poe_invocation(command)
    )
    if configuration is None and not invocations:
        return ()
    validation_names = {
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
    if invocations:
        selections = invocations
        declaration = "declared"
    elif configuration:
        names = tuple(
            name
            for name in configuration.tasks
            if name in validation_names
            or name.startswith(
                (
                    "check-",
                    "check_",
                    "lint-",
                    "lint_",
                    "test-",
                    "test_",
                    "typecheck-",
                    "validate-",
                )
            )
        )
        selections = tuple((("poe", name), name) for name in names)
        declaration = "inferred"
    else:
        return ()
    workflow_paths = tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if re.search(r"\bpoe\b|poethepoet", _read_text(path))
    )
    profiles: list[DiscoveredValidationProfile] = []
    for index, (command, selector) in enumerate(selections, start=1):
        unresolved = list(configuration.unresolved if configuration else ())
        task = configuration.tasks.get(selector) if configuration and selector else None
        expanded: tuple[tuple[str, ...], ...] = ()
        if configuration and selector:
            expanded, diagnostics = poe_task_closure(configuration, selector)
            unresolved.extend(diagnostics)
        elif selector:
            unresolved.append("Poe invocation has no static task configuration.")
        if selector is None:
            unresolved.append("Poe invocation has no statically selected task.")
        known_validation = selector is not None and (
            selector in validation_names
            or selector.startswith(
                (
                    "check-",
                    "check_",
                    "lint-",
                    "lint_",
                    "test-",
                    "test_",
                    "typecheck-",
                    "validate-",
                )
            )
        )
        evidence_paths = (
            (configuration.config_file,) if configuration else ()
        ) + workflow_paths
        profiles.append(
            DiscoveredValidationProfile(
                name=f"poe-{selector or 'default'}"
                if len(selections) == 1
                else f"poe-{index}-{selector or 'default'}",
                command=command,
                source="Poe tasks/GitHub Actions",
                provider_name="aggregate" if known_validation else "custom",
                purpose="Run the native Poe task recorded in the project.",
                roles=("validation",) if known_validation else (),
                selectors=(selector,) if selector else (),
                config_files=(configuration.config_file,) if configuration else (),
                settings={
                    "task": task,
                    "global": dict(configuration.settings) if configuration else {},
                    "expanded_commands": [list(item) for item in expanded],
                },
                declaration=declaration,
                evidence=tuple(
                    f"validation-input:{path}" for path in dict.fromkeys(evidence_paths)
                ),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _poe_task_selector(command: Sequence[str]) -> str | None:
    index: int | None = None
    for position, token in enumerate(command):
        if Path(token).name == "poe":
            index = position + 1
            break
        if (
            token == "-m"
            and position + 1 < len(command)
            and command[position + 1] == "poethepoet"
        ):
            index = position + 2
            break
    if index is None:
        return None
    tail = list(command[index:])
    value_options = {"-C", "--directory", "--executor", "--executor-opt"}
    position = 0
    while position < len(tail):
        token = tail[position]
        if token in value_options:
            position += 2
        elif token.startswith("-"):
            position += 1
        else:
            return token
    return None


def _is_poe_invocation(command: Sequence[str]) -> bool:
    return any(
        Path(token).name == "poe"
        or (
            token == "-m"
            and index + 1 < len(command)
            and command[index + 1] == "poethepoet"
        )
        for index, token in enumerate(command)
    )


def _discover_pipenv_profiles(
    root: Path,
    ci_commands: Sequence[tuple[str, ...]],
    configuration: PipenvConfiguration | None,
) -> tuple[DiscoveredValidationProfile, ...]:
    invocations = tuple(
        command
        for command in ci_commands
        if _pipenv_script_selector(command) is not None
    )
    if configuration is None and not invocations:
        return ()
    validation_names = {
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
    if invocations:
        selections = tuple(
            (command, _pipenv_script_selector(command)) for command in invocations
        )
        declaration = "declared"
    elif configuration:
        names = tuple(
            name
            for name in configuration.scripts
            if name in validation_names
            or name.startswith(("check_", "lint_", "test_", "type_check_", "validate_"))
        )
        selections = tuple((("pipenv", "run", name), name) for name in names)
        declaration = "inferred"
    else:
        return ()
    workflow_paths = tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if re.search(r"\bpipenv\b", _read_text(path))
    )
    profiles: list[DiscoveredValidationProfile] = []
    for index, (command, selector) in enumerate(selections, start=1):
        unresolved = list(configuration.unresolved if configuration else ())
        script = (
            configuration.scripts.get(selector) if configuration and selector else None
        )
        expanded: tuple[tuple[str, ...], ...] = ()
        if configuration and selector:
            expanded, diagnostics = pipenv_script_closure(configuration, selector)
            unresolved.extend(diagnostics)
        elif selector:
            unresolved.append("Pipenv script invocation has no Pipfile declaration.")
        if selector is None:
            unresolved.append("Pipenv run has no statically selected script.")
        known_validation = selector is not None and (
            selector in validation_names
            or selector.startswith(
                ("check_", "lint_", "test_", "type_check_", "validate_")
            )
        )
        evidence_paths = (
            (configuration.config_file,) if configuration else ()
        ) + workflow_paths
        profiles.append(
            DiscoveredValidationProfile(
                name=f"pipenv-{selector or 'default'}"
                if len(selections) == 1
                else f"pipenv-{index}-{selector or 'default'}",
                command=command,
                source="Pipenv scripts/GitHub Actions",
                provider_name="aggregate" if known_validation else "custom",
                purpose="Run the native Pipenv script recorded in the project.",
                roles=("validation",) if known_validation else (),
                selectors=(selector,) if selector else (),
                config_files=(configuration.config_file,) if configuration else (),
                settings={
                    "script": dict(script) if script else {},
                    "expanded_commands": [list(item) for item in expanded],
                },
                declaration=declaration,
                evidence=tuple(
                    f"validation-input:{path}" for path in dict.fromkeys(evidence_paths)
                ),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _pipenv_script_selector(command: Sequence[str]) -> str | None:
    pipenv_index = next(
        (i for i, token in enumerate(command) if Path(token).name == "pipenv"), None
    )
    if pipenv_index is None:
        return None
    tail = command[pipenv_index + 1 :]
    if len(tail) < 2 or tail[0] != "run":
        return None
    return tail[1] if not tail[1].startswith("-") else None


def _discover_pdm_profiles(
    root: Path,
    ci_commands: Sequence[tuple[str, ...]],
    configuration: PdmConfiguration | None,
) -> tuple[DiscoveredValidationProfile, ...]:
    invocations = tuple(
        command for command in ci_commands if _pdm_script_selector(command) is not None
    )
    if configuration is None and not invocations:
        return ()
    validation_names = {
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
    if invocations:
        selections = tuple(
            (command, _pdm_script_selector(command)) for command in invocations
        )
        declaration = "declared"
    elif configuration:
        names = tuple(
            name
            for name in configuration.scripts
            if name in validation_names
            or name.startswith(("check-", "lint-", "test-", "typecheck-", "validate-"))
        )
        selections = tuple((("pdm", "run", name), name) for name in names)
        declaration = "inferred"
    else:
        return ()
    workflow_paths = tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if re.search(r"\bpdm\b", _read_text(path))
    )
    profiles: list[DiscoveredValidationProfile] = []
    for index, (command, selector) in enumerate(selections, start=1):
        unresolved = list(configuration.unresolved if configuration else ())
        script = (
            configuration.scripts.get(selector) if configuration and selector else None
        )
        commands: tuple[tuple[str, ...], ...] = ()
        if configuration and selector:
            commands, diagnostics = pdm_script_closure(configuration, selector)
            unresolved.extend(diagnostics)
            if (
                f"pre_{selector}" in configuration.scripts
                or f"post_{selector}" in configuration.scripts
            ):
                unresolved.append(
                    f"PDM pre/post scripts for {selector!r} may run around "
                    "the selected task."
                )
        elif selector:
            unresolved.append(
                "PDM script invocation has no pyproject.toml declaration."
            )
        if selector is None:
            unresolved.append("PDM run has no statically selected script.")
        known_validation = selector is not None and (
            selector in validation_names
            or selector.startswith(
                ("check-", "lint-", "test-", "typecheck-", "validate-")
            )
        )
        settings: dict[str, object] = {
            "script": dict(script) if script else {},
            "shared_options": dict(configuration.shared_options)
            if configuration
            else {},
            "expanded_commands": [list(item) for item in commands],
        }
        evidence_paths = (
            (configuration.config_file,) if configuration else ()
        ) + workflow_paths
        profiles.append(
            DiscoveredValidationProfile(
                name=f"pdm-{selector or 'default'}"
                if len(selections) == 1
                else f"pdm-{index}-{selector or 'default'}",
                command=command,
                source="PDM scripts/GitHub Actions",
                provider_name="aggregate" if known_validation else "custom",
                purpose="Run the native PDM script recorded in the project.",
                roles=("validation",) if known_validation else (),
                selectors=(selector,) if selector else (),
                config_files=(configuration.config_file,) if configuration else (),
                settings=settings,
                declaration=declaration,
                evidence=tuple(
                    f"validation-input:{path}" for path in dict.fromkeys(evidence_paths)
                ),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _pdm_script_selector(command: Sequence[str]) -> str | None:
    pdm_index = next(
        (i for i, token in enumerate(command) if Path(token).name == "pdm"), None
    )
    if pdm_index is None:
        return None
    tail = list(command[pdm_index + 1 :])
    if not tail:
        return None
    if tail[0] == "run":
        tail = tail[1:]
    elif not tail[0].startswith("-"):
        # PDM exposes scripts as root shortcuts unless they collide with commands.
        return tail[0]
    else:
        return None
    value_options = {
        "--env",
        "--env-file",
        "--working-dir",
        "--venv",
        "-p",
        "--project",
    }
    index = 0
    while index < len(tail):
        token = tail[index]
        if token in value_options:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            return token
    return None


def _discover_hatch_profiles(
    root: Path,
    ci_commands: Sequence[tuple[str, ...]],
    configuration: HatchConfiguration | None,
) -> tuple[DiscoveredValidationProfile, ...]:
    invocations = tuple(command for command in ci_commands if _is_hatch_run(command))
    if configuration is None and not invocations:
        return ()
    if invocations:
        selections = tuple(
            (command, _hatch_script_selector(command, configuration))
            for command in invocations
        )
        declaration = "declared"
    elif configuration:
        selections = tuple(
            (
                ("hatch", "run", f"{environment.name}:{script}"),
                (environment.name, script),
            )
            for environment in configuration.environments.values()
            for script in environment.scripts
            if _likely_validation_name(script)
        )
        declaration = "inferred"
    else:
        return ()
    config_files = configuration.config_files if configuration else ()
    workflow_paths = tuple(
        path
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if re.search(r"\bhatch\b", _read_text(path))
    )
    profiles: list[DiscoveredValidationProfile] = []
    for index, (command, selectors) in enumerate(selections, start=1):
        unresolved = list(configuration.unresolved if configuration else ())
        environment_name, script_name = selectors if len(selectors) == 2 else ("", "")
        script_commands: tuple[str, ...] = ()
        environment: object = None
        if configuration and selectors:
            environment = configuration.environments.get(environment_name)
            script_commands, diagnostics = hatch_script_closure(
                configuration, environment_name, script_name
            )
            unresolved.extend(diagnostics)
            if environment is None:
                unresolved.append(
                    f"Hatch environment {environment_name!r} is not statically "
                    "declared."
                )
        elif invocations:
            unresolved.append(
                "Hatch CI command selects an environment script, but no Hatch "
                "configuration was found to explain it."
                if selectors
                else "Hatch CI command does not select a statically declared "
                "environment script."
            )
        if len(selectors) == 2:
            selected_token = f"{environment_name}:{script_name}"
            hatch_index = next(
                (i for i, token in enumerate(command) if Path(token).name == "hatch"),
                -1,
            )
            selected_index = next(
                (i for i, token in enumerate(command) if token == selected_token),
                len(command) - 1,
            )
            if selected_index + 1 < len(command):
                unresolved.append(
                    "Arguments after the Hatch script selector may supply script "
                    "context and remain unresolved."
                )
            if hatch_index >= 0 and selected_index <= hatch_index:
                unresolved.append(
                    "Could not locate the Hatch script selector in its command."
                )
        if environment is not None:
            environment_unresolved = getattr(environment, "unresolved", ())
            unresolved.extend(environment_unresolved)
        declared_script = bool(
            environment and script_name in getattr(environment, "scripts", {})
        )
        known_validation = (
            declared_script
            and len(selectors) == 2
            and _likely_validation_name(script_name)
        )
        settings: dict[str, object] = {
            "environment": environment_name or None,
            "script": script_name or None,
            "commands": list(script_commands),
            "environment_settings": (
                dict(getattr(environment, "settings", {})) if environment else {}
            ),
        }
        target_name = f"{environment_name}-{script_name}" if selectors else "run"
        evidence_paths = [*config_files]
        evidence_paths.extend(
            path.relative_to(root).as_posix() for path in workflow_paths
        )
        profiles.append(
            DiscoveredValidationProfile(
                name=f"hatch-{target_name}"
                if len(selections) == 1
                else f"hatch-{index}-{target_name}",
                command=command,
                source="Hatch environment/GitHub Actions",
                provider_name="aggregate" if known_validation else "custom",
                purpose=(
                    f"Run Hatch environment script {environment_name}:{script_name}."
                    if selectors
                    else "Run the native Hatch command recorded by CI."
                ),
                roles=("validation",) if known_validation else (),
                selectors=selectors,
                config_files=config_files,
                settings=settings,
                declaration=declaration,
                evidence=tuple(
                    f"validation-input:{path}" for path in dict.fromkeys(evidence_paths)
                ),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _is_hatch_run(command: Sequence[str]) -> bool:
    hatch_index = next(
        (index for index, token in enumerate(command) if Path(token).name == "hatch"),
        None,
    )
    if hatch_index is None:
        return False
    tail = command[hatch_index + 1 :]
    return "run" in tail


def _hatch_script_selector(
    command: Sequence[str], configuration: HatchConfiguration | None
) -> tuple[str, ...]:
    hatch_index = next(
        (index for index, token in enumerate(command) if Path(token).name == "hatch"),
        None,
    )
    if hatch_index is None:
        return ()
    tail = command[hatch_index + 1 :]
    try:
        run_index = tail.index("run")
    except ValueError:
        return ()
    selected_environment: str | None = None
    skip_next_environment = False
    for token in tail[:run_index]:
        if skip_next_environment:
            selected_environment = token
            skip_next_environment = False
        elif token in {"-e", "--env"}:
            skip_next_environment = True
        elif token.startswith("--env="):
            selected_environment = token.split("=", 1)[1]
    skip_next_option: str | None = None
    for token in tail[run_index + 1 :]:
        if skip_next_option:
            if skip_next_option == "environment":
                selected_environment = token
            skip_next_option = None
            continue
        if token in {"-e", "--env"}:
            skip_next_option = "environment"
            continue
        if token in {"-i", "--include", "-x", "--exclude"}:
            skip_next_option = "matrix"
            continue
        if token.startswith("--env="):
            selected_environment = token.split("=", 1)[1]
            continue
        if token.startswith(("+", "-")):
            continue
        if "=" in token:
            continue
        if ":" in token:
            environment, script = token.split(":", 1)
            return (environment, script) if environment and script else ()
        environment = selected_environment or "default"
        if (
            configuration is not None
            and environment in configuration.environments
            and token in configuration.environments[environment].scripts
        ):
            return environment, token
        return ()
    return ()


def _likely_validation_name(name: str) -> bool:
    return name in {
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
    } or name.startswith(("check-", "lint-", "test-", "typecheck-", "validate-"))


def _task_runner_selectors(command: Sequence[str], runner: str) -> tuple[str, ...]:
    executable_index = next(
        (index for index, token in enumerate(command) if Path(token).name == runner),
        None,
    )
    if executable_index is None:
        return ()
    value_options = (
        {"--justfile", "-f", "--working-directory", "-d"}
        if runner == "just"
        else {"--taskfile", "-t", "--dir", "-d"}
    )
    skip_next = False
    for token in command[executable_index + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if token in value_options:
            skip_next = True
            continue
        if token.startswith("-") or "=" in token:
            continue
        return (token,)
    return ()


def _task_runner_config_argument(command: Sequence[str], runner: str) -> str | None:
    executable_index = next(
        (index for index, token in enumerate(command) if Path(token).name == runner),
        None,
    )
    if executable_index is None:
        return None
    options = {"--justfile", "-f"} if runner == "just" else {"--taskfile", "-t"}
    skip_next = False
    for token in command[executable_index + 1 : -1]:
        if skip_next:
            return token
        if token in options:
            skip_next = True
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _discover_pre_commit_profiles(
    root: Path, ci_commands: Sequence[tuple[str, ...]]
) -> tuple[DiscoveredValidationProfile, ...]:
    invocations = tuple(
        command
        for command in ci_commands
        if any(Path(token).name == "pre-commit" for token in command)
    )
    if not invocations:
        return ()
    profiles: list[DiscoveredValidationProfile] = []
    workflow_paths = tuple(
        path
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if _workflow_mentions(path, "pre-commit")
    )
    for index, command in enumerate(invocations, start=1):
        config = discover_pre_commit_configuration(
            root, _pre_commit_config_argument(command)
        )
        config_file = config.config_file if config else None
        hook_ids = {hook.id for hook in config.hooks} if config else set()
        selectors = _pre_commit_hook_selectors(command, hook_ids)
        selected_hooks = tuple(
            hook
            for hook in (config.hooks if config else ())
            if not selectors or hook.id in selectors
        )
        settings: dict[str, object] = {
            "defaults": dict(config.settings) if config else {},
            "hooks": [
                {
                    "id": hook.id,
                    "name": hook.name,
                    "repo": hook.repo,
                    "rev": hook.rev,
                    "entry": hook.entry,
                    "language": hook.language,
                    "args": list(hook.args),
                    "files": hook.files,
                    "exclude": hook.exclude,
                    "types": list(hook.types),
                    "stages": list(hook.stages),
                    "additional_dependencies": list(hook.additional_dependencies),
                    "pass_filenames": hook.pass_filenames,
                    "always_run": hook.always_run,
                    "require_serial": hook.require_serial,
                }
                for hook in selected_hooks
            ],
        }
        unresolved = list(config.unresolved if config else ())
        unknown_selectors = sorted(set(selectors) - hook_ids) if config else []
        if not config:
            unresolved.append(
                "pre-commit is invoked by CI, but no supported pre-commit config "
                "was found."
            )
        if unknown_selectors:
            unresolved.append(
                "CI selects hook ids absent from the discovered configuration: "
                + ", ".join(unknown_selectors)
                + "."
            )
        if not selectors:
            unresolved.append(
                "The invocation does not select a hook id; hook selection follows "
                "the installed pre-commit version, configured stages, files, "
                "and options."
            )
        evidence_paths = [config_file] if config_file else []
        evidence_paths.extend(
            path.relative_to(root).as_posix() for path in workflow_paths
        )
        profiles.append(
            DiscoveredValidationProfile(
                name=(
                    f"pre-commit-{index}-{','.join(selectors)}"
                    if selectors and len(invocations) > 1
                    else f"pre-commit-{','.join(selectors)}"
                    if selectors
                    else f"pre-commit-{index}"
                ),
                command=command,
                source="pre-commit configuration/GitHub Actions",
                provider_name="aggregate",
                purpose="Run configured pre-commit hooks selected by CI.",
                roles=("validation",),
                selectors=selectors,
                config_files=(config_file,) if config_file else (),
                settings=settings,
                declaration="declared",
                evidence=tuple(f"validation-input:{path}" for path in evidence_paths),
                unresolved=tuple(dict.fromkeys(unresolved)),
            )
        )
    return tuple(profiles)


def _pre_commit_hook_selectors(
    command: Sequence[str], known_hook_ids: set[str]
) -> tuple[str, ...]:
    executable_index = next(
        (
            index
            for index, token in enumerate(command)
            if Path(token).name == "pre-commit"
        ),
        None,
    )
    if executable_index is None:
        return ()
    tail = command[executable_index + 1 :]
    if not tail or tail[0] != "run":
        return ()
    selectors: list[str] = []
    skip_next = False
    skip_files = False
    for token in tail[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in {"--config", "-c", "--hook-stage", "--color"}:
            skip_next = True
            continue
        if token == "--files":
            skip_files = True
            continue
        if token.startswith("-"):
            skip_files = False
            continue
        if skip_files:
            continue
        if "=" in token or token.startswith((".", "/")):
            continue
        if token in known_hook_ids or not selectors:
            selectors.append(token)
            if token not in known_hook_ids:
                break
    return tuple(selectors)


def _pre_commit_config_argument(command: Sequence[str]) -> str | None:
    executable_index = next(
        (
            index
            for index, token in enumerate(command)
            if Path(token).name == "pre-commit"
        ),
        None,
    )
    if executable_index is None:
        return None
    for index, token in enumerate(command[executable_index + 1 : -1]):
        if token in {"--config", "-c"}:
            return command[executable_index + index + 2]
    return None


def _pre_commit_child_tools(
    profiles: Sequence[DiscoveredValidationProfile],
) -> set[str]:
    tools = {"pytest", "ruff", "mypy", "black", "flake8", "pyright"}
    found: set[str] = set()
    for profile in profiles:
        if not profile.name.startswith("pre-commit-") or not profile.settings:
            continue
        hooks = profile.settings.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            entry = hook.get("entry")
            if not isinstance(entry, str):
                continue
            try:
                tokens = shlex.split(entry, posix=True)
            except ValueError:
                continue
            found.update(tools.intersection(tokens))
            for index, token in enumerate(tokens[:-1]):
                if token == "-m" and tokens[index + 1] in tools:
                    found.add(tokens[index + 1])
    return found


def _workflow_mentions(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


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


def _package_manager(root: Path, package_json: dict[str, object]) -> str:
    declared = package_json.get("packageManager")
    if isinstance(declared, str):
        manager = declared.split("@", 1)[0]
        if manager in {"npm", "pnpm", "yarn", "bun"}:
            return manager
    for manager, lockfile in (
        ("pnpm", "pnpm-lock.yaml"),
        ("yarn", "yarn.lock"),
        ("bun", "bun.lockb"),
        ("npm", "package-lock.json"),
    ):
        if (root / lockfile).is_file():
            return manager
    return "npm"


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


def _github_validation_origins(root: Path) -> tuple[dict[str, object], ...]:
    """Read GitHub Actions run steps with their declared execution context."""
    origins: list[dict[str, object]] = []
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        try:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        triggers = data.get("on", {})
        workflow_defaults = _nested_mapping(data, "defaults", "run")
        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            job_defaults = _nested_mapping(job, "defaults", "run") or workflow_defaults
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                continue
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                    continue
                origins.append(
                    {
                        "workflow": path.relative_to(root).as_posix(),
                        "workflow_name": str(data.get("name", path.stem)),
                        "triggers": triggers,
                        "job_id": str(job_id),
                        "job_name": str(job.get("name", job_id)),
                        "job_if": job.get("if"),
                        "needs": job.get("needs", []),
                        "matrix": _nested_mapping(job, "strategy", "matrix"),
                        "step_index": index,
                        "step_name": str(step.get("name", f"step {index + 1}")),
                        "step_if": step.get("if"),
                        "run": step["run"],
                        "shell": step.get("shell", job_defaults.get("shell")),
                        "working_directory": step.get(
                            "working-directory",
                            job_defaults.get("working-directory", "."),
                        ),
                        "evidence": "GitHub Actions workflow jobs[].steps[].run",
                    }
                )
    return tuple(origins)


def _nested_mapping(value: dict[str, object], *keys: str) -> dict[str, object]:
    current: object = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def _commands_from_script(script: str) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    for line in _logical_script_lines(script):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "if ", "then", "fi", "set ")):
            continue
        for segment in re.split(r"\s*(?:&&|\|\||;)\s*", stripped):
            try:
                tokens = tuple(shlex.split(segment, posix=True))
            except ValueError:
                continue
            if any(token == "`" for token in tokens):
                continue
            if (
                tokens
                and tokens[0]
                in {
                    "uv",
                    "python",
                    "python3",
                    "pytest",
                    "ruff",
                    "mypy",
                    "tox",
                    "nox",
                    "make",
                    "gmake",
                    "just",
                    "task",
                    "pdm",
                    "pipenv",
                    "poe",
                    "poetry",
                    "npm",
                    "pnpm",
                    "yarn",
                    "bun",
                    "pre-commit",
                    "hatch",
                    "stestr",
                    "go",
                    "cargo",
                }
                or (tokens and re.fullmatch(r"(?:\./)?scripts/[\w.-]+", tokens[0]))
            ):
                commands.append(tokens)
    return tuple(commands)


def _logical_script_lines(script: str) -> tuple[str, ...]:
    lines: list[str] = []
    pending = ""
    for physical_line in script.splitlines():
        line = physical_line.rstrip()
        if line.endswith(("\\", "`")):
            pending += line[:-1] + " "
            continue
        lines.append(pending + line)
        pending = ""
    if pending:
        lines.append(pending)
    return tuple(lines)


def _commands_with_working_directory(
    script: str, working_directory: str
) -> tuple[tuple[tuple[str, ...], str], ...]:
    """Extract supported commands while honoring simple preceding ``cd`` lines."""
    current_directory = working_directory or "."
    commands: list[tuple[tuple[str, ...], str]] = []
    for line in _logical_script_lines(script):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "if ", "then", "fi", "set ")):
            continue
        for segment in re.split(r"\s*(?:&&|\|\||;)\s*", stripped):
            try:
                tokens = tuple(shlex.split(segment, posix=True))
            except ValueError:
                continue
            if tokens and tokens[0] == "cd":
                if len(tokens) == 2:
                    destination = tokens[1]
                    if (
                        not destination.startswith(("/", "~", "$"))
                        and "$" not in destination
                    ):
                        current_directory = os.path.normpath(
                            os.path.join(current_directory, destination)
                        ).replace(os.sep, "/")
                continue
            for command in _commands_from_script(segment):
                commands.append((command, current_directory))
    return tuple(commands)


def _profile_markers(profile: DiscoveredValidationProfile) -> tuple[str, ...]:
    command = tuple(part.removeprefix("uv") for part in profile.command)
    if profile.provider == "unittest":
        return (
            ("runtests.py",)
            if any("runtests.py" in item for item in command)
            else ("unittest",)
        )
    if profile.provider == "custom":
        return tuple(
            marker
            for marker in (
                "validate-procedrr-processes",
                "workflow-scenario-suite",
                "check_migrations.py",
                "scripts/check",
                "scripts/test",
                "run-deterministic-workflow-suite.sh",
            )
            if any(marker in item for item in command)
        )
    for marker in (
        ("ruff", "format"),
        ("ruff", "check"),
        ("mypy",),
        ("pytest",),
        ("tox",),
        ("nox",),
        ("pre-commit",),
        ("hatch",),
        ("pdm",),
        ("pipenv",),
        ("make",),
        ("just",),
        ("task",),
        ("poe",),
        ("npm", "test"),
        ("pnpm", "test"),
        ("go", "test"),
        ("cargo", "test"),
    ):
        if _contains_marker(command, marker):
            return marker
    if command[:1] in {("python",), ("python3",)}:
        return ()
    return command[:1]


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
    if len(marker) == 1 and "/" not in marker[0]:
        return any(Path(token).name == marker[0] for token in command)
    return False


__all__ = [
    "DiscoveredValidationProfile",
    "VALIDATION_PROVIDER_INVENTORY_SCHEMA_VERSION",
    "discover_validation_profiles",
    "validation_inventory",
]
