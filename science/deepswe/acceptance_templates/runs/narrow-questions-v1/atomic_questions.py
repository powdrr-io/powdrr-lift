"""A second arm isolates one concrete observation before choosing new behavior."""

from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from powdrr_lift.workrr.jev_classifier import JevSemanticClassifierClient
from science.deepswe.acceptance_templates.cli import DEFAULT_MODEL, _client
from science.deepswe.acceptance_templates.common import (
    TEXT,
    Recorder,
    choice_schema,
    object_schema,
    write_json,
)
from science.deepswe.acceptance_templates.production_analysis import JevPromptAdapter

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "narrow_runner", HERE / "run_experiment.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("missing paired experiment runner")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

# Questions isolate observed failures. Options are competing interpretations,
# including unknown; the requested choice is never passed as a target answer.
QUESTIONS: dict[str, dict[str, Any]] = {
    "immutability": {
        "question": (
            "In the original createQuery implementation, does Object.freeze on "
            "queryRef prevent mutation of the referenced parameters array itself?"
        ),
        "options": {
            "reference_only": (
                "No: queryRef properties cannot be reassigned, but the referenced "
                "array is not frozen by this call."
            ),
            "recursive": (
                "Yes: freezing queryRef recursively freezes its parameters array."
            ),
            "unknown": "The supplied code does not allow this question to be answered.",
        },
    },
    "callback-grouping": {
        "question": (
            "Does the instruction's nested-aspect flattening rule require replacing "
            "readEach's merged aspect data object with separate constituent objects?"
        ),
        "options": {
            "merged": (
                "No: flattening identifies constituents; readEach still delivers "
                "merged aspect data."
            ),
            "expanded": (
                "Yes: each constituent must occupy a separate callback data slot."
            ),
            "unknown": (
                "The instruction does not distinguish merged from separate "
                "constituent data."
            ),
        },
    },
    "set-notifications": {
        "question": (
            "In original setTraitForTrait, if ctx.set returns normally after an "
            "equal-value write and triggerChanged is true, is setChanged called?"
        ),
        "options": {
            "called": (
                "Yes: the call is guarded by triggerChanged, not value inequality."
            ),
            "suppressed": "No: equal values suppress setChanged.",
            "unknown": (
                "The supplied function does not establish whether setChanged is called."
            ),
        },
    },
}


def run(case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["id"]
    inputs = runner.packet(case)
    question = QUESTIONS[case_id]
    flash = Recorder(
        _client("deepinfra", DEFAULT_MODEL, 120, 1536),
        HERE / "atomic/calls" / case_id / "flash",
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
        HERE / "atomic/calls" / case_id / "jev",
        provider="typesafe.ai",
        model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
        resume=True,
        repairs=0,
    )
    choices = choice_schema(*question["options"])
    try:
        observation = flash.call(
            "observation",
            "Answer exactly the supplied question. Interpret actual execution and "
            "ordinary meanings; do not demand the instruction repeat semantics "
            "already established by the supplied code. Choose one option and "
            "give one short reason. Do not decide any other new behavior.",
            {"sources": inputs["sources"], **question},
            object_schema(choice=choices, reason=TEXT),
            lambda row: row,
        )
        # Independent classification: Jev sees options/sources, not Flash's choice.
        opinion = jev.call(
            "independent-observation",
            "Question:\n" + question["question"] + "\nChoose the most faithful option.",
            {"sources": inputs["sources"], "options": question["options"]},
            object_schema(choice=choices),
            lambda row: row,
        )
        resolved = flash.call(
            "resolve",
            "Resolve the original behavioral question. Preserve normal meanings "
            "of the instruction and existing operation semantics. Do not turn "
            "an analogy into a claimed explicit obligation. Use the checked "
            "observation to avoid extending a rule beyond its scope. State a "
            "proposed default separately if the sources do not settle a choice.",
            {
                **inputs,
                "observation_question": question,
                "flash_observation": observation,
                "jev_opinion": opinion,
            },
            runner.ANSWER,
            lambda row: row,
        )
        verdict = jev.call(
            "resolved-check",
            "Question:\nDoes the candidate answer follow from the original sources "
            "with its claimed basis? Supported if justified; unsupported if "
            "contradicted or an analogy is mislabeled as an established requirement; "
            "insufficient for a reasonable proposed default or an unresolved choice.",
            {"sources": inputs["sources"], "candidate": resolved},
            object_schema(
                verdict=choice_schema("supported", "unsupported", "insufficient")
            ),
            lambda row: row,
        )
        result = {
            "case": case_id,
            "question": question,
            "observation": observation,
            "independent_jev": opinion,
            "agreement": observation["choice"] == opinion["choice"],
            "resolved": resolved,
            "resolved_verdict": verdict["verdict"],
            "calls": flash.calls + jev.calls,
        }
        write_json(HERE / "atomic" / f"{case_id}.json", result)
        print(case_id, observation["choice"], opinion["choice"], verdict, flush=True)
        return result
    finally:
        adapter.close()


def main() -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, runner.CASES))
    write_json(HERE / "atomic/results.json", {"results": results})
    write_json(
        HERE / "atomic/manifest.json",
        {
            "questions": QUESTIONS,
            "limitation": (
                "Development refinement after reading first-arm failures; "
                "not a holdout."
            ),
            "boundary": (
                "Same original sources; no solution, tests, or target labels "
                "in requests."
            ),
        },
    )


if __name__ == "__main__":
    main()
