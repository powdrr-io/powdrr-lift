"""Generate and run instruction-derived post-run behavior probes."""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.validation import DiscoveredValidationProfile
from powdrr_lift.workrr.llm import complete_json
from powdrr_lift.workrr.protocol import WorkflowLLMClient

POSTRUN_PROBE_SCHEMA_VERSION = "instruction-derived-probe-suite-v1"
POSTRUN_PROBE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "probes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "probe_id": {"type": "string"},
                    "scenario_id": {"type": "string"},
                    "oracle_basis": {"type": "string"},
                    "test_source": {"type": "string"},
                },
                "required": [
                    "probe_id",
                    "scenario_id",
                    "oracle_basis",
                    "test_source",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["probes"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class PostRunProbe:
    probe_id: str
    scenario_id: str
    oracle_basis: str
    test_source: str

    def to_data(self) -> dict[str, str]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "oracle_basis": self.oracle_basis,
            "test_source": self.test_source,
        }


@dataclass(frozen=True, slots=True)
class PostRunProbeResult:
    probe_id: str
    scenario_id: str
    status: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    output: str = ""
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "command": list(self.command),
            "returncode": self.returncode,
            "output": self.output,
            "error": self.error,
        }


def generate_postrun_probes(
    client: WorkflowLLMClient,
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    repository_contract: Mapping[str, Any] | None = None,
) -> tuple[PostRunProbe, ...]:
    """Ask for executable checks while the candidate implementation is absent."""
    eligible = [
        dict(item)
        for item in scenarios
        if isinstance(item.get("scenario_id"), str)
        and item.get("routing", "include") == "include"
        and not item.get("unresolved_dimensions")
    ]
    if not eligible:
        return ()
    messages = [
        {
            "role": "system",
            "content": (
                "Create a small independent Python probe suite for software behavior. "
                "Use only the source instruction, behavior scenarios, and baseline "
                "repository contract supplied below. The candidate implementation "
                "and task solution tests are intentionally unavailable. Each probe "
                "must test one observable behavior and use a public API or stable "
                "repository contract. Do not invent expected behavior: omit a "
                "scenario if its oracle is unresolved. Every probe must import and "
                "exercise the candidate software's public API or stable repository "
                "contract, then assert the observed result. Never test that the "
                "instruction text contains a requirement or copy source text into "
                "an assertion. Return test_source as a "
                "self-contained Python module containing exactly one ordinary "
                "zero-argument test function. Use plain Python assertions; do not "
                "use pytest fixtures or pytest-specific helpers. The function name "
                "must start with test_probe_. The function must have no arguments, "
                "contain at least one assert and one call to the software under "
                "test, and must not import pytest. Do not "
                "write files, launch subprocesses, access the network, or alter "
                "repository state. Keep probes minimal and deterministic."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "behavior_scenarios": eligible,
                    "baseline_repository_contract": repository_contract or {},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    response = complete_json(
        client, messages, response_schema=POSTRUN_PROBE_RESPONSE_SCHEMA
    )
    raw_probes = response.get("probes")
    if not isinstance(raw_probes, list):
        raise ValueError("probe generator response must contain a probes array")
    probes: list[PostRunProbe] = []
    seen_ids: set[str] = set()
    known_scenarios = {str(item["scenario_id"]) for item in eligible}
    for raw in raw_probes:
        if not isinstance(raw, Mapping):
            raise ValueError("probe generator returned a malformed probe")
        probe_id = _required_text(raw, "probe_id")
        scenario_id = _required_text(raw, "scenario_id")
        oracle_basis = _required_text(raw, "oracle_basis")
        test_source = _required_text(raw, "test_source")
        if probe_id in seen_ids:
            raise ValueError(f"probe generator returned duplicate id {probe_id!r}")
        if scenario_id not in known_scenarios:
            raise ValueError(f"probe refers to unknown scenario {scenario_id!r}")
        _validate_test_source(test_source)
        seen_ids.add(probe_id)
        probes.append(PostRunProbe(probe_id, scenario_id, oracle_basis, test_source))
    return tuple(probes)


def run_postrun_probes(
    probes: Sequence[PostRunProbe],
    *,
    root: Path,
    profiles: Sequence[DiscoveredValidationProfile],
    timeout_seconds: float = 180.0,
) -> tuple[PostRunProbeResult, ...]:
    """Run frozen probes independently of the repository's test framework."""
    del profiles  # Probes have their own execution boundary and need no test profile.
    results: list[PostRunProbeResult] = []
    with tempfile.TemporaryDirectory(prefix="powdrr-probes-") as temporary:
        probe_root = Path(temporary)
        runner_path = probe_root / "run_probe.py"
        runner_path.write_text(_PROBE_RUNNER, encoding="utf-8")
        for index, probe in enumerate(probes):
            test_path = probe_root / f"test_probe_{index:03d}.py"
            test_path.write_text(probe.test_source.rstrip() + "\n", encoding="utf-8")
            command = (sys.executable, str(runner_path), str(test_path))
            environment = os.environ.copy()
            environment.pop("VIRTUAL_ENV", None)
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


def probe_failures(results: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Convert failed probe results into focused coding-agent repair findings."""
    return [
        {
            "scenario_id": str(item.get("scenario_id", "unknown")),
            "reason": (
                f"probe {item.get('probe_id', 'unknown')} {item.get('status')}: "
                f"{_truncate(str(item.get('output') or item.get('error') or ''), 2400)}"
            ),
        }
        for item in results
        if item.get("status") in {"failed", "timed_out"}
    ]


_PROBE_RUNNER = """\
import importlib.util
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("powdrr_generated_probe", path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load generated probe: {path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tests = [
    value
    for name, value in vars(module).items()
    if name.startswith("test_probe_") and callable(value)
]
if len(tests) != 1:
    raise RuntimeError(f"expected exactly one probe function, found {len(tests)}")
tests[0]()
"""


def _validate_test_source(source: str) -> None:
    try:
        tree = ast.parse(source)
        compile(tree, "<generated-postrun-probe>", "exec")
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"generated probe is not valid Python: {error}") from error
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(functions) != 1 or not functions[0].name.startswith("test_probe_"):
        raise ValueError("each generated probe must contain one test_probe_ function")
    function = functions[0]
    if (
        function.args.args
        or function.args.posonlyargs
        or function.args.kwonlyargs
        or function.args.vararg
        or function.args.kwarg
    ):
        raise ValueError("generated probe function must take no arguments")
    if any(
        not isinstance(item, (ast.Import, ast.ImportFrom, ast.FunctionDef))
        for item in tree.body
    ):
        raise ValueError("generated probe has executable module-level statements")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"open", "exec", "eval", "compile", "__import__"}:
                raise ValueError(
                    f"generated probe uses disallowed function {node.func.id}"
                )
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            raise ValueError("generated probe references __builtins__")
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
            if "pytest" in roots:
                raise ValueError("generated probe must not depend on pytest")
            if roots & {
                "os",
                "socket",
                "subprocess",
                "shutil",
                "urllib",
                "requests",
                "pathlib",
                "tempfile",
                "importlib",
                "ctypes",
            }:
                raise ValueError("generated probe imports a disallowed system module")
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root == "pytest":
                raise ValueError("generated probe must not depend on pytest")
            if root in {
                "os",
                "socket",
                "subprocess",
                "shutil",
                "urllib",
                "requests",
                "pathlib",
                "tempfile",
                "importlib",
                "ctypes",
            }:
                raise ValueError("generated probe imports a disallowed system module")
    if not any(isinstance(node, ast.Assert) for node in ast.walk(function)):
        raise ValueError("generated probe must contain an observable assertion")
    builtin_names = set(dir(builtins))
    assert_calls = (
        call
        for assertion in ast.walk(function)
        if isinstance(assertion, ast.Assert)
        for call in ast.walk(assertion.test)
        if isinstance(call, ast.Call)
    )
    if not any(
        not isinstance(call.func, ast.Name) or call.func.id not in builtin_names
        for call in assert_calls
    ):
        raise ValueError(
            "generated probe assertion must exercise a non-built-in software call"
        )


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"probe generator returned an empty {key}")
    return item.strip()


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


__all__ = [
    "POSTRUN_PROBE_RESPONSE_SCHEMA",
    "POSTRUN_PROBE_SCHEMA_VERSION",
    "PostRunProbe",
    "PostRunProbeResult",
    "generate_postrun_probes",
    "probe_failures",
    "run_postrun_probes",
]
