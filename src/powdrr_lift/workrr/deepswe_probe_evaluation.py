"""Compare frozen instruction-derived probes with DeepSWE verifier tests."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

PROBE_EVALUATION_SCHEMA_VERSION = "deepswe-probe-evaluation-v1"
SUPPORTED_PROBE_SUITE_SCHEMA_VERSIONS = {
    "instruction-derived-probe-suite-v1",
    "instruction-derived-probe-suite-v2",
    "instruction-derived-probe-suite-v3",
    "instruction-derived-probe-suite-v4",
    "instruction-derived-probe-suite-v5",
    "instruction-derived-probe-suite-v6",
}

_TEST_PATTERNS = (
    re.compile(r"\b(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"\bfunc\s+(Test[A-Z][A-Za-z0-9_]*)\s*\("),
    re.compile(r"\bfn\s+(test_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"\bvoid\s+(test[A-Z][A-Za-z0-9_]*)\s*\("),
    re.compile(r"\b(?:test|it)\s*\(\s*(['\"`])([^'\"`]+)\1"),
)
_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["test_cases", "probe_assessments"],
    "additionalProperties": False,
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["test_id", "behavior", "evaluable", "verifier_evidence"],
                "additionalProperties": False,
                "properties": {
                    "test_id": {"type": "string"},
                    "behavior": {"type": "string"},
                    "evaluable": {"type": "boolean"},
                    "verifier_evidence": {"type": "string"},
                },
            },
        },
        "probe_assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "probe_id",
                    "coverage",
                    "matching_test_ids",
                    "instruction_support",
                    "probe_evidence",
                    "verifier_evidence",
                    "rationale",
                ],
                "additionalProperties": False,
                "properties": {
                    "probe_id": {"type": "string"},
                    "coverage": {
                        "type": "string",
                        "enum": ["exact", "partial", "none", "unjudgeable"],
                    },
                    "matching_test_ids": {"type": "array", "items": {"type": "string"}},
                    "instruction_support": {
                        "type": "string",
                        "enum": ["explicit", "inferred", "unsupported", "unclear"],
                    },
                    "probe_evidence": {"type": "string"},
                    "verifier_evidence": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}


class ProbeEvaluationJudge(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class DeepSWEProbeEvaluationError(ValueError):
    """A probe suite, verifier patch, or judge response is invalid."""


def evaluate_deepswe_probes(
    *,
    task_dir: Path,
    run_dir: Path,
    judge: ProbeEvaluationJudge | None,
    probe_suite_path: Path | None = None,
    judge_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate a frozen probe suite against task verifier tests offline.

    The verifier patch is supplied only to this evaluator, after probes have
    been generated and persisted. It is never sent to the probe generator.
    """
    task_dir = task_dir.resolve()
    run_dir = run_dir.resolve()
    instruction_path = task_dir / "instruction.md"
    metadata_path = task_dir / "task.toml"
    verifier_path = task_dir / "tests" / "test.patch"
    suite_path = probe_suite_path or run_dir / "postrun-probes" / "probe-suite.json"
    suite_path = suite_path.resolve()
    for path in (instruction_path, metadata_path, verifier_path, suite_path):
        if not path.is_file():
            raise DeepSWEProbeEvaluationError(
                f"required evaluation input is missing: {path}"
            )

    instruction = instruction_path.read_text(encoding="utf-8")
    verifier_patch = verifier_path.read_text(encoding="utf-8")
    suite_bytes = suite_path.read_bytes()
    suite = _read_json_bytes(suite_bytes, suite_path)
    if suite.get("schema_version") not in SUPPORTED_PROBE_SUITE_SCHEMA_VERSIONS:
        raise DeepSWEProbeEvaluationError("unsupported generated probe suite")
    if suite.get("source_instruction") != instruction:
        raise DeepSWEProbeEvaluationError(
            "probe suite was generated from different instruction text"
        )
    probes = suite.get("probes")
    if not isinstance(probes, list) or not all(
        isinstance(item, Mapping) for item in probes
    ):
        raise DeepSWEProbeEvaluationError("probe suite has a malformed probes array")
    probe_ids: list[str] = []
    for probe in probes:
        probe_id = probe.get("probe_id")
        if not isinstance(probe_id, str) or not probe_id.strip():
            raise DeepSWEProbeEvaluationError(
                "probe suite contains an invalid probe ID"
            )
        if probe_id in probe_ids:
            raise DeepSWEProbeEvaluationError(
                f"duplicate generated probe ID {probe_id!r}"
            )
        probe_ids.append(probe_id)

    test_ids = _discover_test_ids(verifier_patch)
    if not test_ids:
        raise DeepSWEProbeEvaluationError(
            "no individual verifier test identifiers were found in tests/test.patch"
        )
    try:
        with metadata_path.open("rb") as stream:
            metadata = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeepSWEProbeEvaluationError(
            f"could not read task metadata: {error}"
        ) from error
    task_metadata = metadata.get("metadata", {})
    task_id = (
        task_metadata.get("task_id") if isinstance(task_metadata, Mapping) else None
    )
    if not isinstance(task_id, str) or not task_id:
        raise DeepSWEProbeEvaluationError("DeepSWE task metadata has no task_id")

    if probes:
        if judge is None:
            raise DeepSWEProbeEvaluationError(
                "a judge is required when the frozen suite contains probes"
            )
        judged = _judge_probe_coverage(
            judge,
            instruction=instruction,
            verifier_patch=verifier_patch,
            verifier_test_ids=test_ids,
            probes=probes,
        )
        test_cases = judged["test_cases"]
        probe_assessments = judged["probe_assessments"]
        evaluation_status = "evaluated"
    else:
        test_cases = [
            {
                "test_id": test_id,
                "behavior": (
                    "Not assessed because the frozen suite has no executable probes."
                ),
                "evaluable": False,
                "verifier_evidence": "",
            }
            for test_id in test_ids
        ]
        probe_assessments = []
        evaluation_status = "no_executable_probes"
    evaluable_ids = {
        item["test_id"] for item in test_cases if item["evaluable"] is True
    }
    test_coverage: dict[str, str] = {}
    for test_id in sorted(evaluable_ids):
        statuses = [
            item["coverage"]
            for item in probe_assessments
            if test_id in item["matching_test_ids"]
        ]
        test_coverage[test_id] = (
            "exact"
            if "exact" in statuses
            else "partial"
            if "partial" in statuses
            else "missed"
        )
    exact_cases = sum(status == "exact" for status in test_coverage.values())
    partial_cases = sum(status == "partial" for status in test_coverage.values())
    matched_probes = sum(
        item["coverage"] in {"exact", "partial"} for item in probe_assessments
    )
    exact_probes = sum(item["coverage"] == "exact" for item in probe_assessments)
    summary = {
        "verifier_case_count": len(test_ids),
        "evaluable_verifier_case_count": len(evaluable_ids),
        "unjudgeable_verifier_case_count": len(test_ids) - len(evaluable_ids),
        "probe_count": len(probes),
        "exactly_covered_case_count": exact_cases,
        "partially_covered_case_count": partial_cases,
        "missed_case_count": sum(
            status == "missed" for status in test_coverage.values()
        ),
        "exact_case_recall": _ratio(exact_cases, len(evaluable_ids)),
        "any_case_recall": _ratio(exact_cases + partial_cases, len(evaluable_ids)),
        "exact_probe_precision": _ratio(exact_probes, len(probes)),
        "any_probe_precision": _ratio(matched_probes, len(probes)),
        "unsupported_probe_count": sum(
            item["instruction_support"] == "unsupported" for item in probe_assessments
        ),
        "verifier_unmatched_probe_count": sum(
            item["coverage"] == "none" for item in probe_assessments
        ),
        "unclear_instruction_support_count": sum(
            item["instruction_support"] == "unclear" for item in probe_assessments
        ),
        "judge_error_count": sum(
            item.get("evidence_validation") == "failed"
            for item in [*test_cases, *probe_assessments]
        ),
        "evidence_validation_failure_count": sum(
            item.get("evidence_validation") == "failed"
            for item in [*test_cases, *probe_assessments]
        ),
    }
    report: dict[str, Any] = {
        "schema_version": PROBE_EVALUATION_SCHEMA_VERSION,
        "evaluation_status": evaluation_status,
        "task_id": task_id,
        "probe_suite": {
            "path": str(suite_path),
            "fingerprint": _fingerprint(suite_bytes),
            "generation_status": suite.get("generation_status"),
            "generation_error": suite.get("generation_error"),
            "unprobed_scenario_ids": suite.get(
                "scenarios_without_executable_probe", []
            ),
        },
        "ground_truth": {
            "source_path": str(verifier_path),
            "fingerprint": _fingerprint(verifier_patch.encode("utf-8")),
            "test_ids": test_ids,
            "availability": "individual_tests",
        },
        "judge_id": judge_id,
        "test_cases": test_cases,
        "probe_assessments": probe_assessments,
        "test_coverage": test_coverage,
        "summary": summary,
    }
    report["fingerprint"] = _fingerprint(
        json.dumps(report, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return report


def _judge_probe_coverage(
    judge: ProbeEvaluationJudge,
    *,
    instruction: str,
    verifier_patch: str,
    verifier_test_ids: Sequence[str],
    probes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    extraction_schema = {
        "type": "object",
        "required": ["test_cases"],
        "additionalProperties": False,
        "properties": {"test_cases": _JUDGE_SCHEMA["properties"]["test_cases"]},
    }
    cases_by_id: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(verifier_test_ids), 10):
        batch_ids = list(verifier_test_ids[offset : offset + 10])
        extraction_messages = [
            {
                "role": "system",
                "content": (
                    "You are an offline evaluator. Extract the observable behavior "
                    "and expected result asserted by each requested named verifier "
                    "test from the patch. Do not infer behavior from a test name "
                    "alone. "
                    "Mark a test unevaluable when its behavior cannot be established "
                    "from the patch. Return exactly one record for every requested "
                    "test ID. Include an exact quote from its assertion or verifier "
                    "code. Do not inspect or infer from solution code."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "requested_test_ids": batch_ids,
                        "verifier_test_patch": verifier_patch,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        extracted = _call_judge(judge, extraction_messages, extraction_schema)
        raw_cases = extracted.get("test_cases")
        if not isinstance(raw_cases, list):
            raise DeepSWEProbeEvaluationError(
                "judge returned no verifier test inventory"
            )
        for raw in raw_cases:
            if not isinstance(raw, Mapping):
                raise DeepSWEProbeEvaluationError(
                    "judge returned a malformed verifier case"
                )
            test_id = raw.get("test_id")
            behavior = raw.get("behavior")
            evaluable = raw.get("evaluable")
            evidence = raw.get("verifier_evidence")
            if test_id not in batch_ids:
                raise DeepSWEProbeEvaluationError(
                    f"judge returned unexpected verifier test ID {test_id!r}"
                )
            if not isinstance(behavior, str) or not behavior.strip():
                raise DeepSWEProbeEvaluationError(
                    f"judge returned no behavior summary for {test_id!r}"
                )
            if not isinstance(evaluable, bool):
                raise DeepSWEProbeEvaluationError(
                    f"judge returned invalid evaluability for {test_id!r}"
                )
            evidence_valid = isinstance(evidence, str) and (
                not evaluable
                or (bool(evidence) and _quote_in_patch(evidence, verifier_patch))
            )
            case = dict(raw)
            case["evidence_validation"] = "passed" if evidence_valid else "failed"
            if not evidence_valid:
                case["evaluable"] = False
                case["evidence_validation_error"] = (
                    "verifier evidence quote was absent or not found in test.patch"
                )
            if test_id in cases_by_id:
                raise DeepSWEProbeEvaluationError(
                    f"duplicate verifier case {test_id!r}"
                )
            cases_by_id[str(test_id)] = case
    if set(cases_by_id) != set(verifier_test_ids):
        missing = sorted(set(verifier_test_ids) - cases_by_id.keys())
        raise DeepSWEProbeEvaluationError(f"judge omitted verifier cases: {missing}")

    assessment_messages = [
        {
            "role": "system",
            "content": (
                "Compare each frozen generated probe to the verifier behavior "
                "inventory. "
                "A match requires the same observable behavior and expected outcome; "
                "shared keywords are insufficient. A probe that checks only part of a "
                "verifier case is partial. A probe may validly check instruction- "
                "supported behavior absent from the verifier: classify its verifier "
                "coverage as none, but assess instruction support separately. Use "
                "unjudgeable only when comparison cannot be made. Return one "
                "assessment "
                "for every probe ID, quote exact source text from both the probe and "
                "verifier for exact or partial matches, and explain the decision."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source_instruction": instruction,
                    "verifier_cases": list(cases_by_id.values()),
                    "generated_probes": [dict(item) for item in probes],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assessment_schema = {
        "type": "object",
        "required": ["probe_assessments"],
        "additionalProperties": False,
        "properties": {
            "probe_assessments": _JUDGE_SCHEMA["properties"]["probe_assessments"]
        },
    }
    assessed = _call_judge(judge, assessment_messages, assessment_schema)
    raw_assessments = assessed.get("probe_assessments")
    if not isinstance(raw_assessments, list):
        raise DeepSWEProbeEvaluationError("judge returned no probe assessments")

    verifier_test_ids_by_id = {
        item["test_id"] for item in cases_by_id.values() if item["evaluable"] is True
    }
    assessments_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_assessments:
        if not isinstance(raw, Mapping):
            raise DeepSWEProbeEvaluationError(
                "judge returned a malformed probe assessment"
            )
        probe_id = raw.get("probe_id")
        coverage = raw.get("coverage")
        matching = raw.get("matching_test_ids")
        instruction_support = raw.get("instruction_support")
        probe_evidence = raw.get("probe_evidence")
        verifier_evidence = raw.get("verifier_evidence")
        rationale = raw.get("rationale")
        if (
            not isinstance(probe_id, str)
            or coverage not in {"exact", "partial", "none", "unjudgeable"}
            or not isinstance(matching, list)
            or not all(item in verifier_test_ids_by_id for item in matching)
            or instruction_support
            not in {"explicit", "inferred", "unsupported", "unclear"}
            or not isinstance(probe_evidence, str)
            or not isinstance(verifier_evidence, str)
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise DeepSWEProbeEvaluationError("judge returned invalid probe coverage")
        probe = next(
            (item for item in probes if item.get("probe_id") == probe_id), None
        )
        probe_text = "\n".join(str(value) for value in probe.values()) if probe else ""
        if probe is None or probe_id in assessments_by_id:
            raise DeepSWEProbeEvaluationError(
                f"unknown or duplicate probe ID {probe_id!r}"
            )
        probe_quote_valid = _quote_matches(probe_evidence, probe_text)
        verifier_quote_valid = (
            bool(verifier_evidence)
            and bool(matching)
            and (_quote_in_patch(verifier_evidence, verifier_patch))
        )
        evidence_validation = "passed"
        evidence_validation_error = None
        if coverage in {"exact", "partial"} and (
            not matching or not probe_quote_valid or not verifier_quote_valid
        ):
            coverage = "unjudgeable"
            matching = []
            evidence_validation = "failed"
            evidence_validation_error = "coverage evidence quote did not validate"
        if instruction_support in {"explicit", "inferred", "unsupported"} and (
            not probe_quote_valid
        ):
            instruction_support = "unclear"
            evidence_validation = "failed"
            evidence_validation_error = (
                "instruction support quote did not validate against the probe"
            )
        if coverage == "none" and matching:
            raise DeepSWEProbeEvaluationError("unmatched probe lists verifier cases")
        if coverage == "unjudgeable" and matching:
            raise DeepSWEProbeEvaluationError("unjudgeable probe lists verifier cases")
        assessment = dict(raw)
        assessment["coverage"] = coverage
        assessment["matching_test_ids"] = matching
        assessment["instruction_support"] = instruction_support
        assessment["evidence_validation"] = evidence_validation
        if evidence_validation_error:
            assessment["evidence_validation_error"] = evidence_validation_error
        assessments_by_id[probe_id] = assessment
    if set(assessments_by_id) != set(item["probe_id"] for item in probes):
        raise DeepSWEProbeEvaluationError("judge omitted one or more generated probes")
    return {
        "test_cases": [cases_by_id[item] for item in verifier_test_ids],
        "probe_assessments": [
            assessments_by_id[item] for item in (p["probe_id"] for p in probes)
        ],
    }


def _call_judge(
    judge: ProbeEvaluationJudge,
    messages: list[dict[str, str]],
    schema: Mapping[str, Any],
) -> Mapping[str, Any]:
    complete = judge.complete_json
    try:
        try:
            response = complete(messages, response_schema=schema)  # type: ignore[call-arg]
        except TypeError:
            response = complete(messages)
    except Exception as error:
        raise DeepSWEProbeEvaluationError(
            f"offline probe evaluator judge failed: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(response, Mapping):
        raise DeepSWEProbeEvaluationError("judge returned a non-object response")
    return response


def _discover_test_ids(patch: str) -> list[str]:
    current_path: str | None = None
    identifiers: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current_path = line.removeprefix("+++ b/")
            continue
        if not line.startswith(("+", " ")) or line.startswith("+++"):
            continue
        code = line[1:]
        for pattern in _TEST_PATTERNS:
            match = pattern.search(code)
            if match:
                name = match.group(2) if match.lastindex == 2 else match.group(1)
                identifier = f"{current_path or 'tests/test.patch'}::{name}"
                if identifier not in identifiers:
                    identifiers.append(identifier)
    return identifiers


def _quote_in_patch(quote: str, patch: str) -> bool:
    if quote in patch:
        return True
    source_lines = [
        line[1:]
        for line in patch.splitlines()
        if line.startswith(("+", " ")) and not line.startswith("+++")
    ]

    def normalize(value: str) -> str:
        return " ".join(value.split())

    normalized_source = normalize("\n".join(source_lines))
    return bool(normalize(quote)) and normalize(quote) in normalized_source


def _quote_matches(quote: str, source: str) -> bool:
    if not quote:
        return False
    if quote in source:
        return True
    return " ".join(quote.split()) in " ".join(source.split())


def _read_json_bytes(data: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeepSWEProbeEvaluationError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise DeepSWEProbeEvaluationError(f"expected a JSON object in {path}")
    return value


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


__all__ = [
    "DeepSWEProbeEvaluationError",
    "PROBE_EVALUATION_SCHEMA_VERSION",
    "evaluate_deepswe_probes",
]
