"""Compare a DeepSWE design-only run with task-grounded behavior references."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import yaml

DEFAULT_STATE_DATA_RUBRIC = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "evaluations"
    / "deepswe-python-statemachine-state-data.yaml"
)

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "evidence_quote", "reason"],
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["supported", "missing", "contradicted", "ambiguous"],
        },
        "evidence_quote": {"type": "string"},
        "reason": {"type": "string", "minLength": 1},
    },
}


class DesignJudge(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class DeepSWEEvaluationError(ValueError):
    """A task reference, run artifact, or judge response is invalid."""


def evaluate_deepswe_design(
    *,
    task_dir: Path,
    run_dir: Path,
    judge: DesignJudge,
    rubric_path: Path = DEFAULT_STATE_DATA_RUBRIC,
    judge_id: str | None = None,
) -> dict[str, Any]:
    """Grade canonical design projections against one DeepSWE task's references.

    The judge receives curated claims and small references to the solution and
    verifier tests. These inputs are used only here, after design generation.
    """
    task_dir = task_dir.resolve()
    run_dir = run_dir.resolve()
    rubric_path = rubric_path.resolve()
    instruction_path = task_dir / "instruction.md"
    task_metadata_path = task_dir / "task.toml"
    solution_path = task_dir / "solution" / "solution.patch"
    verifier_path = task_dir / "tests" / "test.patch"
    ledger_path = run_dir / "instruction-ledger.json"
    design_path = run_dir / "canonical-feature-design.json"
    for path in (
        instruction_path,
        task_metadata_path,
        solution_path,
        verifier_path,
        ledger_path,
        design_path,
        rubric_path,
    ):
        if not path.is_file():
            raise DeepSWEEvaluationError(
                f"required evaluation input is missing: {path}"
            )

    instruction = instruction_path.read_text(encoding="utf-8")
    solution_patch = solution_path.read_text(encoding="utf-8")
    verifier_patch = verifier_path.read_text(encoding="utf-8")
    ledger = _read_json(ledger_path)
    design = _read_json(design_path)
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    try:
        with task_metadata_path.open("rb") as stream:
            task_metadata = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeepSWEEvaluationError(
            f"could not read DeepSWE task metadata: {error}"
        ) from error

    metadata = task_metadata.get("metadata", {})
    task_id = metadata.get("task_id") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(rubric, Mapping)
        or rubric.get("schema_version") != "deepswe-design-gold-v1"
    ):
        raise DeepSWEEvaluationError("unsupported DeepSWE design rubric")
    expected_task_id = rubric.get("task_id")
    if task_id != expected_task_id:
        raise DeepSWEEvaluationError(
            f"rubric is for {expected_task_id!r}, but task metadata names {task_id!r}"
        )
    if ledger.get("schema_version") != "instruction-ledger-v1":
        raise DeepSWEEvaluationError("unsupported instruction ledger")
    if design.get("schema_version") != "feature-design-v2":
        raise DeepSWEEvaluationError("unsupported canonical feature design")
    ledger_source = ledger.get("source")
    clauses = ledger.get("clauses")
    projections = design.get("projections")
    if not isinstance(ledger_source, Mapping) or not isinstance(clauses, list):
        raise DeepSWEEvaluationError("instruction ledger is incomplete")
    if not isinstance(projections, list):
        raise DeepSWEEvaluationError("canonical feature design has no projections")
    if ledger_source.get("text") != instruction:
        raise DeepSWEEvaluationError(
            "design-only run does not use the task's exact instruction text"
        )
    if design.get("ledger_fingerprint") != ledger.get("fingerprint"):
        raise DeepSWEEvaluationError("canonical feature design uses a different ledger")

    projection_by_clause: dict[str, Mapping[str, Any]] = {}
    for projection in projections:
        if not isinstance(projection, Mapping):
            raise DeepSWEEvaluationError("canonical design projection is malformed")
        clause_id = projection.get("clause_id")
        if not isinstance(clause_id, str) or clause_id in projection_by_clause:
            raise DeepSWEEvaluationError(
                "canonical design has a missing or duplicate clause id"
            )
        projection_by_clause[clause_id] = projection

    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise DeepSWEEvaluationError("rubric has no evaluation criteria")
    findings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    judge_calls = 0
    for criterion in criteria:
        if not isinstance(criterion, Mapping):
            raise DeepSWEEvaluationError("rubric criterion is malformed")
        criterion_id = criterion.get("id")
        excerpt = criterion.get("instruction_excerpt")
        expected_behavior = criterion.get("expected_behavior")
        importance = criterion.get("importance")
        if (
            not isinstance(criterion_id, str)
            or criterion_id in seen_ids
            or not isinstance(excerpt, str)
            or not isinstance(expected_behavior, str)
            or importance not in {"critical", "important"}
        ):
            raise DeepSWEEvaluationError(
                "rubric criterion has invalid identity or fields"
            )
        seen_ids.add(criterion_id)
        if excerpt not in instruction:
            raise DeepSWEEvaluationError(
                f"rubric source excerpt is stale for criterion {criterion_id}"
            )
        symbols = _string_list(criterion, "solution_symbols")
        test_names = _string_list(criterion, "verifier_tests")
        missing_symbols = [
            item for item in symbols if not _patch_has_symbol(solution_patch, item)
        ]
        missing_tests = [item for item in test_names if item not in verifier_patch]
        if missing_symbols or missing_tests:
            raise DeepSWEEvaluationError(
                f"ground-truth references are stale for {criterion_id}: "
                f"solution symbols missing={missing_symbols}, "
                f"verifier tests missing={missing_tests}"
            )

        source_start = instruction.index(excerpt)
        source_end = source_start + len(excerpt)
        matching_clauses = [
            item
            for item in clauses
            if isinstance(item, Mapping)
            and _spans_overlap(item, source_start, source_end)
        ]
        clause_ids = [
            item.get("clause_id")
            for item in matching_clauses
            if isinstance(item.get("clause_id"), str)
        ]
        if not clause_ids:
            raise DeepSWEEvaluationError(
                f"source excerpt did not survive instruction ledger: {criterion_id}"
            )
        selected_projections = [
            projection_by_clause[item]
            for item in clause_ids
            if item in projection_by_clause
        ]
        candidate_text = _render_candidate_projections(selected_projections)
        solution_evidence = _solution_evidence(solution_patch, symbols)
        if not candidate_text:
            decision = {
                "decision": "missing",
                "evidence_quote": "",
                "reason": "No canonical design projection refers to the source span.",
            }
        else:
            judge_calls += 1
            decision = _judge_criterion(
                judge,
                criterion_id=criterion_id,
                instruction_excerpt=excerpt,
                expected_behavior=expected_behavior,
                solution_symbols=symbols,
                solution_evidence=solution_evidence,
                verifier_tests=test_names,
                candidate_text=candidate_text,
            )
            quote = decision["evidence_quote"]
            if quote and quote not in candidate_text:
                decision = {
                    "decision": "ambiguous",
                    "evidence_quote": "",
                    "reason": (
                        "Judge evidence quote was not present in the candidate design."
                    ),
                }
            elif decision["decision"] == "supported" and not quote:
                decision = {
                    "decision": "ambiguous",
                    "evidence_quote": "",
                    "reason": (
                        "Judge marked the criterion supported without a design quote."
                    ),
                }
        findings.append(
            {
                "criterion_id": criterion_id,
                "importance": importance,
                "instruction_excerpt": excerpt,
                "ledger_clause_ids": clause_ids,
                "solution_symbols": symbols,
                "reference_solution_evidence": solution_evidence,
                "verifier_tests": test_names,
                **decision,
            }
        )

    weights = {"critical": 3, "important": 1}
    total_weight = sum(weights[item["importance"]] for item in findings)
    supported_weight = sum(
        weights[item["importance"]]
        for item in findings
        if item["decision"] == "supported"
    )
    report: dict[str, Any] = {
        "schema_version": "deepswe-design-evaluation-v1",
        "task_id": task_id,
        "judge_id": judge_id,
        "design_fingerprint": _content_fingerprint(design),
        "ledger_fingerprint": ledger.get("fingerprint"),
        "reference_fingerprints": {
            "instruction": _sha256(instruction_path),
            "solution_patch": _sha256(solution_path),
            "verifier_patch": _sha256(verifier_path),
            "rubric": _sha256(rubric_path),
        },
        "candidate_fingerprints": {
            "instruction_ledger": _sha256(ledger_path),
            "canonical_design": _sha256(design_path),
        },
        "summary": {
            "criterion_count": len(findings),
            "judge_calls": judge_calls,
            "supported_count": sum(
                item["decision"] == "supported" for item in findings
            ),
            "critical_failures": [
                item["criterion_id"]
                for item in findings
                if item["importance"] == "critical" and item["decision"] != "supported"
            ],
            "weighted_coverage": round(supported_weight / total_weight, 4),
            "passed": all(
                item["decision"] == "supported"
                for item in findings
                if item["importance"] == "critical"
            ),
        },
        "findings": findings,
    }
    report["fingerprint"] = _content_fingerprint(report)
    return report


def evaluate_deepswe_worker_prompt(
    *,
    task_dir: Path,
    run_dir: Path,
    judge: DesignJudge,
    rubric_path: Path = DEFAULT_STATE_DATA_RUBRIC,
    judge_id: str | None = None,
) -> dict[str, Any]:
    """Grade provider-ready worker prompts against one DeepSWE task's references."""
    task_dir = task_dir.resolve()
    run_dir = run_dir.resolve()
    rubric_path = rubric_path.resolve()
    instruction_path = task_dir / "instruction.md"
    metadata_path = task_dir / "task.toml"
    solution_path = task_dir / "solution" / "solution.patch"
    verifier_path = task_dir / "tests" / "test.patch"
    run_metadata_path = run_dir / "run-metadata.json"
    ledger_path = run_dir / "instruction-ledger.json"
    prompt_index_path = run_dir / "artifacts" / "prompts" / "index.json"
    for path in (
        instruction_path,
        metadata_path,
        solution_path,
        verifier_path,
        run_metadata_path,
        ledger_path,
        prompt_index_path,
        rubric_path,
    ):
        if not path.is_file():
            raise DeepSWEEvaluationError(
                f"required prompt-evaluation input is missing: {path}"
            )

    instruction = instruction_path.read_text(encoding="utf-8")
    solution_patch = solution_path.read_text(encoding="utf-8")
    verifier_patch = verifier_path.read_text(encoding="utf-8")
    run_metadata = _read_json(run_metadata_path)
    ledger = _read_json(ledger_path)
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    try:
        with metadata_path.open("rb") as stream:
            task_metadata = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DeepSWEEvaluationError(
            f"could not read DeepSWE task metadata: {error}"
        ) from error

    metadata = task_metadata.get("metadata", {})
    task_id = metadata.get("task_id") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(rubric, Mapping)
        or rubric.get("schema_version") != "deepswe-design-gold-v1"
    ):
        raise DeepSWEEvaluationError("unsupported DeepSWE prompt rubric")
    if task_id != rubric.get("task_id") or run_metadata.get("task_id") != task_id:
        raise DeepSWEEvaluationError(
            "task, prompt run metadata, and rubric must identify the same task"
        )
    if ledger.get("schema_version") != "instruction-ledger-v1":
        raise DeepSWEEvaluationError("unsupported instruction ledger")
    ledger_source = ledger.get("source")
    if (
        not isinstance(ledger_source, Mapping)
        or ledger_source.get("text") != instruction
    ):
        raise DeepSWEEvaluationError(
            "prompt-only run does not use the task's exact instruction text"
        )

    try:
        prompt_records = json.loads(prompt_index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeepSWEEvaluationError(
            f"could not read captured worker prompt index: {error}"
        ) from error
    if not isinstance(prompt_records, list) or not prompt_records:
        raise DeepSWEEvaluationError("prompt-only run captured no worker prompts")
    artifacts_root = (run_dir / "artifacts").resolve()
    prompts: list[dict[str, str]] = []
    seen_request_ids: set[str] = set()
    for record in prompt_records:
        if not isinstance(record, Mapping):
            raise DeepSWEEvaluationError("captured worker prompt index is malformed")
        request_id = record.get("request_id")
        provider = record.get("provider")
        relative_path = record.get("prompt_path")
        prompt_digest = record.get("prompt_sha256")
        if (
            not isinstance(request_id, str)
            or request_id in seen_request_ids
            or Path(request_id).name != request_id
            or not isinstance(provider, str)
            or not isinstance(relative_path, str)
            or not isinstance(prompt_digest, str)
        ):
            raise DeepSWEEvaluationError("captured worker prompt metadata is invalid")
        seen_request_ids.add(request_id)
        prompt_path = (artifacts_root / relative_path).resolve()
        if not prompt_path.is_relative_to(artifacts_root) or not prompt_path.is_file():
            raise DeepSWEEvaluationError(
                f"captured prompt path is missing or escapes artifacts: {relative_path}"
            )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        if "sha256:" + hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() != (
            prompt_digest
        ):
            raise DeepSWEEvaluationError(
                f"captured prompt fingerprint does not match: {relative_path}"
            )
        request_path = artifacts_root / "requests" / f"{request_id}.json"
        request = _read_json(request_path)
        if (
            request.get("request_id") != request_id
            or request.get("prompt") != prompt_text
        ):
            raise DeepSWEEvaluationError(
                f"captured prompt does not match its serialized request: {request_id}"
            )
        prompts.append(
            {
                "request_id": request_id,
                "provider": provider,
                "prompt": prompt_text,
                "fingerprint": prompt_digest,
            }
        )

    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise DeepSWEEvaluationError("rubric has no evaluation criteria")
    findings: list[dict[str, Any]] = []
    seen_criteria: set[str] = set()
    for criterion in criteria:
        if not isinstance(criterion, Mapping):
            raise DeepSWEEvaluationError("rubric criterion is malformed")
        criterion_id = criterion.get("id")
        excerpt = criterion.get("instruction_excerpt")
        expected_behavior = criterion.get("expected_behavior")
        importance = criterion.get("importance")
        if (
            not isinstance(criterion_id, str)
            or criterion_id in seen_criteria
            or not isinstance(excerpt, str)
            or not isinstance(expected_behavior, str)
            or importance not in {"critical", "important"}
        ):
            raise DeepSWEEvaluationError(
                "rubric criterion has invalid identity or fields"
            )
        seen_criteria.add(criterion_id)
        if excerpt not in instruction:
            raise DeepSWEEvaluationError(
                f"rubric source excerpt is stale for criterion {criterion_id}"
            )
        symbols = _string_list(criterion, "solution_symbols")
        test_names = _string_list(criterion, "verifier_tests")
        missing_symbols = [
            item for item in symbols if not _patch_has_symbol(solution_patch, item)
        ]
        missing_tests = [item for item in test_names if item not in verifier_patch]
        if missing_symbols or missing_tests:
            raise DeepSWEEvaluationError(
                f"ground-truth references are stale for {criterion_id}: "
                f"solution symbols missing={missing_symbols}, "
                f"verifier tests missing={missing_tests}"
            )
        candidate_prompts = [
            {"request_id": item["request_id"], "prompt": item["prompt"]}
            for item in prompts
        ]
        prompt_expectation = criterion.get("worker_prompt_expectation", "present")
        if prompt_expectation not in {"present", "absent"}:
            raise DeepSWEEvaluationError(
                f"invalid worker_prompt_expectation for {criterion_id}"
            )
        if prompt_expectation == "absent":
            appears = any(excerpt in item["prompt"] for item in prompts)
            decision = {
                "decision": "contradicted" if appears else "supported",
                "evidence_quote": "",
                "reason": (
                    "Process instruction was excluded from all worker prompts."
                    if not appears
                    else "Process instruction appears in a worker prompt."
                ),
            }
        else:
            decision = _judge_worker_prompt_criterion(
                judge,
                criterion_id=criterion_id,
                instruction_excerpt=excerpt,
                expected_behavior=expected_behavior,
                solution_symbols=symbols,
                solution_evidence=_solution_evidence(solution_patch, symbols),
                verifier_tests=test_names,
                candidate_prompts=candidate_prompts,
            )
        quote = decision["evidence_quote"]
        if quote and not any(quote in item["prompt"] for item in prompts):
            decision = {
                "decision": "ambiguous",
                "evidence_quote": "",
                "reason": (
                    "Judge evidence quote was not present in a captured worker prompt."
                ),
            }
        elif (
            prompt_expectation == "present"
            and decision["decision"] == "supported"
            and not quote
        ):
            decision = {
                "decision": "ambiguous",
                "evidence_quote": "",
                "reason": (
                    "Judge marked the criterion supported without a prompt quote."
                ),
            }
        findings.append(
            {
                "criterion_id": criterion_id,
                "importance": importance,
                "instruction_excerpt": excerpt,
                "solution_symbols": symbols,
                "reference_solution_evidence": _solution_evidence(
                    solution_patch, symbols
                ),
                "verifier_tests": test_names,
                "prompt_request_ids": [item["request_id"] for item in prompts],
                **decision,
            }
        )

    weights = {"critical": 3, "important": 1}
    total_weight = sum(weights[item["importance"]] for item in findings)
    supported_weight = sum(
        weights[item["importance"]]
        for item in findings
        if item["decision"] == "supported"
    )
    report: dict[str, Any] = {
        "schema_version": "deepswe-worker-prompt-evaluation-v1",
        "task_id": task_id,
        "judge_id": judge_id,
        "worker_prompts": [
            {
                "request_id": item["request_id"],
                "provider": item["provider"],
                "prompt_fingerprint": item["fingerprint"],
            }
            for item in prompts
        ],
        "reference_fingerprints": {
            "instruction": _sha256(instruction_path),
            "solution_patch": _sha256(solution_path),
            "verifier_patch": _sha256(verifier_path),
            "rubric": _sha256(rubric_path),
        },
        "candidate_fingerprints": {
            "instruction_ledger": _sha256(ledger_path),
            "prompt_index": _sha256(prompt_index_path),
        },
        "summary": {
            "criterion_count": len(findings),
            "judge_calls": len(findings),
            "supported_count": sum(
                item["decision"] == "supported" for item in findings
            ),
            "critical_failures": [
                item["criterion_id"]
                for item in findings
                if item["importance"] == "critical" and item["decision"] != "supported"
            ],
            "weighted_coverage": round(supported_weight / total_weight, 4),
            "passed": all(
                item["decision"] == "supported"
                for item in findings
                if item["importance"] == "critical"
            ),
        },
        "findings": findings,
    }
    report["fingerprint"] = _content_fingerprint(report)
    return report


def _judge_worker_prompt_criterion(
    judge: DesignJudge,
    *,
    criterion_id: str,
    instruction_excerpt: str,
    expected_behavior: str,
    solution_symbols: Sequence[str],
    solution_evidence: Sequence[str],
    verifier_tests: Sequence[str],
    candidate_prompts: Sequence[Mapping[str, str]],
) -> dict[str, str]:
    payload = {
        "criterion_id": criterion_id,
        "source_instruction": instruction_excerpt,
        "expected_behavior_grounded_in_reference_solution_and_verifier": (
            expected_behavior
        ),
        "reference_solution_symbols": list(solution_symbols),
        "reference_solution_evidence": list(solution_evidence),
        "reference_verifier_tests": list(verifier_tests),
        "candidate_worker_prompts": list(candidate_prompts),
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Evaluate whether the exact worker-facing coding prompt or prompts "
                "explicitly preserve this source requirement's externally observable "
                "behavior. Use the reference solution and verifier only to understand "
                "the intended behavior; do not require the reference implementation's "
                "internal design. Return supported only when a candidate prompt states "
                "the behavior clearly. For supported or contradicted, quote exact text "
                "from one candidate prompt. Return missing if absent and ambiguous if "
                "the prompts do not resolve the behavior."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    complete = judge.complete_json
    try:
        try:
            result = complete(messages, response_schema=_JUDGE_SCHEMA)  # type: ignore[call-arg]
        except TypeError:
            result = complete(messages)
    except Exception as error:
        return {
            "decision": "ambiguous",
            "evidence_quote": "",
            "reason": f"Judge call failed: {type(error).__name__}: {error}",
        }
    if not isinstance(result, Mapping):
        return {
            "decision": "ambiguous",
            "evidence_quote": "",
            "reason": "Judge returned a non-object response.",
        }
    decision = result.get("decision")
    quote = result.get("evidence_quote")
    reason = result.get("reason")
    if (
        decision not in {"supported", "missing", "contradicted", "ambiguous"}
        or not isinstance(quote, str)
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        return {
            "decision": "ambiguous",
            "evidence_quote": "",
            "reason": "Judge response did not match the required evaluation schema.",
        }
    return {"decision": decision, "evidence_quote": quote, "reason": reason}


def _judge_criterion(
    judge: DesignJudge,
    *,
    criterion_id: str,
    instruction_excerpt: str,
    expected_behavior: str,
    solution_symbols: Sequence[str],
    solution_evidence: Sequence[str],
    verifier_tests: Sequence[str],
    candidate_text: str,
) -> dict[str, str]:
    payload = {
        "criterion_id": criterion_id,
        "source_instruction": instruction_excerpt,
        "expected_behavior_grounded_in_"
        "reference_solution_and_verifier": expected_behavior,
        "reference_solution_symbols": list(solution_symbols),
        "reference_solution_evidence": list(solution_evidence),
        "reference_verifier_tests": list(verifier_tests),
        "candidate_design_projection": candidate_text,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Evaluate one design criterion against the source instruction and "
                "the listed reference-solution symbols and verifier tests. Decide "
                "whether the candidate design preserves the required externally "
                "observable behavior. A solution symbol or test name alone does "
                "not prove coverage; assess the candidate projection's meaning. "
                "Do not require the reference implementation's internal design. "
                "Return supported only when the behavior is explicit and consistent. "
                "For supported or contradicted, quote exact text from the candidate "
                "projection as evidence. Return missing if absent and ambiguous if "
                "the projection does not resolve the behavior."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    complete = judge.complete_json
    try:
        try:
            result = complete(messages, response_schema=_JUDGE_SCHEMA)  # type: ignore[call-arg]
        except TypeError:
            result = complete(messages)
    except Exception as error:
        return {
            "decision": "ambiguous",
            "evidence_quote": "",
            "reason": f"Judge call failed: {type(error).__name__}: {error}",
        }
    if not isinstance(result, Mapping):
        return {
            "decision": "ambiguous",
            "evidence_quote": "",
            "reason": "Judge returned a non-object response.",
        }
    decision = result.get("decision")
    quote = result.get("evidence_quote")
    reason = result.get("reason")
    if (
        decision not in {"supported", "missing", "contradicted", "ambiguous"}
        or not isinstance(quote, str)
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        return {
            "decision": "ambiguous",
            "evidence_quote": "",
            "reason": "Judge response did not match the required evaluation schema.",
        }
    return {"decision": decision, "evidence_quote": quote, "reason": reason}


def _render_candidate_projections(items: Sequence[Mapping[str, Any]]) -> str:
    fragments: list[str] = []
    for item in items:
        for key in ("kind", "description", "acceptance_criterion", "expected_test"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                fragments.append(value.strip())
        scenario = item.get("behavior_scenario")
        if isinstance(scenario, Mapping):
            for key in (
                "subject",
                "given",
                "when",
                "then",
                "dimensions",
                "capability_matrix",
            ):
                value = scenario.get(key)
                if isinstance(value, str) and value.strip():
                    fragments.append(value.strip())
                elif isinstance(value, (Mapping, list)):
                    fragments.append(
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                    )
    return "\n".join(fragments)


def _spans_overlap(clause: Mapping[str, Any], start: int, end: int) -> bool:
    span = clause.get("source_span")
    if not isinstance(span, Mapping):
        return False
    clause_start = span.get("start")
    clause_end = span.get("end")
    return (
        isinstance(clause_start, int)
        and isinstance(clause_end, int)
        and clause_start < end
        and clause_end > start
    )


def _patch_has_symbol(patch: str, symbol: str) -> bool:
    if not symbol.strip():
        return False
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", patch)
        is not None
    )


def _solution_evidence(patch: str, symbols: Sequence[str]) -> list[str]:
    added_lines = [
        line[1:].strip()
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    evidence: list[str] = []
    for symbol in symbols:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")
        for line in added_lines:
            if pattern.search(line) and line not in evidence:
                evidence.append(line[:320])
                if len(evidence) >= 12:
                    return evidence
    return evidence


def _string_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeepSWEEvaluationError(f"rubric field {key!r} must be a string list")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeepSWEEvaluationError(
            f"could not read JSON artifact {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise DeepSWEEvaluationError(f"JSON artifact must contain an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _content_fingerprint(data: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DeepSWEEvaluationError",
    "evaluate_deepswe_design",
    "evaluate_deepswe_worker_prompt",
]
