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

POSTRUN_PROBE_SCHEMA_VERSION = "instruction-derived-probe-suite-v3"


@dataclass(frozen=True, slots=True)
class PostRunProbe:
    probe_id: str
    scenario_id: str
    oracle_basis: str
    test_source: str
    function_name: str
    obligation_id: str = ""
    obligation_category: str = "other"
    obligation_rule: str = "not_applicable"

    def to_data(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "oracle_basis": self.oracle_basis,
            "function_name": self.function_name,
            "test_source": self.test_source,
            "obligation_id": self.obligation_id,
            "obligation_category": self.obligation_category,
            "obligation_rule": self.obligation_rule,
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


def parse_generated_postrun_probe_source(
    source: str,
    scenarios: Sequence[Mapping[str, Any]],
    *,
    instruction: str,
    repository_contract: Mapping[str, Any] | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[PostRunProbe, ...]]:
    """Validate MiniSWE's executable probes and typed obligations in one module."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("probe suite source is empty")
    try:
        tree = ast.parse(source)
        compile(tree, "<miniswe-postrun-probes>", "exec")
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"probe suite is not valid Python: {error}") from error

    functions = [
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name.startswith("test_probe_")
    ]
    module_doc_node = tree.body[0] if tree.body else None
    if not (
        isinstance(module_doc_node, ast.Expr)
        and isinstance(module_doc_node.value, ast.Constant)
        and isinstance(module_doc_node.value.value, str)
    ):
        raise ValueError(
            "probe suite must declare typed obligations in its module docstring"
        )
    allowed_top_level = (ast.Import, ast.ImportFrom, ast.FunctionDef)
    if any(
        item is not module_doc_node and not isinstance(item, allowed_top_level)
        for item in tree.body
    ):
        raise ValueError("probe suite has executable module-level statements")

    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc is None:
        raise ValueError(
            "probe suite must declare typed obligations in its module docstring"
        )
    try:
        module_metadata = json.loads(module_doc)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"probe suite module docstring must be JSON: {error}"
        ) from error
    if not isinstance(module_metadata, Mapping) or not isinstance(
        module_metadata.get("obligations"), list
    ):
        raise ValueError("probe suite module metadata must contain obligations")
    obligations = _validate_probe_obligations(
        module_metadata["obligations"],
        scenarios,
        instruction=instruction,
        repository_contract=repository_contract or {},
    )
    if not functions and any(item["supported"] is True for item in obligations):
        raise ValueError("probe suite must define probes for supported obligations")
    obligation_by_id = {item["obligation_id"]: item for item in obligations}

    known_scenarios = {str(item["scenario_id"]) for item in scenarios}
    seen_probe_ids: set[str] = set()
    covered_obligations: set[str] = set()
    probes: list[PostRunProbe] = []
    for function in functions:
        if isinstance(function, ast.AsyncFunctionDef):
            raise ValueError("probe functions must be synchronous")
        if (
            function.args.args
            or function.args.posonlyargs
            or function.args.kwonlyargs
            or function.args.vararg
            or function.args.kwarg
        ):
            raise ValueError(f"{function.name} must take no arguments")
        metadata = _probe_metadata(function)
        probe_id = _required_text(metadata, "probe_id")
        scenario_id = _required_text(metadata, "scenario_id")
        oracle_basis = _required_text(metadata, "oracle_basis")
        obligation_id = _required_text(metadata, "obligation_id")
        if probe_id in seen_probe_ids:
            raise ValueError(f"probe suite has duplicate probe id {probe_id!r}")
        if scenario_id not in known_scenarios:
            raise ValueError(f"probe refers to unknown scenario {scenario_id!r}")
        obligation = obligation_by_id.get(obligation_id)
        if obligation is None or obligation.get("supported") is not True:
            raise ValueError(
                f"probe refers to unsupported obligation {obligation_id!r}"
            )
        if obligation.get("scenario_id") != scenario_id:
            raise ValueError(
                f"probe {probe_id!r} does not match its obligation scenario"
            )
        seen_probe_ids.add(probe_id)
        covered_obligations.add(obligation_id)
        _validate_probe_function(function, tree)
        probes.append(
            PostRunProbe(
                probe_id,
                scenario_id,
                oracle_basis,
                source,
                function.name,
                obligation_id,
                str(obligation["category"]),
                str(obligation["rule"]),
            )
        )
    missing_obligations = {
        str(item["obligation_id"]) for item in obligations if item["supported"] is True
    } - covered_obligations
    if missing_obligations:
        raise ValueError(
            "probe suite omitted supported obligations: "
            + ", ".join(sorted(missing_obligations))
        )
    return obligations, tuple(probes)


def _validate_probe_obligations(
    raw_obligations: list[Any],
    scenarios: Sequence[Mapping[str, Any]],
    *,
    instruction: str,
    repository_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    category_values = {
        "transformation",
        "validation",
        "preservation",
        "lifecycle",
        "compatibility",
        "presence",
        "other",
    }
    rule_values = {
        "reference_integrity",
        "uniqueness",
        "collision",
        "mutual_exclusion",
        "type_constraint",
        "range_constraint",
        "presence",
        "format",
        "other",
        "not_applicable",
        "unresolved",
    }
    operand_roles = {
        "mapping_key",
        "mapping_value",
        "input",
        "result",
        "constraint",
        "trigger",
        "state",
        "other",
    }
    known_scenarios = {str(item["scenario_id"]) for item in scenarios}
    instruction_strings = (instruction,)
    contract_strings = tuple(_string_leaves(repository_contract))
    seen: set[str] = set()
    covered: set[str] = set()
    obligations: list[dict[str, Any]] = []
    for raw in raw_obligations:
        if not isinstance(raw, Mapping):
            raise ValueError("probe suite contains a malformed obligation")
        item = dict(raw)
        obligation_id = _required_text(item, "obligation_id")
        scenario_id = _required_text(item, "scenario_id")
        if obligation_id in seen:
            raise ValueError(f"duplicate obligation id {obligation_id!r}")
        if scenario_id not in known_scenarios:
            raise ValueError(f"obligation refers to unknown scenario {scenario_id!r}")
        if (
            item.get("category") not in category_values
            or item.get("rule") not in rule_values
        ):
            raise ValueError(f"obligation {obligation_id!r} has an invalid type")
        if not isinstance(item.get("supported"), bool):
            raise ValueError(f"obligation {obligation_id!r} has invalid support status")
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(f"obligation {obligation_id!r} has malformed evidence")
        for citation in evidence:
            if not isinstance(citation, Mapping):
                raise ValueError(f"obligation {obligation_id!r} has malformed evidence")
            source, quote = citation.get("source"), citation.get("quote")
            allowed_strings = (
                instruction_strings if source == "instruction" else contract_strings
            )
            if (
                source not in {"instruction", "repository_contract"}
                or not isinstance(quote, str)
                or not quote.strip()
                or not any(quote in value for value in allowed_strings)
            ):
                raise ValueError(f"obligation {obligation_id!r} cites absent evidence")
        if item["supported"]:
            if item["rule"] == "unresolved" or not evidence:
                raise ValueError(
                    f"supported obligation {obligation_id!r} is not grounded"
                )
            for key in ("summary", "condition", "operation", "expected"):
                _required_text(item, key)
            operands = item.get("operands")
            if not isinstance(operands, list):
                raise ValueError(f"obligation {obligation_id!r} has malformed operands")
            for operand in operands:
                if (
                    not isinstance(operand, Mapping)
                    or operand.get("role") not in operand_roles
                ):
                    raise ValueError(
                        f"obligation {obligation_id!r} has invalid operand roles"
                    )
                _required_text(operand, "name")
                _required_text(operand, "meaning")
        elif item["rule"] != "unresolved":
            raise ValueError(
                f"unsupported obligation {obligation_id!r} must be unresolved"
            )
        else:
            _required_text(item, "unresolved_reason")
        seen.add(obligation_id)
        covered.add(scenario_id)
        obligations.append(item)
    missing = known_scenarios - covered
    if missing:
        raise ValueError("probe suite omitted scenarios: " + ", ".join(sorted(missing)))
    return tuple(obligations)


def _probe_metadata(function: ast.FunctionDef) -> Mapping[str, Any]:
    docstring = ast.get_docstring(function, clean=False)
    if docstring is None:
        raise ValueError(f"{function.name} must have a JSON metadata docstring")
    try:
        metadata = json.loads(docstring)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{function.name} metadata docstring must be a JSON object: {error}"
        ) from error
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{function.name} metadata must be a JSON object")
    return metadata


def _validate_probe_function(function: ast.FunctionDef, tree: ast.Module) -> None:
    builtin_names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"open", "exec", "eval", "compile", "__import__"}:
                raise ValueError(f"probe suite uses disallowed function {node.func.id}")
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            raise ValueError("probe suite references __builtins__")
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
            _validate_import_roots(roots)
        if isinstance(node, ast.ImportFrom):
            _validate_import_roots({(node.module or "").split(".", 1)[0]})

    if not any(isinstance(node, ast.Assert) for node in ast.walk(function)):
        raise ValueError(f"{function.name} must contain an observable assertion")
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
            f"{function.name} assertion must exercise a non-built-in software call"
        )


def _validate_import_roots(roots: set[str]) -> None:
    if "pytest" in roots:
        raise ValueError("probe suite must not depend on pytest")
    if roots & {
        "os",
        "socket",
        "subprocess",
        "shutil",
        "urllib",
        "requests",
        "tempfile",
        "importlib",
        "ctypes",
    }:
        raise ValueError("probe suite imports a disallowed system module")


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"probe metadata has an empty {key}")
    return item.strip()


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def run_postrun_probes(
    probes: Sequence[PostRunProbe],
    *,
    root: Path,
    profiles: Sequence[DiscoveredValidationProfile],
    timeout_seconds: float = 180.0,
) -> tuple[PostRunProbeResult, ...]:
    """Run frozen probes independently of the repository's test framework."""
    del profiles
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


def baseline_probe_failures(
    results: Sequence[PostRunProbeResult],
) -> list[str]:
    """Return probes that did not execute and fail through an assertion."""
    problems = []
    for result in results:
        if result.status != "failed" or "AssertionError" not in result.output:
            problems.append(
                f"{result.probe_id} ({result.scenario_id}) returned "
                f"{result.status}; expected an AssertionError. "
                f"{_truncate(result.output or result.error or '', 1600)}"
            )
    return problems


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


__all__ = [
    "POSTRUN_PROBE_SCHEMA_VERSION",
    "PostRunProbe",
    "PostRunProbeResult",
    "baseline_probe_failures",
    "parse_generated_postrun_probe_source",
    "probe_failures",
    "run_postrun_probes",
]
