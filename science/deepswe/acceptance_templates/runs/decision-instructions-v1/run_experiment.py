"""Compare current, consistency-focused, and few-example decision instructions."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    Recorder,
    choice_schema,
    digest,
    object_schema,
    write_json,
)
from science.deepswe.acceptance_templates.production_analysis import JevPromptAdapter

HERE = Path(__file__).resolve().parent
ARMS = ("current", "revised", "examples")
CURRENT = (
    "Choose the most faithful behavior for exactly the supplied question using "
    "the original instruction and original code. Preserve ordinary instruction "
    "meanings and established behavior of the corresponding operation. Do not "
    "infer recursive freezing from readonly properties or shallow freezing. An "
    "analogous API is not proof of a new policy; choose unknown if the available "
    "sources do not settle the choice. Return only the selected choice."
)
REVISED = (
    "Choose the behavior that best satisfies the requested feature's meaning, "
    "purpose and relationships with existing operations. First identify the "
    "operation, object, condition and scope governed by each relevant statement. "
    "Keep construction, membership, callback projection, writing and event "
    "delivery separate. Inspect actual corresponding code behavior. Preserve "
    "that behavior where compatible; an explicit new behavior overrides it. "
    "Compare the alternatives by their observable consequences: do they keep "
    "the requested relationships consistent, or introduce an unrequested "
    "capability, side effect or cross-operation rule? Infer an ordinary coherent "
    "contract when supported by these relationships, even if no sentence states "
    "every detail verbatim. Do not require literal repetition of a necessary "
    "implication. Choose unknown for unsupported extra guarantees or a choice "
    "still genuinely underdetermined after checking scope, existing behavior "
    "and consequences. Return only the selected choice."
)

# Synthetic demonstrations from development failure families. No evaluation
# repository names, selectors, streamed paths or result policies appear here.
EXAMPLES = [
    {
        "source": (
            "A bundle constructor flattens nested bundles into members. Its "
            "read callback delivers one merged bundle record."
        ),
        "question": "Does flattening require separate member callback arguments?",
        "correct": (
            "No; flattening governs membership, while the callback's promised "
            "value remains merged."
        ),
        "tempting_wrong": "Yes; flattening applies to all later representations.",
    },
    {
        "source": (
            "Object.freeze({members: suppliedMembers}) protects an object's own "
            "properties."
        ),
        "question": (
            "Does this freeze make suppliedMembers and its nested values immutable?"
        ),
        "correct": (
            "No; object-property protection and "
            "referenced-container/nested-value protection are different "
            "decisions."
        ),
        "tempting_wrong": "Yes; metadata immutability implies recursive immutability.",
    },
    {
        "source": (
            "assign writes a supplied field and calls announce when "
            "announceEnabled is true. edit callback processing announces only "
            "unequal values. New assignBundle routes fields through assign."
        ),
        "question": "Does an equal-value assignBundle field announce when enabled?",
        "correct": (
            "Yes; this operation corresponds to assign, not edit callback detection."
        ),
        "tempting_wrong": (
            "No; adopt equality filtering from the neighboring edit operation."
        ),
    },
    {
        "source": (
            "A component defines immutable membership and a lookup derived from "
            "it. Task adds a composed component view, with no operation to "
            "reconfigure its definition."
        ),
        "question": (
            "Should exposing its member list introduce independent reconfiguration?"
        ),
        "correct": (
            "No; keep the definition and its derived lookup consistent, while "
            "deciding nested-value protection separately."
        ),
        "tempting_wrong": (
            "Allow arbitrary member-list edits because the word immutable was "
            "not repeated for the view."
        ),
    },
]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def run(job: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    arm, case = job
    source = load(HERE / "sources" / f"{case['task_id']}.json")
    routed = load(HERE / "routing" / f"{case['task_id']}.json")
    question = (
        case["current_question"] if arm == "current" else case["revised_question"]
    )
    payload = {
        "task_id": case["task_id"],
        "instruction": source["instruction"],
        "original_code": source["original_code"],
        "routed_clauses": [
            {key: row[key] for key in ("id", "text", "route", "source_ids")}
            for row in routed["inventory"]["items"]
        ],
        "question": question,
        "options": case["options"],
    }
    if arm == "examples":
        payload["demonstrations"] = EXAMPLES
    prompt = f"Question:\n{question}\n\nInstructions:\n" + (
        CURRENT if arm == "current" else REVISED
    )
    schema = object_schema(choice=choice_schema(*case["options"]))
    saved_path = HERE / "results" / arm / f"{case['id']}.json"
    if saved_path.exists():
        saved = load(saved_path)
        if saved.get("diagnostic"):
            diagnostic = load(
                HERE / "calls/diagnostics" / case["id"] / "observe-abstention-0.json"
            )
            expected_messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            if (
                diagnostic["request"]["messages"] != expected_messages
                or diagnostic["request"]["schema"] != schema
            ):
                raise ValueError("stale normalized diagnostic; inputs changed")
            if diagnostic["request"]["model"] != os.environ.get(
                "SYSTEM_ONE_MODEL", "jev-latest"
            ):
                raise ValueError("stale normalized diagnostic; model changed")
            return saved

    def checked(row: dict[str, Any]) -> dict[str, Any]:
        try:
            validate(row, schema)
        except ValidationError as exc:
            raise ValueError(exc.message) from None
        return row

    flash = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 256),
        HERE / "calls/decisions" / arm / case["id"] / "flash",
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
        HERE / "calls/decisions" / arm / case["id"] / "jev",
        provider="typesafe.ai",
        model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        resume=True,
        repairs=0,
    )
    try:
        answer = flash.call("choose", prompt, payload, schema, checked)
        opinion = jev.call("choose", prompt, payload, schema, checked)
        consensus = (
            answer["choice"] if answer["choice"] == opinion["choice"] else "unknown"
        )
        result = {
            "id": case["id"],
            "task_id": case["task_id"],
            "arm": arm,
            "flash": answer["choice"],
            "jev": opinion["choice"],
            "consensus": consensus,
            "rendered": None if consensus == "unknown" else case["options"][consensus],
            "calls": flash.calls + jev.calls,
        }
        write_json(HERE / "results" / arm / f"{case['id']}.json", result)
        print(arm, case["id"], answer["choice"], opinion["choice"], flush=True)
        return result
    finally:
        adapter.close()


def metrics(
    rows: list[dict[str, Any]], labels: dict[str, dict[str, Any]], model: str
) -> dict[str, Any]:
    count: Counter[str] = Counter()
    for row in rows:
        gold = labels[row["id"]]
        value = row.get(model, "failed")
        if value == "failed":
            count["failed"] += 1
        elif gold["expected"] == "unknown":
            count[
                "correct_withholding" if value == "unknown" else "unsupported_addition"
            ] += 1
        elif value == gold["expected"]:
            count["correct"] += 1
        elif value == "unknown":
            count["missing"] += 1
        else:
            count["wrong"] += 1
    return {
        key: count[key]
        for key in (
            "correct",
            "missing",
            "wrong",
            "correct_withholding",
            "unsupported_addition",
            "failed",
        )
    }


def finish(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    # Evaluation labels are loaded only after model decisions are complete.
    labels = {row["id"]: row for row in load(HERE / "oracle.json")["labels"]}
    by_id = {row["id"]: row for row in cases}
    scores: dict[str, Any] = {}
    for split in ("development", "evaluation", "all"):
        scores[split] = {}
        for arm in ARMS:
            rows = [
                r
                for r in results
                if r["arm"] == arm
                and (split == "all" or by_id[r["id"]]["split"] == split)
            ]
            scores[split][arm] = {
                model: metrics(rows, labels, model)
                for model in ("flash", "jev", "consensus")
            }
    per_kind = {}
    for kind in ("explicit", "inherited", "inferred", "unsupported"):
        per_kind[kind] = {
            arm: {
                model: metrics(
                    [
                        r
                        for r in results
                        if r["arm"] == arm and by_id[r["id"]]["kind"] == kind
                    ],
                    labels,
                    model,
                )
                for model in ("flash", "jev", "consensus")
            }
            for arm in ARMS
        }
    for arm in ARMS:
        for task in sorted({case["task_id"] for case in cases}):
            routed = load(HERE / "routing" / f"{task}.json")
            source = load(HERE / "sources" / f"{task}.json")
            product = [
                r
                for r in routed["inventory"]["items"]
                if r["route"] in {"include", "include_prohibition"}
            ]
            rows = [r for r in results if r["arm"] == arm and r["task_id"] == task]
            lines = [
                "# Objective",
                "",
                (
                    "Implement the requested feature described below using the "
                    "existing repository APIs."
                ),
                "",
                "# Requirements",
                "",
            ]
            lines += [f"{i}. {r['text']}" for i, r in enumerate(product, 1)]
            lines += [
                "",
                "# Constraints",
                "",
                "- Powdrr manages branching and commits in the supplied checkout.",
                "",
                "# Relevant context",
                "",
            ]
            lines += [
                "- Original code paths: "
                + ", ".join(sorted({r["file"] for r in source["original_code"]}))
                + "."
            ]
            lines += ["", "# Acceptance criteria", ""]
            lines += [f"- {r['rendered']}" for r in rows if r.get("rendered")]
            lines += [
                "",
                (
                    "The numbered requirements remain completion conditions "
                    "when an added clarification is unresolved."
                ),
                "",
                "# Verification and completion",
                "",
                (
                    "- Add or update relevant validations for these "
                    "requirements. Run applicable existing repository checks "
                    "and report checks performed, failures and remaining "
                    "ambiguity."
                ),
                "",
            ]
            target = HERE / "prompts" / arm / f"{task}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines))
    transitions = []
    indexed = {(r["arm"], r["id"]): r for r in results}
    for case in cases:
        transitions.append(
            {
                "id": case["id"],
                "split": case["split"],
                "kind": case["kind"],
                "expected": labels[case["id"]]["expected"],
                "evidence": labels[case["id"]]["evidence"],
                "arms": {
                    arm: {
                        model: indexed[(arm, case["id"])].get(model, "failed")
                        for model in ("flash", "jev", "consensus")
                    }
                    for arm in ARMS
                },
            }
        )
    write_json(
        HERE / "scores.json",
        {
            "scores": scores,
            "by_kind": per_kind,
            "rows": transitions,
            "limitation": (
                "One sample per manually designed question per model/arm; no "
                "automatic question-discovery or coding success claim."
            ),
        },
    )
    checkpoints = []
    paths = sorted((HERE / "calls").glob("**/*.json"))
    with tarfile.open(HERE / "call-checkpoints.tar.gz", "w:gz") as archive:
        for path in paths:
            record = load(path)
            if record.get("status") == "running":
                raise ValueError("incomplete checkpoint")
            archive.add(path, arcname=str(path.relative_to(HERE)))
            checkpoints.append(
                {
                    "file": str(path.relative_to(HERE)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "request_sha256": record["request_sha256"],
                    "status": record["status"],
                    "receipt": record["receipt"],
                }
            )
    write_json(HERE / "call-checkpoints.json", {"checkpoints": checkpoints})
    print(json.dumps(scores["evaluation"], indent=2), flush=True)


def main() -> None:
    case_file = load(HERE / "cases.json")
    manifest = load(HERE / "manifest.json")
    assert digest(case_file) == manifest["cases_sha256"]
    assert (
        hashlib.sha256((HERE / "oracle.json").read_bytes()).hexdigest()
        == manifest["oracle_sha256"]
    )
    definitions = {
        "arms": list(ARMS),
        "current": CURRENT,
        "revised": REVISED,
        "examples": EXAMPLES,
    }
    frozen = HERE / "experiment-definition.json"
    if frozen.exists() and load(frozen) != definitions:
        raise ValueError("experiment instructions changed after freezing")
    write_json(frozen, definitions)

    def collect(job: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        try:
            return run(job)
        except (RuntimeError, ValueError) as exc:
            arm, case = job
            result = {
                "id": case["id"],
                "task_id": case["task_id"],
                "arm": arm,
                "error": str(exc),
            }
            write_json(HERE / "results" / arm / f"{case['id']}.json", result)
            return result

    jobs = [(arm, case) for case in case_file["cases"] for arm in ARMS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(collect, jobs))
    finish(case_file["cases"], results)


if __name__ == "__main__":
    main()
