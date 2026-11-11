"""Collect real pytest node IDs in an isolated process through its public hooks."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_agent.execution.execute_pr_tests import (
    ValidationProfile,
    pytest_command_for_files,
)

_RUNTIME_CODE = """
def _runtime_snapshot():
    import hashlib
    import importlib.metadata
    import json
    import platform
    import sys
    import pytest
    packages = sorted((dist.metadata.get("Name", ""), dist.version)
                      for dist in importlib.metadata.distributions())
    return {"python": sys.version, "platform": sys.platform,
            "machine": platform.machine(), "pytest": pytest.__version__,
            "packages_digest": "sha256:" + hashlib.sha256(
                json.dumps(packages, separators=(",", ":")).encode()).hexdigest()}
"""

_PLUGIN = (
    _RUNTIME_CODE
    + """
import json
import os
from pathlib import Path

def pytest_collection_finish(session):
    root = Path(os.environ["POWDRR_STORY_COLLECTION_ROOT"])
    selectors = [
        str(Path(item.path).relative_to(root)).replace(os.sep, "/")
        + "::" + item.nodeid.split("::", 1)[1]
        for item in session.items
    ]
    Path(os.environ["POWDRR_STORY_COLLECTION_REPORT"]).write_text(json.dumps({
        "selectors": selectors,
        "collection_errors": session.testsfailed,
        "runtime": _runtime_snapshot(),
    }))
"""
)


def collect_story_tests(
    profile: ValidationProfile,
    *,
    root: Path,
    paths: Sequence[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """A failed or absent collection report never authorizes an invented selector."""
    with tempfile.TemporaryDirectory(prefix="powdrr-story-collection-") as directory:
        temporary = Path(directory)
        plugin = temporary / "_powdrr_story_collection.py"
        plugin.write_text(_PLUGIN, encoding="utf-8")
        report = temporary / "collected.json"
        command = (
            *pytest_command_for_files(profile, paths),
            "--collect-only",
            "-q",
            "-p",
            "_powdrr_story_collection",
        )
        environment = os.environ.copy()
        environment["POWDRR_STORY_COLLECTION_REPORT"] = str(report)
        environment["POWDRR_STORY_COLLECTION_ROOT"] = str(root.resolve())
        environment.pop("VIRTUAL_ENV", None)
        environment["PYTHONPATH"] = os.pathsep.join(
            [directory, str(root), str(root / "src"), environment.get("PYTHONPATH", "")]
        )
        nested_sources = []
        for raw_path in paths:
            parts = Path(raw_path).parts
            for index, part in enumerate(parts[:-1]):
                if part in {"tests", "test"}:
                    nested = root.joinpath(*parts[:index]) / "src"
                    if nested.is_dir():
                        nested_sources.append(str(nested))
        environment["PYTHONPATH"] = os.pathsep.join(
            [*nested_sources, environment["PYTHONPATH"]]
        )
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            data = json.loads(report.read_text()) if report.is_file() else {}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return {"status": "unavailable", "selectors": [], "error": str(error)}
        if (
            completed.returncode
            or data.get("collection_errors") != 0
            or not data.get("selectors")
        ):
            return {
                "status": "unavailable",
                "selectors": [],
                "error": "pytest collection failed or collected no tests",
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        return {
            "status": "collected",
            "selectors": data["selectors"],
            "runtime": data["runtime"],
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


_EXECUTION_PLUGIN = (
    _RUNTIME_CODE
    + """
import hashlib
import json
import os
import sys
from pathlib import Path
import pytest

_root = Path(os.environ["POWDRR_STORY_EXECUTION_ROOT"])
_expected = json.loads(Path(os.environ["POWDRR_STORY_EXECUTION_INPUT"]).read_text())
_reports = []
_executed = {}
_selectors = {}
_collection_errors = 0
_sources_before = {}
_runtime_before = {}

def _sources():
    return {path: hashlib.sha256((_root / path).read_bytes()).hexdigest()
            for path in _expected["paths"] if (_root / path).is_file()}

def _selector(item):
    return (Path(item.path).relative_to(_root).as_posix()
            + "::" + item.nodeid.split("::", 1)[1])

def pytest_sessionstart(session):
    global _sources_before, _runtime_before
    _sources_before = _sources()
    _runtime_before = _runtime_snapshot()

def pytest_collection_finish(session):
    global _collection_errors
    _collection_errors = session.testsfailed
    _selectors.update({item.nodeid: _selector(item) for item in session.items})

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    selector = _selector(item)
    seen = _executed.setdefault(selector, set())
    paths = {str((_root / path).resolve()): (path, set(lines))
             for path, lines in _expected["assertion_lines"].items()}
    def trace(frame, event, arg):
        target = paths.get(frame.f_code.co_filename)
        if target is None:
            return None
        if event == "line" and frame.f_lineno in target[1]:
            seen.add((target[0], frame.f_lineno))
        return trace
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(previous)

def pytest_runtest_logreport(report):
    _reports.append({"selector": _selectors.get(report.nodeid, report.nodeid),
                     "phase": report.when, "outcome": report.outcome,
                     "wasxfail": str(getattr(report, "wasxfail", "")),
                     "diagnostic": str(report.longrepr or "")[:4000],
                     "exception_type": getattr(report, "_powdrr_exception", None)})

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    report._powdrr_exception = call.excinfo.typename if call.excinfo else None

def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["POWDRR_STORY_EXECUTION_REPORT"]).write_text(json.dumps({
        "schema_version": "pytest-story-execution-v1",
        "selectors": list(_selectors.values()), "collection_errors": _collection_errors,
        "reports": _reports,
        "assertion_lines": {key: sorted(values) for key, values in _executed.items()},
        "sources_before": _sources_before, "sources_after": _sources(),
        "runtime": _runtime_before,
        "runtime_after": _runtime_snapshot(),
        "exitstatus": int(exitstatus),
    }))
"""
)


def run_story_tests(
    profile: ValidationProfile,
    *,
    root: Path,
    paths: Sequence[str],
    assertion_lines: dict[str, list[int]],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Observe pytest phases and assertion execution without editing tests."""
    with tempfile.TemporaryDirectory(prefix="powdrr-story-execution-") as directory:
        temporary = Path(directory)
        (temporary / "_powdrr_story_execution.py").write_text(_EXECUTION_PLUGIN)
        inputs = temporary / "inputs.json"
        inputs.write_text(
            json.dumps({"paths": list(paths), "assertion_lines": assertion_lines})
        )
        report = temporary / "execution.json"
        command = (
            *pytest_command_for_files(profile, paths),
            "-p",
            "_powdrr_story_execution",
        )
        environment = os.environ.copy()
        environment.pop("VIRTUAL_ENV", None)
        environment.update(
            POWDRR_STORY_EXECUTION_ROOT=str(root.resolve()),
            POWDRR_STORY_EXECUTION_INPUT=str(inputs),
            POWDRR_STORY_EXECUTION_REPORT=str(report),
        )
        search = [directory, str(root), str(root / "src")]
        for raw_path in paths:
            parts = Path(raw_path).parts
            for index, part in enumerate(parts[:-1]):
                if part in {"test", "tests"}:
                    nested = root.joinpath(*parts[:index]) / "src"
                    if nested.is_dir():
                        search.append(str(nested))
        search.append(environment.get("PYTHONPATH", ""))
        environment["PYTHONPATH"] = os.pathsep.join(search)
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            observed = json.loads(report.read_text()) if report.is_file() else {}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return {
                "passed": False,
                "command": list(command),
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "error": str(error),
                "story_execution": {"status": "unavailable"},
            }
        return {
            "passed": completed.returncode == 0,
            "command": list(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "error": None,
            "story_execution": observed or {"status": "unavailable"},
        }


def capture_story_execution_context(
    root: Path,
    *,
    repository: str,
    command: Sequence[str],
    excluded_paths: Sequence[str] = (),
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an immutable Git tree using a temporary index; leave the real index alone.

    Evidence destinations supplied by the trusted adapter are omitted, avoiding a
    receipt containing its own hash. The exclusion list is part of configuration.
    """
    import hashlib

    def git(*args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    paths = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    selected = []
    for raw in paths:
        if not raw:
            continue
        path = raw.decode("utf-8")
        if any(
            path == excluded or path.startswith(excluded.rstrip("/") + "/")
            for excluded in excluded_paths
        ):
            continue
        absolute = root / path
        if absolute.is_file() or absolute.is_symlink():
            selected.append(raw)
        elif absolute.is_dir():
            raise ValueError(
                f"story execution requires an explicit submodule identity: {path}"
            )
    with tempfile.TemporaryDirectory(prefix="powdrr-story-tree-") as directory:
        temporary = Path(directory)
        environment = {
            **os.environ,
            "GIT_INDEX_FILE": str(temporary / "index"),
            "GIT_LITERAL_PATHSPECS": "1",
        }
        inventory = temporary / "paths"
        inventory.write_bytes(b"\0".join(sorted(set(selected))) + b"\0")
        git("read-tree", "--empty", env=environment)
        if selected:
            git(
                "add",
                "-f",
                "--pathspec-from-file=" + str(inventory),
                "--pathspec-file-nul",
                env=environment,
            )
        tree = git("write-tree", env=environment)
    configuration = {"command": list(command), "excluded_paths": sorted(excluded_paths)}
    normalized_pythonpath = os.environ.get("PYTHONPATH", "").replace(
        str(root), "<repository>"
    )
    environment_identity = {
        "runtime": dict(runtime),
        "pythonpath": normalized_pythonpath,
        "variables": {
            key: value.replace(str(root), "<repository>")
            for key, value in os.environ.items()
            if key not in {"PWD", "OLDPWD", "SHLVL", "_", "GIT_INDEX_FILE"}
        },
    }

    def digest(value: Any) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    return {
        "repository": repository,
        "candidate_sha": tree,
        "configuration_digest": digest(configuration),
        "environment_digest": digest(environment_identity),
    }
