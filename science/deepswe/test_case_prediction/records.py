"""Build auditable task records from DeepSWE instructions and verifier reports."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.validation import discover_validation_profiles

from . import SCHEMA_VERSION

_F2P_PREFIX = re.compile(r"^\[f2p\]\s*", re.IGNORECASE)


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


def discover_verifier_reports(
    runs_dirs: tuple[Path, ...] | list[Path] = (),
) -> dict[str, list[Path]]:
    reports: dict[str, list[Path]] = defaultdict(list)
    seen: set[Path] = set()
    for runs_dir in runs_dirs:
        if not runs_dir.is_dir():
            raise ValueError(f"runs directory does not exist: {runs_dir}")
        for ctrf_path in sorted(runs_dir.rglob("ctrf.json")):
            resolved = ctrf_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            task_id = _task_id_for_report(ctrf_path)
            if task_id:
                reports[task_id].append(ctrf_path)
    return reports


def collect_task_record(
    task_dir: Path,
    *,
    verifier_reports: tuple[Path, ...] = (),
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Read only task metadata, instruction, validation config, and verifier labels."""
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

    cases: dict[str, dict[str, Any]] = {}
    report_sources: list[str] = []
    report_count = 0
    saw_ctrf = False
    saw_feature_marker = False
    for report_path in verifier_reports:
        report_count += 1
        report_sources.append(str(report_path.resolve()))
        report = load_json(report_path)
        saw_ctrf = True
        results = report.get("results", {}) if isinstance(report, dict) else {}
        tests = results.get("tests", []) if isinstance(results, dict) else []
        if not isinstance(tests, list):
            continue
        for test in tests:
            if not isinstance(test, dict):
                continue
            full_name = str(test.get("name", ""))
            match = _F2P_PREFIX.match(full_name)
            if match is None:
                continue
            saw_feature_marker = True
            name = full_name[match.end() :].strip()
            if not name:
                continue
            case = cases.setdefault(
                name,
                {
                    "id": name,
                    "test_name": name,
                    "behavior_group_id": re.sub(r"(?:\[[^\]]*\])+$", "", name),
                    "observed_statuses": [],
                    "behavior_status": "needs_review",
                },
            )
            status = str(test.get("status", "unknown")).casefold()
            if status not in case["observed_statuses"]:
                case["observed_statuses"].append(status)

    if saw_feature_marker:
        availability = "individual_tests"
    elif saw_ctrf:
        availability = "aggregate_or_no_f2p_marker"
    else:
        availability = "unavailable"

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
            "cases": sorted(cases.values(), key=lambda item: item["test_name"]),
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
            "ground_truth_sources": report_sources,
            "verifier_report_count": report_count,
        },
    }


def collect_records(
    *,
    tasks_dir: Path,
    runs_dirs: tuple[Path, ...] | list[Path],
    repository_roots: dict[str, Path],
    output_dir: Path,
) -> dict[str, Any]:
    report_map = discover_verifier_reports(runs_dirs)
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen_task_ids: set[str] = set()
    for task_dir in discover_task_dirs(tasks_dir):
        provisional_id = _task_id_for_dir(task_dir)
        record = collect_task_record(
            task_dir,
            verifier_reports=tuple(report_map.get(provisional_id, ())),
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
    validation_profiles = Counter(
        str(profile.get("name", "unknown"))
        for record in records
        for profile in record["input"].get("validation", [])
    )
    tasks_with_cases = sum(bool(record["ground_truth"]["cases"]) for record in records)
    return {
        "schema_version": "deepswe-test-prediction-audit-v1",
        "unique_task_count": len({record["task_id"] for record in records}),
        "task_ids": sorted({record["task_id"] for record in records}),
        "tasks_with_individual_feature_tests": sum(
            record["ground_truth"]["availability"] == "individual_tests"
            for record in records
        ),
        "tasks_with_any_labeled_cases": tasks_with_cases,
        "ground_truth_availability": dict(sorted(availability.items())),
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
                "verifier_report_count": record["provenance"]["verifier_report_count"],
            }
            for record in sorted(records, key=lambda item: item["task_id"])
        ],
    }


def _task_id_for_report(ctrf_path: Path) -> str | None:
    task_root = ctrf_path.parent.parent
    config_path = task_root / "config.json"
    if config_path.is_file():
        try:
            config = load_json(config_path)
            task = config.get("task", {}) if isinstance(config, dict) else {}
            task_path = task.get("path") if isinstance(task, dict) else None
            if task_path:
                return Path(str(task_path)).name
        except (OSError, json.JSONDecodeError):
            pass
    trial_name = task_root.name
    return trial_name.split("__", 1)[0] if trial_name else None


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
