"""Build auditable task records from DeepSWE instructions and test patches."""

from __future__ import annotations

import ast
import json
import re
import shlex
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.validation import discover_validation_profiles

from . import SCHEMA_VERSION

_TEST_DECLARATIONS = (
    re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*func\s+(Test[A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*(?:it|test)\s*\(\s*(['\"`])(.+?)\1"),
    re.compile(r"^\s*(?:it|test)\.(?:each|todo|skip)\s*\(\s*(['\"`])(.+?)\1"),
    re.compile(r"^\s*fn\s+(test_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*fn\s+([A-Za-z][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^\s*testName\s*:\s*(['\"`])(.+?)\1"),
    re.compile(r"^\s*name\s*:\s*(['\"`])(.+?)\1"),
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_task_record(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported task record: {path}")
    if not isinstance(value.get("input"), dict):
        raise ValueError(f"task record has no input object: {path}")
    return value


def discover_task_dirs(tasks_dir: Path) -> tuple[Path, ...]:
    if not tasks_dir.is_dir():
        raise ValueError(f"tasks directory does not exist: {tasks_dir}")
    return tuple(
        sorted(
            (
                path
                for path in tasks_dir.iterdir()
                if (path / "instruction.md").is_file()
            ),
            key=lambda path: path.name,
        )
    )


def load_repository_roots(path: Path | None) -> dict[str, Path]:
    if path is None:
        return {}
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValueError("repository roots JSON must map task IDs to repository paths")
    return {str(task_id): Path(str(root)).resolve() for task_id, root in value.items()}


def collect_task_record(
    task_dir: Path,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Read task metadata, instruction, validation config, and test patch labels."""
    task_dir = task_dir.resolve()
    instruction_path = task_dir / "instruction.md"
    instruction = instruction_path.read_text(encoding="utf-8").strip()
    task_config_path = task_dir / "task.toml"
    task_config = _load_task_config(task_config_path)
    metadata = task_config.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    task_table = task_config.get("task", {})
    task_table = task_table if isinstance(task_table, dict) else {}
    task_id = str(metadata.get("task_id") or task_dir.name)
    repository_id = str(
        metadata.get("repository_url") or metadata.get("repository") or "unknown"
    )

    validation = []
    if repository_root is not None:
        validation = [
            {
                "name": profile.name,
                "command": list(profile.command),
                "source": profile.source,
            }
            for profile in discover_validation_profiles(repository_root)
        ]
    if not validation:
        validation = _declared_validation(task_config)

    patch_path = task_dir / "tests" / "test.patch"
    cases = _extract_patch_cases(patch_path) if patch_path.is_file() else []
    availability = "patch_test_cases" if cases else "patch_without_test_cases"

    config_task_id = str(task_table.get("name") or task_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "repository_id": repository_id,
        "input": {
            "instruction": instruction,
            "validation": validation,
        },
        "ground_truth": {
            "availability": availability,
            "cases": sorted(
                cases, key=lambda item: (item["source_file"], item["test_name"])
            ),
        },
        "provenance": {
            "instruction_source": str(instruction_path.resolve()),
            "task_config_source": str(task_config_path.resolve())
            if task_config_path.is_file()
            else None,
            "declared_task_name": config_task_id,
            "validation_repository_root": str(repository_root.resolve())
            if repository_root is not None
            else None,
            "ground_truth_sources": [str(patch_path.resolve())]
            if patch_path.is_file()
            else [],
        },
    }


def collect_records(
    *,
    tasks_dir: Path,
    repository_roots: dict[str, Path],
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen_task_ids: set[str] = set()
    for task_dir in discover_task_dirs(tasks_dir):
        provisional_id = _task_id_for_dir(task_dir)
        record = collect_task_record(
            task_dir,
            repository_root=repository_roots.get(provisional_id),
        )
        if record["task_id"] in seen_task_ids:
            raise ValueError(
                f"duplicate task ID in task directory: {record['task_id']}"
            )
        seen_task_ids.add(record["task_id"])
        records.append(record)
        (output_dir / f"{record['task_id']}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    audit = build_audit(records)
    (output_dir / "audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return audit


def build_audit(records: list[dict[str, Any]]) -> dict[str, Any]:
    availability = Counter(
        str(record["ground_truth"]["availability"]) for record in records
    )
    extraction_methods = Counter(
        str(case["extraction_method"])
        for record in records
        for case in record["ground_truth"]["cases"]
    )
    validation_profiles = Counter(
        str(profile.get("name", "unknown"))
        for record in records
        for profile in record["input"].get("validation", [])
    )
    tasks_with_cases = sum(bool(record["ground_truth"]["cases"]) for record in records)
    return {
        "schema_version": "deepswe-test-prediction-audit-v2",
        "unique_task_count": len({record["task_id"] for record in records}),
        "task_ids": sorted({record["task_id"] for record in records}),
        "tasks_with_patch_test_cases": sum(
            record["ground_truth"]["availability"] == "patch_test_cases"
            for record in records
        ),
        "tasks_with_any_labeled_cases": tasks_with_cases,
        "ground_truth_availability": dict(sorted(availability.items())),
        "ground_truth_case_extraction_methods": dict(
            sorted(extraction_methods.items())
        ),
        "validation_profile_counts": dict(sorted(validation_profiles.items())),
        "validation_metadata_available": sum(
            bool(record["input"].get("validation")) for record in records
        ),
        "tasks": [
            {
                "task_id": record["task_id"],
                "repository_id": record["repository_id"],
                "validation_count": len(record["input"].get("validation", [])),
                "ground_truth_availability": record["ground_truth"]["availability"],
                "ground_truth_case_count": len(record["ground_truth"]["cases"]),
                "ground_truth_source_count": len(
                    record["provenance"]["ground_truth_sources"]
                ),
            }
            for record in sorted(records, key=lambda item: item["task_id"])
        ],
    }


def _extract_patch_cases(patch_path: Path) -> list[dict[str, Any]]:
    """Extract named test cases and added source from a unified test patch."""
    lines = patch_path.read_text(encoding="utf-8", errors="replace").splitlines()
    cases: list[dict[str, Any]] = []
    current_file = ""
    added: list[str] = []
    case_id_counts: Counter[str] = Counter()

    def flush() -> None:
        nonlocal added
        initial_case_count = len(cases)
        declarations = []
        for index, line in enumerate(added):
            declaration = next(
                (
                    match
                    for pattern in _TEST_DECLARATIONS
                    if (match := pattern.match(line))
                ),
                None,
            )
            if declaration is None:
                continue
            if declaration.re.pattern.startswith(r"^\s*fn\s+") and not (
                declaration.group(1).startswith("test_")
                or any("#[test]" in prior for prior in added[max(0, index - 3) : index])
            ):
                continue
            declarations.append((index, declaration))
        for declaration_index, (index, declaration) in enumerate(declarations):
            name = (
                declaration.group(2)
                if declaration.lastindex == 2
                else declaration.group(1)
            )
            if not name:
                continue
            end = (
                declarations[declaration_index + 1][0]
                if declaration_index + 1 < len(declarations)
                else len(added)
            )
            # Decorators carry important test dimensions (for example pytest
            # parameter tables), so include the contiguous decorator block.
            excerpt_start = index
            while excerpt_start > 0 and (
                added[excerpt_start - 1].lstrip().startswith("@")
                or added[excerpt_start - 1].lstrip().startswith("#[")
                or not added[excerpt_start - 1].strip()
                and excerpt_start > 1
                and (
                    added[excerpt_start - 2].lstrip().startswith("@")
                    or added[excerpt_start - 2].lstrip().startswith("#[")
                )
            ):
                excerpt_start -= 1
            body = added[excerpt_start : min(end, index + 120)]
            excerpt = "\n".join(added[excerpt_start : min(end, index + 120)]).strip()
            identifier = f"{current_file}::{name}"
            case_id_counts[identifier] += 1
            if case_id_counts[identifier] > 1:
                identifier = f"{identifier}#{case_id_counts[identifier]}"
            variants = _parameterized_case_names(body)
            if variants:
                for variant in variants:
                    cases.append(
                        {
                            "id": f"{identifier}[{variant}]",
                            "test_name": f"{name}[{variant}]",
                            "behavior_group_id": identifier,
                            "source_file": current_file,
                            "source_excerpt": excerpt,
                            "extraction_method": "parameterized_declaration",
                        }
                    )
            else:
                cases.append(
                    {
                        "id": identifier,
                        "test_name": name,
                        "behavior_group_id": identifier,
                        "source_file": current_file,
                        "source_excerpt": excerpt,
                        "extraction_method": "named_declaration",
                    }
                )
        if (
            len(cases) == initial_case_count
            and added
            and _is_test_source_file(current_file)
        ):
            excerpt = "\n".join(added[:160]).strip()[:12000]
            identifier = f"{current_file}::patch-content"
            cases.append(
                {
                    "id": identifier,
                    "test_name": f"test patch for {current_file}",
                    "behavior_group_id": identifier,
                    "source_file": current_file,
                    "source_excerpt": excerpt,
                    "extraction_method": "patch_file_fallback",
                }
            )
        added = []

    for line in lines:
        if line.startswith("diff --git "):
            flush()
            match = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            current_file = match.group(2) if match else "unknown"
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith((" ", "-", "\\")):
            continue
        elif line.startswith("@@"):
            continue
    flush()
    return cases


def _parameterized_case_names(lines: list[str]) -> list[str]:
    """Return statically declared pytest or Go table case names, if present."""
    source = "\n".join(lines)
    names: list[str] = []
    try:
        module = ast.parse(source)
    except SyntaxError:
        module = None
    if module is not None:
        calls = [
            decorator
            for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize"
            and isinstance(decorator.func.value, ast.Attribute)
            and decorator.func.value.attr == "mark"
        ]
        for call in calls:
            if len(call.args) < 2:
                continue
            try:
                parameters = ast.literal_eval(call.args[0])
                values = ast.literal_eval(call.args[1])
            except (SyntaxError, ValueError):
                continue
            parameter_names = (
                [part.strip() for part in parameters.split(",")]
                if isinstance(parameters, str)
                else [str(item) for item in parameters]
            )
            explicit_ids = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "ids"),
                None,
            )
            try:
                explicit_ids = ast.literal_eval(explicit_ids) if explicit_ids else None
            except (SyntaxError, ValueError):
                explicit_ids = None
            if not isinstance(values, (list, tuple)):
                continue
            for index, value in enumerate(values):
                if isinstance(explicit_ids, (list, tuple)) and index < len(
                    explicit_ids
                ):
                    label = str(explicit_ids[index])
                else:
                    values_for_row = (
                        value if isinstance(value, (list, tuple)) else (value,)
                    )
                    label_parts = []
                    for pos, item in enumerate(values_for_row):
                        parameter = (
                            parameter_names[pos]
                            if pos < len(parameter_names)
                            else str(pos)
                        )
                        label_parts.append(f"{parameter}={item!r}")
                    label = ",".join(label_parts)
                names.append(label or f"case-{index + 1}")
    if not names:
        names.extend(re.findall(r"\bname\s*:\s*\"([^\"]+)\"", source))
    return list(dict.fromkeys(names))


def _is_test_source_file(path: str) -> bool:
    name = Path(path).name.casefold()
    parts = {part.casefold() for part in Path(path).parts}
    return not name.endswith(".sh") and bool(
        parts.intersection({"test", "tests", "__tests__", "testdata"})
        or name.startswith("test")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


def _task_id_for_dir(task_dir: Path) -> str:
    task_config = _load_task_config(task_dir / "task.toml")
    metadata = task_config.get("metadata", {})
    if isinstance(metadata, dict) and metadata.get("task_id"):
        return str(metadata["task_id"])
    return task_dir.name


def _load_task_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _declared_validation(task_config: dict[str, Any]) -> list[dict[str, Any]]:
    verifier = task_config.get("verifier", {})
    if not isinstance(verifier, dict):
        return []
    singular = "commands" not in verifier
    commands = verifier.get("commands", verifier.get("command", []))
    if isinstance(commands, str):
        commands = [shlex.split(commands)]
    elif isinstance(commands, list) and all(
        isinstance(value, str) for value in commands
    ):
        commands = (
            [commands] if singular else [shlex.split(value) for value in commands]
        )
    if not isinstance(commands, list):
        return []
    return [
        {
            "name": f"verifier-command-{index + 1}",
            "command": [str(part) for part in command],
            "source": "task.toml verifier command",
        }
        for index, command in enumerate(commands)
        if isinstance(command, list)
        and command
        and all(isinstance(part, str) for part in command)
    ]
