"""Run already-authored behavior probes against a local repository."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from powdrr_contracts.verification import PostRunProbeResult


class ProbeToRun(Protocol):
    """Minimal probe data needed by the local execution adapter."""

    @property
    def probe_id(self) -> str: ...

    @property
    def scenario_id(self) -> str: ...

    @property
    def test_source(self) -> str: ...

    @property
    def function_name(self) -> str: ...


def run_postrun_probes(
    probes: Sequence[ProbeToRun],
    *,
    root: Path,
    timeout_seconds: float = 180.0,
) -> tuple[PostRunProbeResult, ...]:
    """Run frozen probes independently of the repository's test framework."""
    results: list[PostRunProbeResult] = []
    with tempfile.TemporaryDirectory(prefix="powdrr-probes-") as temporary:
        probe_root = Path(temporary)
        runner_path = probe_root / "run_probe.py"
        runner_path.write_text(_PROBE_RUNNER, encoding="utf-8")
        suites: dict[str, Path] = {}
        for probe in probes:
            suite_path = suites.get(probe.test_source)
            if suite_path is None:
                suite_path = probe_root / f"suite_{len(suites):03d}.py"
                suite_path.write_text(
                    probe.test_source.rstrip() + "\n", encoding="utf-8"
                )
                suites[probe.test_source] = suite_path
            command = (
                sys.executable,
                str(runner_path),
                str(suite_path),
                probe.function_name,
            )
            environment = os.environ.copy()
            environment.pop("VIRTUAL_ENV", None)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            existing_pythonpath = environment.get("PYTHONPATH", "")
            import_paths = [str(root), str(root / "src")]
            if existing_pythonpath:
                import_paths.append(existing_pythonpath)
            environment["PYTHONPATH"] = os.pathsep.join(import_paths)
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
            except subprocess.TimeoutExpired as error:
                results.append(
                    PostRunProbeResult(
                        probe.probe_id,
                        probe.scenario_id,
                        "timed_out",
                        command,
                        124,
                        _output(error.stdout, error.stderr),
                        "probe timed out",
                    )
                )
                continue
            except OSError as error:
                results.append(
                    PostRunProbeResult(
                        probe.probe_id,
                        probe.scenario_id,
                        "unavailable",
                        command,
                        error=str(error),
                    )
                )
                continue
            results.append(
                PostRunProbeResult(
                    probe.probe_id,
                    probe.scenario_id,
                    "passed" if completed.returncode == 0 else "failed",
                    command,
                    completed.returncode,
                    _output(completed.stdout, completed.stderr),
                )
            )
    return tuple(results)


_PROBE_RUNNER = """\
import importlib.util
import sys

path, function_name = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("powdrr_generated_probe", path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load generated probe: {path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
function = getattr(module, function_name, None)
if not callable(function):
    raise RuntimeError(f"generated probe function not found: {function_name}")
function()
"""


def _output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    chunks = [
        part.decode(errors="replace") if isinstance(part, bytes) else part
        for part in (stdout, stderr)
        if part
    ]
    return _truncate("\n".join(chunks), 8000)


def _truncate(value: str, limit: int) -> str:
    normalized = re.sub(r"\n{3,}", "\n\n", value).strip()
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


__all__ = ["ProbeToRun", "run_postrun_probes"]
