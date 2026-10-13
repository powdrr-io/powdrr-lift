"""Execute only the declared consolidation tail on frozen generation artifacts."""

import argparse
import json
from pathlib import Path

import yaml

from procedrr import parse_and_validate
from procedrr_evaluator import Evaluator
from science.deepswe.acceptance_templates.common import load_json, write_json
from science.deepswe.acceptance_templates.repository_planning.contracts import (
    ContractClient,
    ContractOperations,
)

from .contracts_workflow import definition


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = load_json(args.output / "input.json")
    operations = ContractOperations(args.setup, args.output, packet)
    operations.records = [
        load_json(p) for p in sorted((args.output / "criteria/reviewed").glob("*.json"))
    ]
    operations.facts = [
        load_json(p) for p in sorted((args.output / "facts/reviewed").glob("*.json"))
    ]
    operations.coverage = [
        load_json(p) for p in sorted((args.output / "coverage").glob("*.json"))
    ]
    complete = definition()
    tail = {
        **complete,
        "name": "consolidate-frozen-criteria",
        "inputs": [complete["inputs"][0]],
        "steps": complete["steps"][3:],
    }
    source = yaml.safe_dump(tail, sort_keys=False)
    parse_and_validate(source)
    (args.output / "consolidation.yaml").write_text(source)
    client = ContractClient(args.output / "consolidation", "typesafe.ai")
    result = Evaluator(client, operations, judge_clients={"jev": client}).evaluate(
        tail, {"instruction": packet["instruction"]}
    )
    write_json(
        args.output / "consolidation/events.json",
        [{"kind": e.kind, "path": e.path, "data": dict(e.data)} for e in result.events],
    )
    summary = load_json(args.output / "summary.json")
    summary.update(
        consolidation_calls=result.llm_activations,
        final_criteria=sum(
            bool(r["rendered"]) and r["criterion"]["id"] not in operations.superseded
            for r in operations.records
        ),
    )
    write_json(args.output / "summary.json", summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
