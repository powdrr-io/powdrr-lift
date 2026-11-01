"""Local subprocess, filesystem, and Git helpers for execute-PR tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from powdrr_lift.workrr.coding_agent_validation import ValidationProfile


class ExecutePrTestError(ValueError):
    """Generated execute-PR tests are invalid or did not prove behavior."""


def pytest_command_for_files(
    profile: ValidationProfile,
    paths: Sequence[str],
) -> tuple[str, ...]:
    """Select a pytest runner for frozen tests and append their paths.

    Prefer the repository's declared verification command when it is pytest.
    Otherwise, use Powdrr's Python environment to run the isolated generated
    contract tests; the repository verification command is run separately.
    """
    command = profile.command
    pytest_present = any(
        Path(token).name == "pytest"
        or (
            token == "-m"
            and index + 1 < len(command)
            and command[index + 1] == "pytest"
        )
        for index, token in enumerate(command)
    )
    if not pytest_present:
        command = (sys.executable, "-m", "pytest")
    return (*command, "--tb=short", *paths)


def run_generated_pytest(
    profile: ValidationProfile,
    *,
    root: Path,
    paths: Sequence[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run only the frozen generated tests, independent of project validation."""
    command = pytest_command_for_files(profile, paths)
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath = [str(root), str(root / "src")]
    for raw_path in paths:
        relative_path = PurePosixPath(raw_path)
        for index, part in enumerate(relative_path.parts[:-1]):
            if part not in {"test", "tests"}:
                continue
            package_root = root.joinpath(*relative_path.parts[:index])
            nested_src = package_root / "src"
            if nested_src.is_dir():
                pythonpath.append(str(nested_src))
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath))
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "passed": False,
            "command": list(command),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": str(error),
        }
    return {
        "passed": completed.returncode == 0,
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "error": None,
    }


def fingerprint_test_files(files: Mapping[str, str]) -> dict[str, str]:
    """Fingerprint frozen generated tests before coding starts."""
    return {
        path: hashlib.sha256(source.encode("utf-8")).hexdigest()
        for path, source in sorted(files.items())
    }


def candidate_test_changes(root: Path, expected: Mapping[str, str]) -> tuple[str, ...]:
    """Report missing or edited frozen test files in the implementation tree."""
    changed: list[str] = []
    for path, source in expected.items():
        candidate = root / path
        if not candidate.is_file() or candidate.read_text(encoding="utf-8") != source:
            changed.append(path)
    return tuple(sorted(changed))


def install_frozen_test_files(root: Path, files: Mapping[str, str]) -> None:
    """Install and stage generated tests so the coding agent can run them."""
    for path, source in files.items():
        candidate = PurePosixPath(path)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.parts
            or not any(part in {"tests", "test"} for part in candidate.parts[:-1])
            or candidate.suffix != ".py"
        ):
            raise ExecutePrTestError(f"invalid frozen integration-test path: {path}")
        destination = root.joinpath(*candidate.parts)
        if destination.is_symlink():
            destination.unlink()
        try:
            destination.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise ExecutePrTestError(
                f"generated integration test escaped the worktree: {path}"
            ) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
    if not files:
        raise ExecutePrTestError("cannot install an empty integration-test suite")
    staged = subprocess.run(
        ["git", "add", "-f", "--", *sorted(files)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode:
        raise ExecutePrTestError(
            "could not stage the frozen integration tests: " + staged.stderr.strip()
        )


__all__ = [
    "ExecutePrTestError",
    "candidate_test_changes",
    "pytest_command_for_files",
    "run_generated_pytest",
    "fingerprint_test_files",
    "install_frozen_test_files",
]
