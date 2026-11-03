"""Discover Python project boundaries and declared development environments."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "requirements.txt",
)
_ENVIRONMENT_FILES = (
    "environment.yml",
    "environment.yaml",
    "conda-lock.yml",
    "conda-lock.yaml",
)
_IGNORED_COMPONENT_PARTS = {
    ".git",
    ".venv",
    "venv",
    "vendor",
    "node_modules",
    "tests",
    "test",
    "fixtures",
    "workflow-evals",
}
_EVIDENCE_FILENAMES = {
    "Makefile",
    "Justfile",
    "justfile",
    "tox.ini",
    "tox.toml",
    "noxfile.py",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    ".python-version",
    ".tool-versions",
    "runtime.txt",
}


@dataclass(frozen=True, slots=True)
class PythonTopology:
    """Static component, environment, and declaration evidence for a repo."""

    components: tuple[dict[str, Any], ...]
    environments: tuple[dict[str, Any], ...]
    input_fingerprints: tuple[dict[str, str], ...]
    diagnostics: tuple[dict[str, str], ...]
    evidence_files: tuple[str, ...]


def is_validation_evidence_path(relative: str) -> bool:
    """Return whether a tracked path can declare validation setup or context."""
    path = Path(relative)
    name = path.name
    parts = set(path.parts)
    return (
        relative.startswith(".github/workflows/")
        or relative.startswith(".github/actions/")
        or name in _PROJECT_MANIFESTS
        or name in _ENVIRONMENT_FILES
        or name in _EVIDENCE_FILENAMES
        or name.startswith(("requirements-", "requirements."))
        or name in {"poetry.lock", "uv.lock", "pdm.lock", "Pipfile.lock"}
        or name in {"hatch.toml", "pyrightconfig.json", "mypy.ini", ".flake8"}
        or path.suffix.lower() in {".py", ".sh", ".bash", ".ps1"}
        and bool(parts & {"scripts", "tools", "ci"})
    )


def discover_python_topology(
    root: str | Path, tracked_paths: Sequence[str]
) -> PythonTopology:
    """Build component/environment records from manifests and Python files.

    This discovery does not execute setup.py, parse Python imports, or infer a
    package purpose from its name. Test fixtures and vendored projects are
    excluded from production component classification.
    """
    root_path = Path(root).resolve()
    paths = tuple(sorted(set(_normalize(path) for path in tracked_paths)))
    eligible = tuple(path for path in paths if not _is_ignored_component_path(path))
    roots = _component_roots(eligible)
    diagnostics: list[dict[str, str]] = []
    components: list[dict[str, Any]] = []
    environments: list[dict[str, Any]] = []
    fingerprints: list[dict[str, str]] = []
    evidence_files: set[str] = set()

    for relative_root in sorted(roots):
        manifests = tuple(
            path
            for path in eligible
            if _parent(path) == relative_root
            and Path(path).name in _PROJECT_MANIFESTS + _ENVIRONMENT_FILES
        )
        python_files = tuple(
            path
            for path in eligible
            if Path(path).suffix.lower() in {".py", ".pyi"}
            and _is_within(path, relative_root)
            and _nearest_component_root(path, roots) == relative_root
        )
        pyproject_path = _join(relative_root, "pyproject.toml")
        pyproject, parse_error = _read_pyproject(root_path / pyproject_path)
        if parse_error:
            diagnostics.append(
                {
                    "code": "python_project_config_invalid",
                    "path": pyproject_path,
                    "message": parse_error,
                }
            )
        package_name = _project_name(pyproject)
        component_id = f"component:python:{relative_root or '.'}"
        components.append(
            {
                "id": component_id,
                "language": "python",
                "path": relative_root or ".",
                "package_name": package_name,
                "manifests": list(manifests),
                "python_file_count": len(python_files),
                "classification": "declared" if manifests else "inferred",
            }
        )
        component_evidence = set(manifests)
        if pyproject_path in eligible:
            component_evidence.add(pyproject_path)
        env, env_diagnostics = _environment_record(
            root_path, relative_root, component_id, pyproject
        )
        diagnostics.extend(env_diagnostics)
        environments.append(env)
        component_evidence.update(env["lockfiles"])
        component_evidence.update(env["configuration_files"])
        evidence_files.update(component_evidence)
        for path in sorted(component_evidence):
            fingerprint = _fingerprint(root_path, path, diagnostics)
            if fingerprint:
                fingerprints.append(fingerprint)

    for path in paths:
        if is_validation_evidence_path(path):
            evidence_files.add(path)
            fingerprint = _fingerprint(root_path, path, diagnostics)
            if fingerprint:
                fingerprints.append(fingerprint)

    fingerprint_by_path = {item["path"]: item for item in fingerprints}
    return PythonTopology(
        components=tuple(components),
        environments=tuple(environments),
        input_fingerprints=tuple(
            fingerprint_by_path[path] for path in sorted(fingerprint_by_path)
        ),
        diagnostics=tuple(_deduplicate_diagnostics(diagnostics)),
        evidence_files=tuple(sorted(evidence_files)),
    )


def _component_roots(paths: Sequence[str]) -> set[str]:
    roots: set[str] = set()
    for path in paths:
        name = Path(path).name
        if name in _PROJECT_MANIFESTS:
            roots.add(_parent(path))
        elif Path(path).suffix.lower() in {".py", ".pyi"}:
            # Repositories with scripts/tests but no package manifest still
            # receive a repository-level Python component.
            roots.add("")
    return roots


def _environment_record(
    root: Path,
    relative_root: str,
    component_id: str,
    pyproject: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    base = root / relative_root
    managers: set[str] = set()
    lockfiles: set[str] = set()
    config_files: set[str] = set()
    checks = (
        ("uv", "uv.lock", "tool.uv"),
        ("poetry", "poetry.lock", "tool.poetry"),
        ("pdm", "pdm.lock", "tool.pdm"),
        ("pipenv", "Pipfile.lock", "Pipfile"),
        ("hatch", "hatch.toml", "tool.hatch"),
    )
    for manager, lockfile, config_key in checks:
        if (base / lockfile).is_file():
            managers.add(manager)
            lockfiles.add(_join(relative_root, lockfile))
        if _nested_mapping(pyproject, config_key.split(".")):
            managers.add(manager)
            config_files.add(_join(relative_root, "pyproject.toml"))
        if manager == "pipenv" and (base / "Pipfile").is_file():
            managers.add(manager)
            config_files.add(_join(relative_root, "Pipfile"))
        if manager == "hatch" and (base / "hatch.toml").is_file():
            managers.add(manager)
            config_files.add(_join(relative_root, "hatch.toml"))

    requirement_files = sorted(
        path.name for path in base.glob("requirements*.txt") if path.is_file()
    )
    if requirement_files and not managers:
        managers.add("pip")
    lockfiles.update(
        _join(relative_root, name)
        for name in requirement_files
        if name in {"requirements.lock", "requirements-dev.lock"}
    )
    for name in (
        "requirements.txt",
        "environment.yml",
        "environment.yaml",
        "conda-lock.yml",
        "conda-lock.yaml",
    ):
        if (base / name).is_file():
            config_files.add(_join(relative_root, name))
            if name.startswith("environment") or name.startswith("conda-lock"):
                managers.add("conda")
    if (base / ".python-version").is_file():
        config_files.add(_join(relative_root, ".python-version"))
    if (base / ".tool-versions").is_file():
        config_files.add(_join(relative_root, ".tool-versions"))

    requires_python = _nested_value(pyproject, ("project", "requires-python"))
    python_version = _read_first_line(base / ".python-version")
    manager = (
        next(iter(managers))
        if len(managers) == 1
        else "multiple"
        if managers
        else "unknown"
    )
    record = {
        "id": f"environment:python:{relative_root or '.'}",
        "component": component_id,
        "manager": manager,
        "detected_managers": sorted(managers),
        "python": {
            "requires_python": requires_python
            if isinstance(requires_python, str)
            else None,
            "version_file": python_version,
        },
        "lockfiles": sorted(lockfiles),
        "configuration_files": sorted(config_files),
        "selected_groups": [],
        "installation_mode": "unknown",
        "status": "declared" if managers else "unknown",
    }
    return record, []


def _read_pyproject(path: Path) -> tuple[Mapping[str, Any], str | None]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, tomllib.TOMLDecodeError) as error:
        return {}, f"Could not parse Python project metadata: {error}"
    return (
        (value, None)
        if isinstance(value, Mapping)
        else ({}, "pyproject.toml root must be a table")
    )


def _project_name(value: Mapping[str, Any]) -> str | None:
    for key_path in (("project", "name"), ("tool", "poetry", "name")):
        candidate = _nested_value(value, key_path)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _nested_mapping(value: Mapping[str, Any], keys: Sequence[str]) -> Mapping[str, Any]:
    current: object = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _nested_value(value: Mapping[str, Any], keys: Sequence[str]) -> object:
    current: object = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _fingerprint(
    root: Path, relative: str, diagnostics: list[dict[str, str]]
) -> dict[str, str] | None:
    try:
        content = (root / relative).read_bytes()
    except OSError as error:
        diagnostics.append(
            {
                "code": "validation_evidence_read_failed",
                "path": relative,
                "message": str(error),
            }
        )
        return None
    return {
        "id": f"input:{relative}",
        "path": relative,
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


def _is_ignored_component_path(relative: str) -> bool:
    return bool(set(Path(relative).parts) & _IGNORED_COMPONENT_PARTS)


def _is_within(path: str, relative_root: str) -> bool:
    return (
        not relative_root
        or path == relative_root
        or path.startswith(relative_root + "/")
    )


def _nearest_component_root(path: str, roots: set[str]) -> str:
    candidates = [candidate for candidate in roots if _is_within(path, candidate)]
    return max(candidates, key=len) if candidates else ""


def _parent(path: str) -> str:
    parent = Path(path).parent.as_posix()
    return "" if parent == "." else parent


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _normalize(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _read_first_line(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return value[0].strip() if value and value[0].strip() else None


def _deduplicate_diagnostics(
    diagnostics: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    return list(
        {
            (item.get("code", ""), item.get("path", ""), item.get("message", "")): item
            for item in diagnostics
        }.values()
    )


__all__ = ["PythonTopology", "discover_python_topology", "is_validation_evidence_path"]
