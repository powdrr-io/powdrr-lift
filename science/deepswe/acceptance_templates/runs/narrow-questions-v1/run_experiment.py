"""Paired direct/decomposed analysis using original sources, Flash, and Jev."""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    IDS,
    TEXT,
    Recorder,
    array_schema,
    choice_schema,
    digest,
    object_schema,
    write_json,
)
from science.deepswe.acceptance_templates.production_analysis import JevPromptAdapter

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "operation-analysis-v1"
BASE = Path("/private/tmp/koota-aspect-analysis-base-20261011")

# Manually selected development cases and original-code retrieval. The author
# knows the solution. Neither question discovery nor retrieval is a blind result.
CASES = [
    {
        "id": "immutability",
        "requirement": "Each aspect exposes `id`, `traits`, and `schema`.",
        "question": (
            "What mutability contract should aspect metadata have? Distinguish "
            "property reassignment, container mutation, and nested-value mutation."
        ),
        "questions": {
            "scope": "Which of these mutation levels does the instruction specify?",
            "counterpart": "Which levels are protected by analogous existing APIs?",
            "transfer": "Which protections can legitimately transfer to an aspect?",
        },
        "sources": [
            ("trait/types.ts", 13, 18),
            ("trait/trait.ts", 47, 99),
            ("query/types.ts", 78, 88),
            ("query/query.ts", 444, 469),
        ],
        "negative": (
            "The instruction requires recursively freezing nested schema values."
        ),
    },
    {
        "id": "callback-grouping",
        "requirement": (
            "Nested aspects flatten to their individual traits. `readEach` delivers "
            "a merged data object and `updateEach` distributes writes back to "
            "constituent stores."
        ),
        "question": (
            "For query(createAspect(createAspect(A, B), C)), should the callback "
            "state contain one merged object or separate constituent objects?"
        ),
        "questions": {
            "scope": "What operation does each flattening or merging statement govern?",
            "counterpart": "How do original query parameters map to callback slots?",
            "transfer": "Does constituent flattening entail callback-slot expansion?",
        },
        "sources": [
            ("query/query-result.ts", 22, 51),
            ("query/query-result.ts", 245, 282),
            ("query/types.ts", 53, 75),
        ],
        "negative": (
            "Nested aspect flattening requires one separate callback data slot "
            "per constituent trait instead of a merged aspect object."
        ),
    },
    {
        "id": "set-notifications",
        "requirement": (
            "`set` distributes each field to its owning constituent and triggers "
            "per-trait change detection."
        ),
        "question": (
            "When an aspect setter supplies an equal value to a constituent, "
            "should that constituent notify with triggerChanged=true?"
        ),
        "questions": {
            "scope": "Does the instruction explicitly require value inequality?",
            "counterpart": (
                "What exactly triggers existing regular-trait set notifications?"
            ),
            "transfer": "Should set inherit setter semantics or updateEach semantics?",
        },
        "sources": [
            ("trait/trait.ts", 319, 350),
            ("trait/trait.ts", 403, 423),
            ("query/query-result.ts", 52, 113),
        ],
        "negative": (
            "Existing regular-trait setters suppress change notifications when "
            "the supplied value equals the stored value."
        ),
    },
]

ANSWER = object_schema(
    answer=TEXT,
    basis=choice_schema("explicit", "inherited", "proposed_default", "unresolved"),
    evidence_ids=IDS,
    transfer_reason=TEXT,
    unresolved=TEXT,
)
PLAN = object_schema(
    facts=array_schema(object_schema(fact=TEXT, evidence_ids=IDS, scope=TEXT)),
    question_ids=IDS,
    missing_information=TEXT,
)
CRITERIA = object_schema(
    criteria=array_schema(object_schema(given=TEXT, when=TEXT, then=TEXT)),
)
COMMON = (
    "Use only the original instruction and original repository excerpts. "
    "Return JSON matching the schema. Cite provided evidence IDs, never invented "
    "quotes. Explicit means required by the instruction; inherited means existing "
    "behavior with a justified relationship to this new operation. An analogy "
    "alone does not prove a new obligation. Proposed defaults are separate from "
    "established requirements. State insufficient information where appropriate. "
)


def packet(case: dict[str, Any]) -> dict[str, Any]:
    task = json.loads((PREVIOUS / "input.json").read_text())
    sources = [{"id": "instruction", "text": task["instruction"]}]
    for index, (path, start, end) in enumerate(case["sources"], 1):
        file = BASE / "packages/core/src" / path
        lines = file.read_text().splitlines()
        sources.append(
            {
                "id": f"code{index}",
                "file": f"packages/core/src/{path}",
                "start_line": start,
                "end_line": end,
                "text": "\n".join(lines[start - 1 : end]),
            }
        )
    return {
        "requirement": case["requirement"],
        "question": case["question"],
        "sources": sources,
    }


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["id"]
    inputs = packet(case)
    write_json(HERE / case_id / "input.json", inputs)
    flash = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 2048),
        HERE / "calls" / case_id / "flash",
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=0,
    )
    adapter = JevPromptAdapter(
        JevSemanticClassifierClient(flash.client, fail_closed=True)
    )
    jev = Recorder(
        adapter,
        HERE / "calls" / case_id / "jev",
        provider="typesafe.ai",
        model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        resume=True,
        repairs=0,
    )
    allowed = {source["id"] for source in inputs["sources"]}

    def validate_answer(row: dict[str, Any]) -> dict[str, Any]:
        if not row["answer"].strip():
            raise ValueError("empty answer")
        if set(row["evidence_ids"]) - allowed:
            raise ValueError("unknown source ID")
        return row

    def validate_plan(row: dict[str, Any]) -> dict[str, Any]:
        try:
            validate(row, PLAN)
        except ValidationError as exc:
            raise ValueError(exc.message) from None
        return row

    def judge(stage: str, question: str, subject: Any) -> str:
        result = jev.call(
            stage,
            "Question:\n" + question + "\nReturn only the classifier verdict.",
            {"original_sources": inputs, "candidate": subject},
            object_schema(
                verdict=choice_schema("supported", "unsupported", "insufficient")
            ),
            lambda row: row,
        )
        return str(result["verdict"])

    try:
        direct = flash.call(
            "direct",
            COMMON + "Answer the behavioral question in one precise statement.",
            inputs,
            ANSWER,
            validate_answer,
        )
        plan_prompt = (
            COMMON
            + "Do not decide the final behavior yet. Extract literal instruction "
            "facts and separate existing-code observations. Label the operation "
            "each fact governs. Select applicable question IDs from the supplied "
            "question bank. Do not extend construction rules to projection or "
            "setter rules to updateEach without justification."
        )
        plan_payload = {**inputs, "question_bank": case["questions"]}
        plan = flash.call(
            "decompose",
            plan_prompt,
            plan_payload,
            PLAN,
            lambda row: row,
        )
        try:
            validate_plan(plan)
        except ValueError as exc:
            # Preserve the first malformed response. Exactly one explicit shape
            # repair; do not repeat the original call or alter semantic feedback.
            plan = flash.call(
                "decompose-shape-repair",
                plan_prompt + " Return the full plan object, not a single fact.",
                {**plan_payload, "invalid_response": plan, "shape_error": str(exc)},
                PLAN,
                validate_plan,
            )
        if set(plan["question_ids"]) - set(case["questions"]):
            raise ValueError("unknown question ID")
        for fact in plan["facts"]:
            if set(fact["evidence_ids"]) - allowed:
                raise ValueError("unknown fact evidence ID")
        plan_verdict = judge(
            "plan-check",
            "Are all candidate facts supported by the original sources within "
            "their stated operation scope? Unsupported if any fact invents a "
            "requirement, conflates operations, or overstates what code proves. "
            "Insufficient if the sources cannot establish a fact; otherwise supported.",
            plan["facts"],
        )
        observations = flash.call(
            "answer-subquestions",
            COMMON + "Answer each selected question separately. Distinguish what is "
            "specified, what code does, and whether transferring it is justified. "
            "Do not silently accept a rejected plan fact.",
            {
                **inputs,
                "plan": plan,
                "plan_verdict": plan_verdict,
                "questions": {
                    key: case["questions"][key] for key in plan["question_ids"]
                },
            },
            object_schema(
                answers=array_schema(
                    object_schema(question_id=TEXT, **ANSWER["properties"])
                )
            ),
            lambda row: row,
        )
        for row in observations["answers"]:
            validate_answer(row)
        resolved = flash.call(
            "resolve",
            COMMON + "Answer only the original behavioral question using the separate "
            "observations. Check whether scope crossed between operations. "
            "Keep a plausible default distinct from an established requirement.",
            {**inputs, "observations": observations},
            ANSWER,
            validate_answer,
        )
        support_question = (
            "Does the evidence justify the candidate answer AND its claimed basis? "
            "Supported requires the instruction to entail an explicit answer, or "
            "code evidence plus a justified inheritance relationship for an "
            "inherited answer. Unsupported if contradicted or an unsupported "
            "extension is presented as required. Insufficient for unresolved "
            "answers or proposed defaults not established by original sources."
        )
        direct_verdict = judge("direct-check", support_question, direct)
        resolved_verdict = judge("resolved-check", support_question, resolved)
        negative_verdict = judge(
            "negative-control",
            "Is this single claim established by the original sources? Supported "
            "only if established; unsupported if contradicted or falsely states "
            "an instruction requirement; insufficient if not settled by sources.",
            case["negative"],
        )
        criteria: dict[str, Any] = {"criteria": []}
        criteria_verdict = "skipped-unresolved"
        if resolved_verdict == "supported" and resolved["basis"] in {
            "explicit",
            "inherited",
        }:
            criteria = flash.call(
                "criteria",
                COMMON
                + "Write at most two observable given/when/then criteria for only "
                "the resolved answer. Add no APIs, mutation rules or event semantics.",
                {**inputs, "resolved_answer": resolved},
                CRITERIA,
                lambda row: row,
            )
            criteria_verdict = judge(
                "criteria-check",
                "Does every candidate criterion follow from the resolved answer "
                "and original sources, without introducing behavior? Unsupported "
                "if any criterion adds an unsupported requirement; insufficient "
                "if evidence cannot settle it; otherwise supported.",
                {"resolved_answer": resolved, **criteria},
            )
        result = {
            "case": case_id,
            "direct": direct,
            "direct_verdict": direct_verdict,
            "plan": plan,
            "plan_verdict": plan_verdict,
            "observations": observations,
            "resolved": resolved,
            "resolved_verdict": resolved_verdict,
            "negative_claim": case["negative"],
            "negative_verdict": negative_verdict,
            **criteria,
            "criteria_verdict": criteria_verdict,
            "calls": flash.calls + jev.calls,
        }
        write_json(HERE / case_id / "result.json", result)
        print(case_id, resolved_verdict, criteria_verdict, flush=True)
        return result
    finally:
        adapter.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[case["id"] for case in CASES])
    args = parser.parse_args()
    manifest = {
        "experiment": "narrow-questions-v1",
        "cases": CASES,
        "script_sha256": digest(Path(__file__).read_text()),
        "boundary": "Original instruction/base excerpts only; no oracle or patches.",
        "limitation": (
            "Three manually selected known failures; one task, one sample per arm."
        ),
    }
    if not (HERE / "manifest.json").exists():
        write_json(HERE / "manifest.json", manifest)

    def collect(case: dict[str, Any]) -> None:
        try:
            run_case(case)
        except (RuntimeError, ValueError) as exc:
            write_json(
                HERE / case["id"] / "failure.json",
                {
                    "case": case["id"],
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(
            pool.map(
                collect, [c for c in CASES if not args.case or c["id"] == args.case]
            )
        )
    results = [
        json.loads(path.read_text()) for path in sorted(HERE.glob("*/result.json"))
    ]
    failures = [
        json.loads(path.read_text()) for path in sorted(HERE.glob("*/failure.json"))
    ]
    write_json(HERE / "results.json", {"results": results, "failures": failures})


if __name__ == "__main__":
    main()
