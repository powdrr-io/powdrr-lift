"""Generate concrete scenarios, resolve observations, and ask Jev to review."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tarfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    Recorder,
    choice_schema,
    digest,
    load_json,
    object_schema,
    write_json,
)

HERE = Path(__file__).resolve().parent
PRIOR = HERE.parent / "automatic-discovery-v1"
spec = importlib.util.spec_from_file_location(
    "scenario_prior", PRIOR / "run_experiment.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("missing committed experiment helpers")
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
TASKS = prior.TASKS
DISCOVER = (
    "Create concrete usage scenarios that clarify the requested features. Use "
    "the original instruction and corresponding existing code, with routed "
    "clauses as an index; do not amplify scope a splitter added. For the active "
    "clauses, give a starting state (given), one operation (when), and one "
    "observable question (observe). Use small concrete values and name the "
    "observation point. Fully specify relevant state: earlier versus later "
    "delivery, operation options, object/container versus nested value, and "
    "relationships between requested features. Include only dimensions relevant "
    "to this operation. Use valid requested usage and informative interactions; "
    "avoid arbitrary new error policies, unrequested limits, or guessing how "
    "malformed inputs ought to work. Each scenario should distinguish plausible "
    "implementations of a requested behavior. Do not invent methods or assume "
    "callbacks consume their return value. Do not propose an expected outcome, "
    "competing policy choices, fact-analysis records, or an ideal prompt. "
    "Produce up to four useful scenarios per batch, not a quota. Related "
    "requirements can share a scenario. Avoid repeating scenarios."
)
ANSWER = (
    "Resolve the stated observation for exactly this starting state and "
    "operation using the original instruction and corresponding existing code. "
    "Explain the expected observable result concisely, with concrete values "
    "where helpful. Preserve compatible existing behavior; explicit new "
    "requirements override it. Infer necessary consequences of the feature's "
    "meaning and relationships without requiring every detail verbatim. Keep "
    "separate operations and observation boundaries separate. Do not add "
    "guarantees outside this scenario or prescribe an implementation. If the "
    "sources genuinely do not settle this observation, mark unresolved rather "
    "than invent an answer. Write only the observation, not the given/when "
    "prefix again. There is no later rewriting or synthesis."
)
REVIEW = (
    "Does the proposed expected observation correctly implement the original "
    "instruction for exactly this given state and operation? Inspect the "
    "corresponding existing code, preserving compatible behavior and applying "
    "explicit overrides. Reason about necessary feature consequences, not just "
    "literal wording. Choose supported when the outcome is correct within this "
    "scenario, contradicted when it conflicts with the source behavior or "
    "introduces an unsupported guarantee, and unknown when it is genuinely "
    "underdetermined. Check all conditions and the observation point; do not "
    "trust the proposed answer merely because it sounds plausible."
)


def text(limit: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": limit}


SCENARIO = object_schema(
    id={"type": "string", "pattern": "^[A-Za-z0-9_-]+$", "maxLength": 24},
    requirement_ids={"type": "array", "minItems": 1, "items": text(20)},
    given=text(420),
    when=text(300),
    observe=text(260),
)
ANSWER_SCHEMA = object_schema(
    status=choice_schema("resolved", "unresolved"), then=text(500)
)
REVIEW_SCHEMA = object_schema(
    choice=choice_schema("supported", "contradicted", "unknown")
)


def checked(row: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    try:
        validate(row, schema)
    except ValidationError as exc:
        raise ValueError(exc.message) from None
    return row


def discover(job: tuple[str, int, list[str]]) -> tuple[str, list[dict[str, Any]]]:
    task, number, ids = job
    packet = prior.source_packet(task)
    batch = f"batch-{number:02}"
    schema = object_schema(
        scenarios={
            "type": "array",
            "maxItems": 4,
            "items": json.loads(json.dumps(SCENARIO)),
        }
    )
    schema["properties"]["scenarios"]["items"]["properties"]["requirement_ids"][
        "items"
    ] = choice_schema(*ids)
    rec = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 3500),
        HERE / "calls/discovery" / task / batch,
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=0,
    )

    def validate_scenarios(row: dict[str, Any]) -> dict[str, Any]:
        checked(row, schema)
        if len({r["id"] for r in row["scenarios"]}) != len(row["scenarios"]):
            raise ValueError("repeated scenario IDs")
        return row

    try:
        result = rec.call(
            "scenarios",
            DISCOVER,
            {**packet, "active_requirement_ids": ids},
            schema,
            validate_scenarios,
        )
        scenarios = result["scenarios"]
        for row in scenarios:
            row["id"] = f"{batch}-{row['id']}"
        write_json(
            HERE / "scenarios" / task / f"{batch}.json", {"scenarios": scenarios}
        )
        print(task, batch, len(scenarios), flush=True)
        return task, scenarios
    except RuntimeError as exc:
        write_json(HERE / "scenarios" / task / f"{batch}.json", {"failed": str(exc)})
        return task, []


def resolve(job: tuple[str, dict[str, Any]], *, future: bool = False) -> dict[str, Any]:
    task, scenario = job
    packet = {**prior.source_packet(task), "scenario": scenario}
    stage = "future-answers" if future else "answers"
    directory = HERE / "calls" / stage / task / scenario["id"]
    framing = (
        (
            "Describe expected behavior AFTER the requested feature is implemented. "
            "The original code intentionally predates the feature: absence of its "
            "implementation is never evidence that a requested outcome is unresolved. "
            "The instruction defines the new behavior; existing code supplies API "
            "boundaries and compatible behavior, not a veto on implementing the task. "
        )
        if future
        else ""
    )
    rec = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 600),
        directory / "flash",
        provider="deepinfra",
        model=DEFAULT_MODEL,
        resume=True,
        repairs=0,
    )
    result: dict[str, Any] = {"task_id": task, "id": scenario["id"]}
    try:
        answer = rec.call(
            "answer",
            framing + ANSWER,
            packet,
            ANSWER_SCHEMA,
            lambda r: checked(r, ANSWER_SCHEMA),
        )
        result.update(answer)
    except RuntimeError as exc:
        result.update(status="failed", then=None, answer_error=str(exc))
    if result["status"] == "resolved":
        reviewer = Recorder(
            prior.ObservedJev(directory / "jev/provider.json"),
            directory / "jev",
            provider="typesafe.ai",
            model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
            resume=True,
            repairs=0,
        )
        try:
            review = reviewer.call(
                "review",
                "Question:\n" + REVIEW + "\n\nInstructions:\n" + framing + REVIEW,
                {**packet, "proposed_observation": result["then"]},
                REVIEW_SCHEMA,
                lambda r: checked(r, REVIEW_SCHEMA),
            )
            result["jev"] = review["choice"]
        except RuntimeError as exc:
            result.update(jev="failed", review_error=str(exc))
    else:
        result["jev"] = "not_called"
    result["rendered"] = (
        (f"Given {scenario['given']} When {scenario['when']} Then {result['then']}")
        if result["jev"] == "supported"
        else None
    )
    target = HERE / "future/results" if future else HERE / "results"
    write_json(target / task / f"{scenario['id']}.json", result)
    print(task, scenario["id"], result["status"], result["jev"], flush=True)
    return result


def finish(
    scenarios: list[tuple[str, dict[str, Any]]], results: list[dict[str, Any]]
) -> None:
    for task in TASKS:
        write_json(
            HERE / "scenarios" / f"{task}.json",
            {"scenarios": [r for t, r in scenarios if t == task]},
        )
        packet = prior.source_packet(task)
        clauses = [
            r
            for r in packet["routed_clauses"]
            if r["route"] in {"include", "include_prohibition"}
        ]
        lines = [
            "# Objective",
            "",
            "Implement the requested feature using the existing repository APIs.",
            "",
            "# Requirements",
            "",
        ]
        lines += [f"{i}. {r['text']}" for i, r in enumerate(clauses, 1)]
        lines += [
            "",
            "# Constraints",
            "",
            "- Powdrr manages branching and commits.",
            "",
            "# Relevant context",
            "",
            "- Original code paths: "
            + ", ".join(sorted({r["file"] for r in packet["original_code"]}))
            + ".",
            "",
            "# Acceptance criteria",
            "",
        ]
        lines += [
            f"- {criterion}"
            for criterion in dict.fromkeys(
                r["rendered"] for r in results if r["task_id"] == task and r["rendered"]
            )
        ]
        lines += [
            "",
            (
                "Original requirements remain completion conditions when "
                "clarification is unresolved."
            ),
            "",
            "# Verification and completion",
            "",
            (
                "- Add or update relevant validations. Run applicable "
                "repository checks and report checks performed, failures and "
                "remaining ambiguity."
            ),
            "",
        ]
        path = HERE / "prompts" / f"{task}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines))
    checkpoints = []
    with tarfile.open(HERE / "call-checkpoints.tar.gz", "w:gz") as archive:
        for path in sorted((HERE / "calls").rglob("*")):
            if not path.is_file() or path.suffix not in {".json", ".txt"}:
                continue
            archive.add(path, arcname=str(path.relative_to(HERE)))
            row = load_json(path) if path.suffix == ".json" else {}
            checkpoints.append(
                {
                    "file": str(path.relative_to(HERE)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    **(
                        {
                            "receipt": row["receipt"],
                            "status": row["status"],
                            "request_sha256": row["request_sha256"],
                        }
                        if "receipt" in row
                        else {"raw_provider_capture": True}
                    ),
                }
            )
    write_json(HERE / "call-checkpoints.json", {"checkpoints": checkpoints})
    calls = [r for r in checkpoints if "receipt" in r]
    write_json(
        HERE / "run-summary.json",
        {
            "scenarios": dict(Counter(task for task, _ in scenarios)),
            "resolved": sum(r["status"] == "resolved" for r in results),
            "reviews": dict(Counter(r["jev"] for r in results)),
            "provider_calls": len(calls),
            "providers": dict(Counter(r["receipt"]["provider"] for r in calls)),
            "statuses": dict(Counter(r["status"] for r in calls)),
            "summed_provider_seconds": round(
                sum(r["receipt"]["elapsed_seconds"] for r in calls), 3
            ),
            "tests_run": False,
            "coding_agents_run": False,
        },
    )


def main() -> None:
    definition = {
        "discovery": DISCOVER,
        "answer": ANSWER,
        "review": REVIEW,
        "scenario_schema": SCENARIO,
        "answer_schema": ANSWER_SCHEMA,
        "review_schema": REVIEW_SCHEMA,
        "model": DEFAULT_MODEL,
        "jev_model": os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        "input_hashes": {task: digest(prior.source_packet(task)) for task in TASKS},
        "oracle_access": False,
        "batch_size": 4,
        "facts_stage": False,
        "evaluation": (
            "Audit all generated scenarios/proposed observations against "
            "solutions after calls; map to the same 18 target facets."
        ),
    }
    target = HERE / "experiment-definition.json"
    if target.exists() and load_json(target) != definition:
        raise ValueError("experiment definition changed")
    write_json(target, definition)
    jobs = []
    for task in TASKS:
        packet = prior.source_packet(task)
        write_json(HERE / "inputs" / f"{task}.json", packet)
        ids = [
            r["id"]
            for r in packet["routed_clauses"]
            if r["route"] in {"include", "include_prohibition"}
        ]
        jobs += [(task, i // 4 + 1, ids[i : i + 4]) for i in range(0, len(ids), 4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        discovered = list(pool.map(discover, jobs))
    scenarios = [(task, row) for task, batch in discovered for row in batch]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(resolve, scenarios))
    finish(scenarios, results)


if __name__ == "__main__":
    main()
