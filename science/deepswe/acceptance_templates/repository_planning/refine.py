"""Independent source review and one bounded criterion repair, without an oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.procedrr import parse_and_validate

from powdrr_lift.workrr import WorkflowLLMClient
from science.deepswe.acceptance_templates.common import (
    load_json,
    object_schema,
    write_json,
)
from science.deepswe.acceptance_templates.repository_planning.contracts import (
    ContractClient,
    ContractOperations,
    evaluate_recorded,
)

from .contracts_workflow import (
    CHOICE,
    TEXT,
    judge,
    loop,
    op,
    ref,
)


class RepairClient(ContractClient):
    @staticmethod
    def fallback(schema: Any) -> dict[str, Any]:
        if "given" in schema["properties"]:
            return {k: "" for k in schema["properties"]}
        return ContractClient.fallback(schema)


class ReviewOperations(ContractOperations):
    def __init__(
        self, setup: Path, output: Path, packet: dict[str, Any], generation: Path
    ) -> None:
        super().__init__(setup, output, packet)
        self.generation = generation
        self.decisions: list[dict[str, Any]] = []

    def __call__(self, tool: str, parameters: Any) -> Any:
        command = parameters["command"][0]
        if command == "independent_sources":
            identifier = parameters["item"]["requirement"]["id"]
            number = int(identifier.removeprefix("r"))
            sources = [
                load_json(p)["response"]
                for p in sorted(
                    (
                        self.generation / "reads" / f"batch-{(number - 1) // 4 + 1:02}"
                    ).glob("*.json")
                )
            ]
            return {
                "repository": sources,
                "dependencies": load_json(self.generation / "dependency-evidence.json"),
                "authority": (
                    "Original instruction and code only. Generated behavior and "
                    "previous approvals are hypotheses, not evidence."
                ),
            }
        if command == "quality_query":
            result = super().__call__(
                "internal",
                {"command": ["check_examples"], "criterion": parameters["criterion"]},
            )
            # Check installed directive locations without guessing a task schema.
            query = parameters["criterion"]["query"].strip()
            if query and result["valid"]:
                from .query_locations import check_locations

                definitions = load_json(self.generation / "dependency-evidence.json")[
                    "directives"
                ]
                issues = check_locations(self.container, query, definitions)
                result["directive_issues"] = issues
                result["valid"] = not issues
            return result
        if command == "quality_verdict":
            return {
                "accepted": parameters["setup"]["choice"]
                == parameters["outcome"]["choice"]
                == "supported"
                and parameters["syntax"]["valid"]
            }
        if command == "original_criterion":
            return parameters["item"]["criterion"]
        if command == "record_quality":
            item, criterion = parameters["item"], parameters["candidate"]
            verdict = parameters["final_verdict"]
            row = {
                "original": item,
                "criterion": criterion,
                "verdict": verdict,
                "first_review": parameters["first_verdict"],
                "repaired": criterion != item["criterion"],
            }
            self.decisions.append(row)
            write_json(self.output / "reviews" / f"{item['criterion']['id']}.json", row)
            record = {
                "requirement": item["requirement"],
                "behavior": item["behavior"],
                "criterion": {**criterion, "id": item["criterion"]["id"]},
                "premise": {
                    "choice": "supported" if verdict["accepted"] else "unknown"
                },
                "review": {"choice": "supported" if verdict["accepted"] else "unknown"},
                "example_check": {"valid": verdict["accepted"]},
            }
            return super().__call__(
                "internal", {"command": ["record_contract_criterion"], **record}
            )
        return super().__call__(tool, parameters)


def checks(criterion: str, result: str) -> list[dict[str, Any]]:
    return [
        op("quality_query", "syntax", criterion=ref(criterion)),
        judge(
            "setup",
            (
                "Do given and when completely specify valid inputs and an "
                "observation point sufficient to verify then?"
            ),
            (
                "Check every concrete value/flag used by then against "
                "given/when. Do not infer arbitrary example values. Check "
                "API/layer, directive locations, complete wire envelopes and "
                "input shapes. A requested new API need not exist yet. "
                "supported means complete valid setup; contradicted means "
                "invalid or invented input; unknown means necessary inputs are "
                "unspecified."
            ),
            ["instruction", "item.requirement", criterion, "sources", "syntax"],
            CHOICE,
            "jev",
        ),
        judge(
            "outcome",
            (
                "Does every expected assertion follow independently from the "
                "instruction and properly scoped source evidence?"
            ),
            (
                "Do not use generated behavior or earlier reviews as authority. "
                "Evaluate requested post-change behavior even when "
                "implementation is absent. Class identity is not required by a "
                "structural attribute requirement; another operation's return "
                "type is not a mandate for this one. Check omitted-field "
                "defaults, payload-local metadata and exact observation timing. "
                "A targeted update must not modify or relocate unrelated state "
                "unless requested. Inspect scope and grammatical attachment in "
                "the original instruction. Reject unsupported guesses about "
                "absent containers or new error policies. supported means all "
                "assertions justified; contradicted means any "
                "unsupported/conflicting assertion; unknown means genuinely "
                "undetermined. Dependency formats are scoped evidence, not "
                "mandated implementation classes."
            ),
            ["instruction", "item.requirement", criterion, "sources"],
            CHOICE,
            "jev",
        ),
        op(
            "quality_verdict",
            result,
            setup=ref("setup"),
            outcome=ref("outcome"),
            syntax=ref("syntax"),
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    packet = load_json(args.generation / "input.json")
    operations = ReviewOperations(args.setup, args.output, packet, args.generation)
    operations.facts = [
        load_json(p)
        for p in sorted((args.generation / "facts/reviewed").glob("*.json"))
    ]
    items = [
        load_json(p)
        for p in sorted((args.generation / "criteria/reviewed").glob("*.json"))
    ]
    schema = object_schema(given=TEXT, when=TEXT, then=TEXT, rejects=TEXT, query=TEXT)
    body = [
        op("independent_sources", "sources", item=ref("item")),
        op("original_criterion", "candidate", item=ref("item")),
        *checks("candidate", "first_verdict"),
        op(
            "quality_verdict",
            "final_verdict",
            setup=ref("setup"),
            outcome=ref("outcome"),
            syntax=ref("syntax"),
        ),
        {
            "branch": {
                "subject": "first_verdict.accepted",
                "cases": {
                    False: [
                        judge(
                            "candidate",
                            (
                                "How should this one criterion be corrected to "
                                "verify the active requirement with justified "
                                "observations?"
                            ),
                            (
                                "Repair the proposed criterion using the "
                                "original instruction and source evidence. "
                                "Fully specify concrete inputs and exact "
                                "observation timing. Query may be empty when "
                                "not needed; otherwise obey captured directive "
                                "locations. Assert only the active requirement "
                                "and necessary interactions. Do not require a "
                                "concrete output class unless explicitly "
                                "required. Avoid unrelated default values, "
                                "legacy wire envelopes and relocating "
                                "untargeted state. Existing absence never "
                                "blocks a required feature. Describe a "
                                "plausible wrong implementation rejected by "
                                "this condition. No solution patch is "
                                "available; do not guess unspecified error "
                                "policies."
                            ),
                            [
                                "instruction",
                                "item.requirement",
                                "candidate",
                                "sources",
                                "setup",
                                "outcome",
                                "syntax",
                            ],
                            schema,
                        ),
                        *checks("candidate", "final_verdict"),
                    ]
                },
                "default": [],
            }
        },
        op(
            "record_quality",
            "record",
            item=ref("item"),
            candidate=ref("candidate"),
            first_verdict=ref("first_verdict"),
            final_verdict=ref("final_verdict"),
        ),
    ]
    document = {
        "version": 1,
        "name": "independent-criterion-review",
        "inputs": [
            {"name": "instruction", "type": "string", "required": True},
            {"name": "items", "type": "array", "required": True},
        ],
        "limits": {
            "llm_activations": 250,
            "tool_calls": 400,
            "context_chars": 140000,
            "context_value_chars": 120000,
        },
        "steps": [
            loop("items", "item", "review_records", "record", body, 80),
            op("merge_exact_criteria", "merge_result"),
            op("render_prompt", "prompt"),
            {"terminal": "succeeded"},
        ],
    }
    source = yaml.safe_dump(document, sort_keys=False)
    parse_and_validate(source)
    (args.output / "pipeline.yaml").write_text(source)
    write_json(args.output / "input.json", packet)
    write_json(
        args.output / "selection.json",
        {
            "selection": "All generated criteria, not a gold-selected subset",
            "ids": [r["criterion"]["id"] for r in items],
            "oracle_access": False,
            "repair_budget": 1,
        },
    )
    flash: WorkflowLLMClient = RepairClient(args.output, "deepinfra")
    jev: WorkflowLLMClient = RepairClient(args.output, "typesafe.ai")
    usage = evaluate_recorded(
        document,
        operations,
        flash,
        jev,
        {"instruction": packet["instruction"], "items": items},
    )
    write_json(
        args.output / "summary.json",
        {
            "criteria": len(items),
            "initially_accepted": sum(
                r["first_review"]["accepted"] for r in operations.decisions
            ),
            "repaired": sum(r["repaired"] for r in operations.decisions),
            "finally_accepted": sum(
                r["verdict"]["accepted"] for r in operations.decisions
            ),
            **usage,
            "tests_run": False,
            "coding_agents_run": False,
        },
    )
    print(json.dumps(load_json(args.output / "summary.json")), flush=True)


if __name__ == "__main__":
    main()
