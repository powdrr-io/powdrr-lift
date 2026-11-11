#!/usr/bin/env python3
"""Build and inspect isolated Powdrr agent and contracts distributions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

_FORBIDDEN_PACKAGE_NAMES = {"powdrr_service", "powdrr_local", "powdrr_lift"}
_FORBIDDEN_AGENT_RESOURCES = {
    "fixtures",
    "processes",
    "prompts",
    "providers",
    "resources",
    "rubrics",
}


def _run(*command: str) -> None:
    subprocess.run(command, check=True)


def _distribution_files(archive: Path) -> tuple[str, ...]:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as wheel:
            return tuple(wheel.namelist())
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as source:
            return tuple(source.getnames())
    raise ValueError(f"Unsupported distribution archive: {archive}")


def _check_archive_contents(
    archive: Path,
    distribution: str,
    *,
    expected_agent_sources: set[str],
) -> None:
    paths = _distribution_files(archive)
    forbidden = sorted(
        name
        for name in paths
        if any(segment in _FORBIDDEN_PACKAGE_NAMES for segment in Path(name).parts)
    )
    if forbidden:
        raise AssertionError(f"{archive.name} contains private packages: {forbidden}")
    package_root = distribution.replace("-", "_")
    if not any(package_root in Path(name).parts for name in paths):
        raise AssertionError(f"{archive.name} does not contain {package_root}")
    if distribution == "powdrr-agent":
        agent_sources = set()
        for name in paths:
            parts = PurePosixPath(name).parts
            try:
                package_index = parts.index("powdrr_agent")
            except ValueError:
                continue
            relative = PurePosixPath(*parts[package_index:])
            if any(
                part.lower() in _FORBIDDEN_AGENT_RESOURCES for part in relative.parts
            ):
                raise AssertionError(
                    f"{archive.name} contains a private agent resource: {name}"
                )
            if relative.suffix == ".py" or relative.name == "py.typed":
                agent_sources.add(relative.as_posix())
        if agent_sources != expected_agent_sources:
            raise AssertionError(
                f"{archive.name} agent source files differ from the "
                "ownership inventory; "
                f"extra={sorted(agent_sources - expected_agent_sources)}, "
                f"missing={sorted(expected_agent_sources - agent_sources)}"
            )


def _check_agent_metadata(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    requirements = [
        line.partition(":")[2].strip().lower().replace("_", "-")
        for line in metadata.splitlines()
        if line.lower().startswith("requires-dist:")
    ]
    if set(requirements) != {"powdrr-contracts", "mini-swe-agent==2.4.6"}:
        raise AssertionError(
            "The agent wheel must depend only on public contracts and the "
            "local MiniSWE runtime; "
            f"found dependencies {requirements}."
        )


def _install_and_import(wheels: Path, temporary_root: Path) -> None:
    environment = temporary_root / "agent-env"
    _run("uv", "venv", "--python", sys.executable, str(environment))
    python = environment / "bin" / "python"
    if os.name == "nt":
        python = environment / "Scripts" / "python.exe"
    agent_wheel = next(wheels.glob("powdrr_agent-*.whl"))
    contracts_wheel = next(wheels.glob("powdrr_contracts-*.whl"))
    _run(
        "uv",
        "pip",
        "install",
        "--python",
        str(python),
        str(contracts_wheel),
        str(agent_wheel),
    )
    smoke = (
        "import importlib, importlib.util, pkgutil, powdrr_agent, powdrr_contracts; "
        "[importlib.import_module(item.name) "
        "for item in pkgutil.walk_packages(powdrr_agent.__path__, 'powdrr_agent.')]; "
        "[importlib.import_module(f'powdrr_contracts.{item.name}') "
        "for item in pkgutil.iter_modules(powdrr_contracts.__path__)]; "
        "assert all(importlib.util.find_spec(name) is None for name in "
        "('powdrr_service', 'powdrr_local', 'powdrr_lift'))"
    )
    _run(str(python), "-I", "-c", smoke)
    _run(
        str(python),
        "-I",
        "-m",
        "powdrr_agent.execution.minisweagent_session",
        "--help",
    )
    _run(str(python), "-I", "-m", "powdrr_agent.execution.source_reader", "--help")


def _expected_agent_sources(repository: Path) -> set[str]:
    inventory_path = (
        repository / "docs/architecture/agent-service-module-ownership.json"
    )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    expected = {
        PurePosixPath(entry["path"]).relative_to("packages/agent/src").as_posix()
        for entry in inventory["entries"]
        if entry.get("owner") == "agent"
        and entry.get("path", "").startswith("packages/agent/src/")
    }
    expected.update({"powdrr_agent/__init__.py", "powdrr_agent/py.typed"})
    return expected


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to build and install the distributions.")
    with tempfile.TemporaryDirectory(prefix="powdrr-agent-distribution-") as temp:
        root = Path(temp)
        repository = Path(__file__).resolve().parents[1]
        expected_agent_sources = _expected_agent_sources(repository)
        for package in ("powdrr-contracts", "powdrr-agent"):
            _run(uv, "build", "--package", package, "--out-dir", str(root))
        archives = tuple(root.glob("powdrr_*"))
        for archive in archives:
            distribution = (
                "powdrr-agent"
                if archive.name.startswith("powdrr_agent-")
                else "powdrr-contracts"
            )
            _check_archive_contents(
                archive,
                distribution,
                expected_agent_sources=expected_agent_sources,
            )
        _check_agent_metadata(next(root.glob("powdrr_agent-*.whl")))
        _install_and_import(root, root)
    print("Agent and contracts wheel/sdist isolation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
