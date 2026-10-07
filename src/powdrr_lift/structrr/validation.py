"""Discover repository validation commands before declaring a feature complete."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from powdrr_lift.structrr.validation_models import (
    VALIDATION_INVENTORY_SCHEMA_VERSION,
    ValidationCheck,
    ValidationContext,
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
) -> dict[str, object]:
    """Describe discovery coverage without claiming the search is exhaustive."""
    return ValidationContext(
        discovery_status="partial",
        coverage=("currently implemented validation detectors",),
        diagnostics=(
            (
                {
                    "code": "discovery_scope_limited",
                    "message": (
                        "Validation discovery is limited to the detectors currently "
                        "implemented; absence of a check is not proof that none exists."
                    ),
                },
            )
            if not profiles
            else ()
        ),
    ).to_data()


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

    if _mentions_tool("ruff", pyproject, text):
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
    if _mentions_tool("mypy", pyproject, text):
        profiles.append(
            _profile(
                "mypy",
                _find_command(ci_commands, ("mypy",))
                or (*uv_prefix, "mypy", "src", "tests"),
                "mypy configuration/CI",
            )
        )
    if _mentions_tool("pytest", pyproject, text) or (root_path / "tests").is_dir():
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
                commands.append(tuple(match.group(1).strip().split()))
            else:
                stripped = line.strip()
                if stripped.startswith(
                    ("uv run ", "python -m ", "pytest ", "ruff ", "mypy ")
                ):
                    commands.append(tuple(stripped.split()))
    return tuple(commands)


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
