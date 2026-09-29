"""Instruction-grounded behavior scenario prediction using Powdrr providers."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from powdrr_lift.core.instruction_ledger import compile_instruction_ledger
from powdrr_lift.errors import ProviderExecutionError
from powdrr_lift.workrr.llm import complete_json
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)

from . import PREDICTIONS_SCHEMA_VERSION
from .records import load_task_record

PREDICTOR_PROMPT_VERSION = "test-case-staged-v3"
FULL_SET_STRUCTURED_OUTPUT_TOKENS = 16_384

COUNT_PROMPT = """Estimate how many new test cases the patch for this task will add.
Use only the task instruction and the validation metadata provided in the user
Predict the number of test cases, not the number of assertions, requirements,
or behavior obligations. Count a test function/block as one case, and count a
separately executed named table row or explicit parameter row as one case.
Group related checks that share setup and verify one contract into one likely
test, even when the instruction lists several invalid values or parsing rules.
Split cases when the API mode, setup/state transition, or expected outcome is
materially different. Do not count fixtures, helpers, or setup-only changes.
Estimate the likely patch's test-case count and plausible lower and upper
bounds; use realistic grouping by a test author instead of expanding every
condition into its own test. When two counts are similarly plausible, lean
slightly higher because underpredicting the patch size is more costly than a
modest overprediction. Keep the estimate grounded in likely test blocks; do not
inflate it to one test per clause or obligation. Output JSON only."""

OBLIGATION_PROMPT = """Enumerate the distinct observable behavior obligations that
the new tests should verify. Use only the task instruction and validation
metadata. Do not infer details from source code, tests, patches, plans, verifier
outcomes, or external benchmark knowledge. Validation commands indicate test
scope or mechanism, not behavior.

An obligation is one independently observable contract or state transition,
not one input permutation. Include all explicit requirements and only necessary,
well-grounded inferences. Keep distinct outcomes separate. Do not create generic
best-practice checks. Use the supplied test-count forecast as a rough size
signal: obligations can have multiple cases, and a case can support related
obligations, so do not force the obligation count to equal the test count.
Return the complete obligation inventory with a concise behavior, cited
instruction_clause_ids, basis, and rationale for each. Output JSON only."""

TEST_CASE_PROMPT = """Expand the supplied obligations into the complete set of
concrete test cases likely to be added by the patch. Use only the task
instruction, validation metadata, count forecast, and obligation inventory.
Do not infer details from source code, tests, patches, plans, verifier outcomes,
or external benchmark knowledge.

Create one case for each likely added test function/block and each separately
executed parameter or named table row when it represents a distinct test. Cover
all obligations and meaningful input classes, outcomes, API modes, and state
transitions. Missing a distinct, well-grounded behavior is more costly than
including one additional grounded case, so favor complete coverage when choosing
between plausible cases. Do not add speculative behavior or unsupported cases.
Avoid speculative cross-products and duplicate cases. Use the
count forecast as the predicted patch size: return exactly the forecast number
of cases. Do not pad the set with unsupported behavior or omit an obligation;
choose the most likely grouping and parameterization of the tests. The forecast
is task-specific and has no fixed global limit. Cite one or more obligation IDs
and valid instruction_clause_ids for each case. Mark grounded extrapolations
as inferred with a rationale. Do not invent test function names. Output JSON
only."""

TEST_COUNT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "estimated_test_case_count": {"type": "integer", "minimum": 0},
        "lower_bound": {"type": "integer", "minimum": 0},
        "upper_bound": {"type": "integer", "minimum": 0},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": [
        "estimated_test_case_count",
        "lower_bound",
        "upper_bound",
        "confidence",
        "rationale",
    ],
}

OBLIGATIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "obligations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "behavior": {"type": "string"},
                    "instruction_clause_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "basis": {"type": "string", "enum": ["explicit", "inferred"]},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "behavior",
                    "instruction_clause_ids",
                    "basis",
                    "rationale",
                ],
            },
        }
    },
    "required": ["obligations"],
}

TEST_CASES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "given": {"type": "string"},
                    "when": {"type": "string"},
                    "then": {"type": "string"},
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
                        "items": {"type": "string"},
                    },
                    "obligation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "basis": {"type": "string", "enum": ["explicit", "inferred"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "inference_rationale": {"type": "string"},
                },
                "required": [
                    "subject",
                    "given",
                    "when",
                    "then",
                    "category",
                    "instruction_clause_ids",
                    "obligation_ids",
                    "basis",
                    "confidence",
                    "inference_rationale",
                ],
            },
        }
    },
    "required": ["cases"],
}


def build_test_cases_response_schema(estimated_count: int) -> dict[str, Any]:
    """Constrain this stage to the separately predicted test-set size."""
    schema = json.loads(json.dumps(TEST_CASES_SCHEMA))
    schema["properties"]["cases"]["minItems"] = estimated_count
    schema["properties"]["cases"]["maxItems"] = estimated_count
    return schema


def build_messages(
    record: dict[str, Any], test_count: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """Build the obligation-stage request from pre-implementation inputs."""
    messages = _stage_messages(record, OBLIGATION_PROMPT, "Enumerate obligations.")
    if test_count is not None:
        messages[1]["content"] += "\nTest-count forecast:\n" + json.dumps(
            test_count, ensure_ascii=False
        )
    return messages


def build_count_messages(record: dict[str, Any]) -> list[dict[str, str]]:
    return _stage_messages(record, COUNT_PROMPT, "Estimate the test-case count.")


def build_case_messages(
    record: dict[str, Any],
    test_count: dict[str, Any],
    obligations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    base = _input_payload(record)
    context = {
        **base,
        "test_count_forecast": test_count,
        "obligations": [
            {
                key: value
                for key, value in obligation.items()
                if key != "instruction_evidence"
            }
            for obligation in obligations
        ],
    }
    return [
        {"role": "system", "content": TEST_CASE_PROMPT},
        {
            "role": "user",
            "content": "Expand the obligations into test cases from this input.\n"
            + json.dumps(context, ensure_ascii=False, indent=2),
        },
    ]


def _stage_messages(
    record: dict[str, Any], system_prompt: str, instruction: str
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": instruction
            + "\n"
            + json.dumps(_input_payload(record), ensure_ascii=False, indent=2),
        },
    ]


def _input_payload(record: dict[str, Any]) -> dict[str, Any]:
    task_input = record["input"]
    return {
        "instruction": task_input["instruction"],
        "instruction_clauses": [
            {"clause_id": clause.clause_id, "text": clause.text}
            for clause in compile_instruction_ledger(
                "deepswe-task", task_input["instruction"]
            ).clauses
        ],
        "validation": task_input.get("validation", []),
    }


def validate_test_count(response: dict[str, Any]) -> dict[str, Any]:
    estimate = _nonnegative_int(
        response.get("estimated_test_case_count"), "estimated_test_case_count"
    )
    lower = _nonnegative_int(response.get("lower_bound"), "lower_bound")
    upper = _nonnegative_int(response.get("upper_bound"), "upper_bound")
    if lower > estimate or estimate > upper:
        raise ValueError("test-count forecast must fall within its bounds")
    rationale = response.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("test-count forecast requires a rationale")
    return {
        "estimated_test_case_count": estimate,
        "lower_bound": lower,
        "upper_bound": upper,
        "confidence": _confidence(response.get("confidence"), 1),
        "rationale": rationale.strip(),
    }


def validate_obligations(
    instruction: str, response: dict[str, Any]
) -> list[dict[str, Any]]:
    clause_map = {
        clause.clause_id: clause.text
        for clause in compile_instruction_ledger("deepswe-task", instruction).clauses
    }
    raw_obligations = response.get("obligations")
    if not isinstance(raw_obligations, list):
        raise ValueError("obligation response must contain an obligations array")
    obligations = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_obligations, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"obligation {index} must be an object")
        behavior = raw.get("behavior")
        if not isinstance(behavior, str) or not behavior.strip():
            raise ValueError(f"obligation {index} requires a behavior")
        clause_ids = _clause_ids(raw, clause_map, index)
        basis = raw.get("basis")
        rationale = raw.get("rationale")
        if basis not in {"explicit", "inferred"}:
            raise ValueError(f"obligation {index} has an unsupported basis")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"obligation {index} requires a rationale")
        identity = re.sub(r"\W+", " ", behavior).strip().casefold()
        if identity in seen:
            raise ValueError(f"duplicate obligation at item {index}")
        seen.add(identity)
        obligations.append(
            {
                "id": f"obligation-{index:03d}",
                "behavior": behavior.strip(),
                "instruction_clause_ids": clause_ids,
                "instruction_evidence": [clause_map[item] for item in clause_ids],
                "basis": basis,
                "rationale": rationale.strip(),
            }
        )
    return obligations


def validate_predictions(
    task_id: str,
    instruction: str,
    response: dict[str, Any],
    *,
    obligation_ids: set[str] | None = None,
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
    seen: set[tuple[str, str, str, str]] = set()
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
            "obligation_ids": _obligation_ids(raw, obligation_ids, index),
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
            re.sub(r"\W+", " ", case["given"]).strip().casefold(),
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
    instruction = str(record["input"]["instruction"])
    test_count, count_repairs = _complete_stage(
        client,
        build_count_messages(record),
        TEST_COUNT_SCHEMA,
        validate_test_count,
        stage_name="test-count forecast",
    )
    obligations, obligation_repairs = _complete_stage(
        client,
        build_messages(record, test_count),
        OBLIGATIONS_SCHEMA,
        lambda response: validate_obligations(instruction, response),
        stage_name="obligation inventory",
    )
    obligation_by_id = {item["id"]: item for item in obligations}
    set_output_limit = getattr(client, "set_structured_output_token_limit", None)
    if callable(set_output_limit):
        set_output_limit(FULL_SET_STRUCTURED_OUTPUT_TOKENS)
    predictions, case_repairs = _complete_stage(
        client,
        build_case_messages(record, test_count, obligations),
        build_test_cases_response_schema(test_count["estimated_test_case_count"]),
        lambda response: validate_predictions(
            str(record["task_id"]),
            instruction,
            response,
            obligation_ids=set(obligation_by_id),
        ),
        stage_name="test-case set",
    )
    for case in predictions["cases"]:
        case["obligation_evidence"] = [
            {
                "obligation_id": obligation_id,
                "behavior": obligation_by_id[obligation_id]["behavior"],
            }
            for obligation_id in case["obligation_ids"]
        ]
    predictions.update(
        {"test_count_prediction": test_count, "obligations": obligations}
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
            "repair_attempts": count_repairs + obligation_repairs + case_repairs,
            "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
        }
    )
    return predictions


def _complete_stage(
    client: Any,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    validator: Any,
    *,
    stage_name: str,
) -> tuple[Any, int]:
    response = _request_stage(client, messages, schema)
    for repair_attempt in range(3):
        try:
            return validator(response), repair_attempt
        except ValueError as exc:
            if repair_attempt == 2:
                raise
            messages = [
                *messages,
                {"role": "assistant", "content": json.dumps(response)},
                {
                    "role": "user",
                    "content": (
                        f"Correct the {stage_name} JSON response. The local "
                        f"validator rejected it: {exc}. Preserve the requested "
                        "evidence boundary and return a complete response "
                        "matching the schema."
                    ),
                },
            ]
            response = _request_stage(client, messages, schema)
    raise AssertionError("unreachable stage validation loop")


def _request_stage(
    client: Any, messages: list[dict[str, str]], schema: dict[str, Any]
) -> dict[str, Any]:
    for retry in range(2):
        try:
            return complete_json(client, messages, response_schema=schema)
        except ProviderExecutionError:
            if retry:
                raise
    raise AssertionError("unreachable provider retry loop")


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


def _obligation_ids(
    raw: dict[str, Any], obligation_ids: set[str] | None, index: int
) -> list[str]:
    if obligation_ids is None:
        return []
    values = raw.get("obligation_ids")
    if not isinstance(values, list) or not values:
        raise ValueError(f"prediction case {index} requires obligation IDs")
    result = []
    for value in values:
        if not isinstance(value, str) or value not in obligation_ids:
            raise ValueError(f"prediction case {index} cites an unknown obligation")
        if value not in result:
            result.append(value)
    return result


def _confidence(value: Any, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"prediction case {index} confidence must be numeric")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"prediction case {index} confidence must be in [0, 1]")
    return result


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value
