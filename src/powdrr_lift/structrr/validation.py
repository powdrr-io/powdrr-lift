"""Discover repository validation commands before declaring a feature complete."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DiscoveredValidationProfile:
    """A validation command inferred from repository configuration."""

    name: str
    command: tuple[str, ...]
    source: str


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
        if _contains_marker(normalized, marker):
            return command
    return None


def _contains_marker(command: tuple[str, ...], marker: tuple[str, ...]) -> bool:
    for index in range(len(command) - len(marker) + 1):
        if command[index : index + len(marker)] == marker:
            return True
    return False


__all__ = ["DiscoveredValidationProfile", "discover_validation_profiles"]
