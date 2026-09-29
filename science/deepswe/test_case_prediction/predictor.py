"""Instruction-grounded behavior scenario prediction using Powdrr providers."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from powdrr_lift.core.instruction_ledger import compile_instruction_ledger
from powdrr_lift.workrr.llm import complete_json
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)

from . import PREDICTIONS_SCHEMA_VERSION
from .records import load_task_record

PREDICTOR_PROMPT_VERSION = "test-scenario-baseline-v3"

SYSTEM_PROMPT = """You predict likely observable test scenarios for a software task.
Use only the task instruction and the validation metadata provided in the user
message. Do not infer details from source code, tests, patches, agent plans,
verifier outcomes, or external knowledge of the benchmark. Validation commands
describe how tests run; they rarely identify the behavior being tested.

Extract distinct explicit requirements, then turn them into concrete cases with
given, when, and then fields. Cover normal behavior and only those error,
boundary, compatibility, or interaction cases supported by the instruction.
Rank direct, high-value cases first. Return at most 8 concise scenarios. Keep
given, when, and then to one sentence each. Cite one or two valid
instruction_clause_ids for every case. Select IDs only; do not reproduce or
paraphrase clause text. Mark extrapolations as inferred and explain why. Do not
fabricate exact test function names. Return a JSON object with a single `cases`
array; every case must include subject, given, when, then, category,
instruction_clause_ids, basis, confidence, and inference_rationale. Output JSON
only."""

PREDICTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cases": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "given": {"type": "string", "maxLength": 500},
                    "when": {"type": "string", "maxLength": 300},
                    "then": {"type": "string", "maxLength": 500},
                    "category": {
                        "type": "string",
                        "enum": [
                            "normal",
                            "error",
                            "boundary",
                            "compatibility",
                            "interaction",
                        ],
                    },
                    "instruction_clause_ids": {
                        "type": "array",
                        "maxItems": 2,
                        "items": {"type": "string"},
                    },
                    "basis": {"type": "string", "enum": ["explicit", "inferred"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "inference_rationale": {"type": "string", "maxLength": 500},
                },
                "required": [
                    "subject",
                    "given",
                    "when",
                    "then",
                    "category",
                    "instruction_clause_ids",
                    "basis",
                    "confidence",
                    "inference_rationale",
                ],
            },
        }
    },
    "required": ["cases"],
}


def build_messages(record: dict[str, Any]) -> list[dict[str, str]]:
    task_input = record["input"]
    payload = {
        "instruction": task_input["instruction"],
        "instruction_clauses": [
            {"clause_id": clause.clause_id, "text": clause.text}
            for clause in compile_instruction_ledger(
                "deepswe-task", task_input["instruction"]
            ).clauses
        ],
        "validation": task_input.get("validation", []),
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Predict test scenarios from this pre-implementation task input.\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            ),
        },
    ]


def validate_predictions(
    task_id: str,
    instruction: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    clause_map = {
        clause.clause_id: clause.text
        for clause in compile_instruction_ledger("deepswe-task", instruction).clauses
    }
    raw_cases = response.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(
            "prediction response must contain a cases array; "
            f"returned keys={sorted(response)}, "
            f"cases type={type(raw_cases).__name__}"
        )
    cases: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"prediction case {index} must be an object")
        case: dict[str, Any] = {
            "id": f"pred-{index:03d}",
            "subject": _required_text(raw, "subject", index),
            "given": _required_text(raw, "given", index),
            "when": _required_text(raw, "when", index),
            "then": _required_text(raw, "then", index),
            "category": _required_text(raw, "category", index),
            "instruction_clause_ids": _clause_ids(raw, clause_map, index),
            "basis": _required_text(raw, "basis", index),
            "confidence": _confidence(raw.get("confidence"), index),
            "inference_rationale": str(raw.get("inference_rationale", "")).strip(),
        }
        if case["category"] not in {
            "normal",
            "error",
            "boundary",
            "compatibility",
            "interaction",
        }:
            raise ValueError(f"prediction case {index} has an unsupported category")
        if case["basis"] not in {"explicit", "inferred"}:
            raise ValueError(f"prediction case {index} has an unsupported basis")
        if case["basis"] == "inferred" and not case["inference_rationale"]:
            raise ValueError(f"inferred prediction case {index} requires a rationale")
        identity = (
            re.sub(r"\W+", " ", case["subject"]).strip().casefold(),
            re.sub(r"\W+", " ", case["when"]).strip().casefold(),
            re.sub(r"\W+", " ", case["then"]).strip().casefold(),
        )
        if identity in seen:
            raise ValueError(f"duplicate prediction scenario at case {index}")
        seen.add(identity)
        case["instruction_evidence"] = [
            clause_map[clause_id] for clause_id in case["instruction_clause_ids"]
        ]
        cases.append(case)
    return {
        "schema_version": PREDICTIONS_SCHEMA_VERSION,
        "task_id": task_id,
        "cases": cases,
    }


def predict_record(
    record: dict[str, Any],
    *,
    provider: str,
    model: str,
) -> dict[str, Any]:
    credentials = resolve_provider_credentials(provider)
    client = build_workflow_client(
        credentials,
        model=model,
        model_cache_dir=Path(tempfile.gettempdir()) / "powdrr-test-predictor-models",
    )
    messages = build_messages(record)
    response = complete_json(
        client, messages, response_schema=PREDICTION_RESPONSE_SCHEMA
    )
    repair_attempts = 0
    while True:
        try:
            predictions = validate_predictions(
                str(record["task_id"]),
                str(record["input"]["instruction"]),
                response,
            )
            break
        except ValueError as exc:
            if repair_attempts >= 2:
                raise
            repair_attempts += 1
            messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": json.dumps(response, ensure_ascii=False),
                },
                {
                    "role": "user",
                    "content": (
                        "Correct the previous JSON response. The deterministic "
                        "validator rejected it for this reason: "
                        f"{exc}. Keep the same evidence boundary and return a "
                        "complete response matching the required schema."
                    ),
                },
            ]
            response = complete_json(
                client, messages, response_schema=PREDICTION_RESPONSE_SCHEMA
            )
    input_text = json.dumps(
        {
            "instruction": record["input"]["instruction"],
            "validation": record["input"].get("validation", []),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    predictions.update(
        {
            "provider": provider,
            "model": model,
            "prompt_version": PREDICTOR_PROMPT_VERSION,
            "repair_attempts": repair_attempts,
            "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
        }
    )
    return predictions


def predict_file(
    record_path: Path,
    *,
    provider: str,
    model: str,
) -> dict[str, Any]:
    return predict_record(load_task_record(record_path), provider=provider, model=model)


def write_predictions(predictions: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(predictions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _required_text(raw: dict[str, Any], field: str, index: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"prediction case {index} requires non-empty {field}")
    return value.strip()


def _clause_ids(
    raw: dict[str, Any], clause_map: dict[str, str], index: int
) -> list[str]:
    values = raw.get("instruction_clause_ids")
    if not isinstance(values, list) or not values:
        raise ValueError(f"prediction case {index} requires instruction clause IDs")
    clause_ids = []
    for value in values:
        if not isinstance(value, str) or value not in clause_map:
            raise ValueError(
                f"prediction case {index} cites an unknown instruction clause"
            )
        clause_ids.append(value)
    return list(dict.fromkeys(clause_ids))


def _confidence(value: Any, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"prediction case {index} confidence must be numeric")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"prediction case {index} confidence must be in [0, 1]")
    return result
