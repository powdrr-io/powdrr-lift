#!/usr/bin/env python3
"""Build root-disposition examples using Powdrr's real source classifiers.

This focused harness reuses the production instruction ledger, atomicity
contracts, disposition request construction, semantic decision validation, and
structured LLM transport. It intentionally stops before dependent semantic
decisions and full design generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powdrr_lift.core.instruction_ledger import (
    apply_atomicity_decisions,
    compile_instruction_ledger,
)
from powdrr_lift.core.semantic_decision import DECISION_VALUES
from powdrr_lift.workrr.provider_config import default_llm_mappings
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.semantic_contract_compiler import (
    bind_source_semantic_decisions,
    prepare_source_semantic_decisions,
)

ROOT_DIR = Path(__file__).resolve().parents[3]
DATASET_SCHEMA = "semantic-classifier-example-v1"
ATOMICITY_INSTRUCTIONS = (
    "Answer true only when this clause requires two or more behaviors, APIs, "
    "constraints, lifecycle events, error cases, or observable results that "
    "could be verified separately.",
    "Answer false when the clause expresses one behavior with its necessary "
    "condition, value, or outcome.",
    "Do not rewrite, classify, design, prioritize, or propose implementation work.",
    "Do not return IDs, references, selectors, hashes, fingerprints, or statements.",
    'Example: "On entry, data initializes as a fresh copy of the defaults." '
    "contains one requirement, so multiple is false.",
    'Example: "On exit, data is removed. Re-entering a state resets data to '
    'the original defaults." is already two clauses and each is judged '
    "separately; do not combine neighboring clauses.",
    'Example: "set_state_data validates active state, declared key, and '
    'DataVar type constraints, raising InvalidDefinition on violation." '
    "contains independently verifiable validations and an error outcome, so "
    "multiple is true.",
)
SPLIT_INSTRUCTIONS = (
    "Return an ordered list of 2 to 8 short statements, and nothing else.",
    "Each statement must express exactly one behavior, API result, constraint, "
    "lifecycle event, error condition, or prohibition that can be verified separately.",
    "Preserve every concrete API name, value, condition, constraint, and required "
    "error from the source clause.",
    "Do not add requirements, choose implementation details, weaken a requirement, "
    "or infer an opposite behavior.",
    "Do not return IDs, references, selectors, hashes, fingerprints, source spans, "
    "or parent fields; Powdrr assigns structure.",
    'Example source: "set_state_data validates active state, declared key, and '
    'DataVar type constraints, raising InvalidDefinition on violation."',
    'Example statements: ["set_state_data rejects an inactive state.", '
    '"set_state_data rejects an undeclared key.", "set_state_data enforces the '
    'declared DataVar type constraint.", "An invalid set_state_data call raises '
    'InvalidDefinition."]',
)


def _json_schema_for_root() -> dict[str, Any]:
    values = sorted(DECISION_VALUES["disposition"])
    reasons = [
        "source_ambiguous",
        "source_underspecified",
        "no_candidate",
        "multiple_candidates",
        "repository_evidence_missing",
        "unsupported_concept",
        "classifier_abstained",
        "invalid_response",
        "conflicting_evidence",
    ]
    return {
        "type": "object",
        "required": ["status", "value", "reason_code"],
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["resolved", "unresolved"]},
            "value": {"type": ["string", "null"], "enum": [*values, None]},
            "reason_code": {
                "type": ["string", "null"],
                "enum": [*reasons, None],
            },
        },
    }


def _call_judge(
    client: WorkrrProcedrrClient,
    *,
    question: str,
    instructions: tuple[str, ...] | list[str],
    context_name: str,
    context_value: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    context_text = json.dumps(
        {context_name: context_value}, ensure_ascii=False, sort_keys=True, indent=2
    )
    messages = [
        {"role": "system", "content": "Return only the declared JSON object."},
        {
            "role": "user",
            "content": (
                "Instructions:\n- "
                + "\n- ".join(instructions)
                + "\n\nQuestion:\n"
                + question
                + "\n\nContext:\n"
                + context_text
            ),
        },
    ]
    return client.complete_json(messages, response_schema=schema)


def _atomicity_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["multiple"],
        "additionalProperties": False,
        "properties": {"multiple": {"type": "boolean"}},
    }


def _split_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["statements"],
        "additionalProperties": False,
        "properties": {
            "statements": {
                "type": "array",
                "minItems": 2,
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            }
        },
    }


def _read_python_tasks(tasks_dir: Path, task_ids: set[str] | None) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for metadata_path in sorted(tasks_dir.glob("*/task.toml")):
        try:
            metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"cannot read {metadata_path}: {error}") from error
        details = metadata.get("metadata", {})
        if not isinstance(details, Mapping) or details.get("language") != "python":
            continue
        task_id = details.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"task metadata has no task_id: {metadata_path}")
        if task_ids is not None and task_id not in task_ids:
            continue
        instruction_path = metadata_path.parent / "instruction.md"
        if not instruction_path.is_file():
            raise ValueError(f"Python task has no instruction.md: {metadata_path}")
        tasks.append(
            {
                "task_id": task_id,
                "task_dir": metadata_path.parent,
                "task_metadata": details,
                "instruction_path": instruction_path,
                "instruction": instruction_path.read_text(encoding="utf-8"),
            }
        )
    if task_ids is not None:
        found = {item["task_id"] for item in tasks}
        missing = task_ids - found
        if missing:
            raise ValueError(f"requested task IDs are not Python tasks: {sorted(missing)}")
    return tasks


def _source_ref(task: Mapping[str, Any]) -> str:
    return f"deepswe:{task['task_id']}:instruction.md"


def _repository_family(repository_url: Any, task_id: str) -> str:
    if not isinstance(repository_url, str) or not repository_url.strip():
        return f"family:deepswe-task:{task_id}"
    normalized = repository_url.strip().rstrip("/").removesuffix(".git").casefold()
    return f"family:deepswe-repository:{normalized}"


def _label_task(
    task: Mapping[str, Any],
    client: WorkrrProcedrrClient,
    *,
    provider: str,
    model: str,
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    work_item_name = f"deepswe-{task_id}"
    instruction = str(task["instruction"])
    ledger = compile_instruction_ledger(work_item_name, instruction)
    atomicity: dict[str, dict[str, Any]] = {}
    for clause in ledger.clauses:
        answer = _call_judge(
            client,
            question=(
                "Does this one instruction clause contain more than one "
                "independently verifiable requirement?"
            ),
            instructions=ATOMICITY_INSTRUCTIONS,
            context_name="atomicity_clause",
            context_value=clause.to_data(),
            schema=_atomicity_schema(),
        )
        decision = {"multiple": answer["multiple"]}
        if decision["multiple"]:
            split = _call_judge(
                client,
                question=(
                    "What are the smallest independently verifiable requirements "
                    "contained in this one instruction clause?"
                ),
                instructions=SPLIT_INSTRUCTIONS,
                context_name="atomicity_split_request",
                context_value={"clause": clause.to_data()},
                schema=_split_schema(),
            )
            decision["statements"] = split["statements"]
        atomicity[clause.clause_id] = decision
    atomic_ledger = apply_atomicity_decisions(ledger, atomicity)

    root_records = []
    for clause in atomic_ledger.clauses:
        clause_data = clause.to_data()
        request_plan = prepare_source_semantic_decisions(clause_data)
        if request_plan["resolved_decisions"]:
            raise ValueError("root classifier unexpectedly returned pre-resolved labels")
        if len(request_plan["pending_specs"]) != 1:
            raise ValueError("root classifier must produce exactly one pending request")
        request = request_plan["pending_specs"][0]
        root_instructions = [
            "Follow the request's question, instructions, and allowed_values exactly.",
            "This is the root of a decision tree. Do not classify polarity, strength, "
            "behavior family, or modifiers here.",
            "Return resolved with exactly one allowed value when the source supports it.",
            "Return unresolved with a closed reason_code when the source does not "
            "support one value.",
            "Never return IDs, evidence, provenance, source spans, explanations, "
            "confidence, or additional fields.",
            *request["instructions"],
        ]
        response = _call_judge(
            client,
            question=str(request["question"]),
            instructions=root_instructions,
            context_name="root_decision_request",
            context_value=request,
            schema=_json_schema_for_root(),
        )
        bound = bind_source_semantic_decisions(
            resolved_decisions=request_plan["resolved_decisions"],
            pending_specs=[request],
            provider_results=[response],
        )
        root = next(item for item in bound if item.decision_kind == "disposition")
        root_records.append(
            {
                "clause": clause_data,
                "decision": root.to_data(),
                "spec": request["spec"],
                "teacher_response": response,
                "confidence": None,
            }
        )
    return {
        "schema_version": "root-disposition-task-run-v1",
        "task_id": task_id,
        "task_name": str(task["task_metadata"].get("name", task_id)),
        "repository_url": task["task_metadata"].get("repository_url"),
        "source_family_id": _repository_family(
            task["task_metadata"].get("repository_url"), task_id
        ),
        "source_ref": _source_ref(task),
        "source_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "source_instruction": instruction,
        "instruction_ledger": ledger.to_data(),
        "atomicity_decisions": atomicity,
        "atomic_instruction_ledger": atomic_ledger.to_data(),
        "root_dispositions": root_records,
        "label_origin": "planning-llm-teacher",
        "label_status": "silver_unadjudicated",
        "provider": provider,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _example(task_run: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(task_run["task_id"])
    clause = record["clause"]
    decision = record["decision"]
    result = decision["result"]
    proposition = clause["text"]
    return {
        "schema_version": DATASET_SCHEMA,
        "example_id": f"example:root-disposition:{task_id}:{clause['clause_id']}",
        "decision_kind": "disposition",
        "decision_contract_revision": record["spec"]["contract_revision"],
        "inputs": {
            "proposition": proposition,
            "parent_context": clause.get("parent_clause_id"),
            "left_text": None,
            "right_text": None,
            "candidate_evidence": None,
        },
        "labels": {
            "class": result["value"],
            "answerable": result["status"] == "resolved",
            "reason_code": result["reason_code"],
            "confidence": None,
        },
        "source": {
            "origin": "deepswe_task_instruction",
            "source_ref": task_run["source_ref"],
            "source_sha256": task_run["source_sha256"],
            "source_family_id": task_run["source_family_id"],
            "task_id": task_id,
            "task_name": task_run["task_name"],
            "repository_url": task_run.get("repository_url"),
            "clause_id": clause["clause_id"],
            "parent_clause_id": clause.get("parent_clause_id"),
            "source_span": clause["source_span"],
            "derivation": clause["derivation"],
        },
        "adjudication": {
            "status": "silver_unadjudicated",
            "label_source": "planning-llm-teacher",
            "adjudicators": [],
            "resolution": None,
            "notes_ref": None,
        },
        "license": "not_declared_in_deepswe_task_metadata",
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _consolidate(output_dir: Path, tasks_dir: Path) -> int:
    examples: list[dict[str, Any]] = []
    task_runs = sorted((output_dir / "task_runs").glob("*/task-run.json"))
    for path in task_runs:
        run = json.loads(path.read_text(encoding="utf-8"))
        if run.get("schema_version") != "root-disposition-task-run-v1":
            raise ValueError(f"unsupported task run artifact: {path}")
        task_metadata_path = tasks_dir / str(run["task_id"]) / "task.toml"
        metadata = tomllib.loads(task_metadata_path.read_text(encoding="utf-8"))
        task_details = metadata.get("metadata", {})
        repository_url = (
            task_details.get("repository_url")
            if isinstance(task_details, Mapping)
            else None
        )
        run["repository_url"] = repository_url
        run["source_family_id"] = _repository_family(repository_url, str(run["task_id"]))
        _write_json(path, run)
        examples.extend(_example(run, record) for record in run["root_dispositions"])
    examples_path = output_dir / "root_disposition.jsonl"
    examples_path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        for item in examples
    )
    examples_path.write_text(text, encoding="utf-8")
    manifest = {
        "schema_version": "root-disposition-dataset-manifest-v1",
        "dataset_schema": DATASET_SCHEMA,
        "example_count": len(examples),
        "task_count": len(task_runs),
        "source_family_count": len(
            {item["source"]["source_family_id"] for item in examples}
        ),
        "label_origin": "planning-llm-teacher",
        "label_status": "silver_unadjudicated",
        "confidence_available": False,
        "examples_path": examples_path.name,
        "task_runs": [path.parent.name for path in task_runs],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _write_json(output_dir / "manifest.json", manifest)
    return len(examples)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT_DIR / "science/classifications/root_disposition/data",
    )
    parser.add_argument(
        "--provider",
        choices=("deepinfra", "deepinfra-cheap", "openai", "anthropic", "zai", "openrouter", "local"),
        default="deepinfra-cheap",
    )
    parser.add_argument("--model")
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    tasks = _read_python_tasks(
        args.tasks_dir.resolve(), set(args.task_ids) if args.task_ids else None
    )
    if args.limit is not None:
        tasks = tasks[: args.limit]
    if not tasks:
        parser.error("no Python tasks with instruction.md were found")

    mapping = default_llm_mappings(args.provider)["standard_reasoning"]
    credentials = resolve_provider_credentials(mapping.provider)
    task_runs_dir = args.output_dir / "task_runs"

    def process_task(task: Mapping[str, Any]) -> tuple[str, int, str | None]:
        task_id = str(task["task_id"])
        task_run_path = task_runs_dir / task_id / "task-run.json"
        if task_run_path.is_file():
            return task_id, 0, "reused"
        workflow_client = build_workflow_client(
            credentials,
            model=args.model or mapping.model,
            model_cache_dir=ROOT_DIR / ".powdrr" / "models",
            progress_stream=None,
        )
        task_client = WorkrrProcedrrClient(
            workflow_client,
            skills_dir=ROOT_DIR / "docs/procedrr/skill-definitions",
        )
        try:
            result = _label_task(
                task,
                task_client,
                provider=credentials.provider,
                model=args.model or mapping.model,
            )
        except Exception as error:  # noqa: BLE001 - preserve per-task failures
            _write_json(
                task_runs_dir / task_id / "task-error.json",
                {
                    "schema_version": "root-disposition-task-error-v1",
                    "task_id": task_id,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "failed_at": datetime.now(UTC).isoformat(),
                },
            )
            return task_id, 0, f"{type(error).__name__}: {error}"
        _write_json(task_run_path, result)
        return task_id, len(result["root_dispositions"]), None

    failures = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_task, task): task for task in tasks}
        for index, future in enumerate(as_completed(futures), start=1):
            task_id, label_count, error = future.result()
            if error == "reused":
                print(f"[{index}/{len(tasks)}] reused {task_id}", file=sys.stderr)
            elif error is not None:
                failures.append(task_id)
                print(f"[{index}/{len(tasks)}] failed {task_id}: {error}", file=sys.stderr)
            else:
                print(
                    f"[{index}/{len(tasks)}] saved {label_count} labels for {task_id}",
                    file=sys.stderr,
                )
    count = _consolidate(args.output_dir, args.tasks_dir.resolve())
    manifest = json.loads(
        (args.output_dir / "manifest.json").read_text(encoding="utf-8")
    )
    print(
        f"wrote {count} examples from {manifest['task_count']} complete tasks "
        f"across {manifest['source_family_count']} repository families; "
        f"{len(failures)} failed"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
