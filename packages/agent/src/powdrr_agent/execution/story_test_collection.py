"""Collect real pytest node IDs in an isolated process through its public hooks."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from powdrr_agent.execution.execute_pr_tests import (
    ValidationProfile,
    pytest_command_for_files,
)

_PLUGIN = """
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
    }))
"""


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
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
