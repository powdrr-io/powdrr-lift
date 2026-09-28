#!/usr/bin/env python3
"""Compare Jev with a configured LLM on source-level classifier decisions.

The repository has archived teacher labels for disposition only. This pilot
generates fresh LLM references from the production classifier definitions for
the other source-level decision kinds. Its agreement scores are model-to-model
agreement, not accuracy against human gold labels.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from powdrr_lift.core.semantic_decision import DECISION_VALUES
from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
from powdrr_lift.workrr.provider_config import default_llm_mappings
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)
from powdrr_lift.workrr.semantic_contract_compiler import CLASSIFIER_DEFINITIONS

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "science/classifications/root_disposition/data/root_disposition.jsonl"
HOLDOUT_FAMILIES = (
    ROOT / "science/classifications/root_disposition/adjudication/holdout-families.json"
)
DEFAULT_OUTPUT_DIR = ROOT / "science/classifications/jev-other-classifiers"
API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
SEED = 20260930
PRODUCT_DISPOSITIONS = (
    "entity",
    "feature",
    "interface",
    "invariant",
    "guidance",
    "non_goal",
)
SOURCE_CLASSIFIER_KINDS = (
    "polarity",
    "quantifier",
    "requirement_strength",
    "has_precondition",
    "has_exception",
    "has_explicit_result",
    "temporal_scope",
    "source_predicate",
    "behavior_family",
)
NONACTIONABLE_KIND = "nonactionable_exclusion_safety"
UNRESOLVED_REASONS = (
    "source_ambiguous",
    "source_underspecified",
    "unsupported_concept",
    "classifier_abstained",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sample(
    rows: list[dict[str, Any]],
    per_disposition: int,
    heldout_families: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = row["labels"].get("class")
        if (
            row["source"].get("source_family_id") not in heldout_families
            and row["labels"].get("answerable") is True
            and label
            in (
                *PRODUCT_DISPOSITIONS,
                "nonactionable",
            )
        ):
            grouped[label].append(row)
    selected: list[dict[str, Any]] = []
    for label, candidates in sorted(grouped.items()):
        random.Random(f"{SEED}:{label}").shuffle(candidates)
        selected.extend(candidates[:per_disposition])
    random.Random(SEED).shuffle(selected)
    return selected


def _kinds_for(row: dict[str, Any]) -> tuple[str, ...]:
    disposition = row["labels"]["class"]
    if disposition == "nonactionable":
        return (NONACTIONABLE_KIND,)
    return SOURCE_CLASSIFIER_KINDS


def _request_schema(kinds: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["decisions"],
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "object",
                "required": list(kinds),
                "additionalProperties": False,
                "properties": {
                    kind: {
                        "type": "object",
                        "required": ["status", "value", "reason_code"],
                        "additionalProperties": False,
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["resolved", "unresolved"],
                            },
                            "value": {
                                "type": ["string", "null"],
                                "enum": [*sorted(DECISION_VALUES[kind]), None],
                            },
                            "reason_code": {
                                "type": ["string", "null"],
                                "enum": [*UNRESOLVED_REASONS, None],
                            },
                        },
                    }
                    for kind in kinds
                },
            }
        },
    }


def _llm_messages(row: dict[str, Any], kinds: tuple[str, ...]) -> list[dict[str, str]]:
    tasks = []
    for kind in kinds:
        definition = CLASSIFIER_DEFINITIONS[kind]
        tasks.append(
            {
                "decision_kind": kind,
                "question": definition.question,
                "instructions": list(definition.instructions),
            }
        )
    state = {
        "proposition": row["inputs"]["proposition"],
        "root_disposition": row["labels"]["class"],
        "behavior_phrase": row["inputs"]["proposition"],
        "accepted_definition_candidate": None,
        "classifier_tasks": tasks,
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a source-grounded semantic classifier. Return only the "
                "declared JSON object. Evaluate each named decision independently "
                "using its own question and instructions. Do not infer information "
                "that is absent from the exact proposition."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(state, ensure_ascii=False, indent=2),
        },
    ]


def _jev_question(kind: str) -> dict[str, Any]:
    definition = CLASSIFIER_DEFINITIONS[kind]
    instructions = "\n".join(f"- {item}" for item in definition.instructions)
    return {
        "type": "choice",
        "instructions": f"{definition.question}\n\n{instructions}",
        "criteria": {value: None for value in sorted(DECISION_VALUES[kind])}
        | {"unresolved": "The proposition does not support a defensible label."},
    }


def _call_jev(
    row: dict[str, Any], kinds: tuple[str, ...], api_key: str, timeout: float
) -> dict[str, Any]:
    body = {
        "model": JEV_MODEL,
        "state": {
            "proposition": row["inputs"]["proposition"],
            "root_disposition": row["labels"]["class"],
            "behavior_phrase": row["inputs"]["proposition"],
            "accepted_definition_candidate": None,
        },
        "questions": {kind: _jev_question(kind) for kind in kinds},
    }
    request = Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(5):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            answers = payload["answers"]
            output = {}
            for kind in kinds:
                answer = answers[kind]
                value = answer.get("choice")
                allowed = DECISION_VALUES[kind] | {"unresolved"}
                if value not in allowed:
                    raise ValueError(
                        f"Jev returned an unsupported {kind} value: {value!r}"
                    )
                output[kind] = {
                    "value": value,
                    "confidence": answer.get("confidence"),
                    "probabilities": answer.get("probabilities"),
                }
            return {"model": payload.get("model", JEV_MODEL), "answers": output}
        except HTTPError as exc:
            if exc.code not in (429, 529) or attempt == 4:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Jev API returned HTTP {exc.code}: {detail}"
                ) from None
        except (URLError, TimeoutError) as exc:
            if attempt == 4:
                raise RuntimeError(f"Jev API request failed: {exc}") from None
        time.sleep(2**attempt)
    raise RuntimeError("Jev API retry budget exhausted")


def _comparison_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = sorted({kind for row in rows for kind in row["decisions"]})
    result = {}
    for kind in kinds:
        paired = [row["decisions"][kind] for row in rows if kind in row["decisions"]]
        confusion: dict[str, Counter[str]] = defaultdict(Counter)
        for item in paired:
            confusion[item["llm"]["value"]][item["jev"]["value"]] += 1
        result[kind] = {
            "n": len(paired),
            "agreement": sum(
                item["llm"]["value"] == item["jev"]["value"] for item in paired
            )
            / len(paired),
            "llm_labels": dict(
                sorted(Counter(item["llm"]["value"] for item in paired).items())
            ),
            "jev_labels": dict(
                sorted(Counter(item["jev"]["value"] for item in paired).items())
            ),
            "confusion_matrix": {
                label: dict(sorted(counts.items()))
                for label, counts in sorted(confusion.items())
            },
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-disposition", type=int, default=3)
    parser.add_argument(
        "--llm-provider", choices=("deepinfra-cheap",), default="deepinfra-cheap"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.per_disposition < 1:
        parser.error("--per-disposition must be positive")
    jev_key = os.environ.get("TYPESAFEAI_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not jev_key:
        parser.error("set TYPESAFEAI_API_KEY (or TYPESAFE_API_KEY)")

    llm_mapping = default_llm_mappings(args.llm_provider)["standard_reasoning"]
    credentials = resolve_provider_credentials(llm_mapping.provider)
    llm_client = WorkrrProcedrrClient(
        build_workflow_client(
            credentials,
            model=llm_mapping.model,
            model_cache_dir=ROOT / ".powdrr" / "models",
            progress_stream=None,
        ),
        skills_dir=ROOT / "docs/procedrr/skill-definitions",
    )
    heldout_families = set(
        json.loads(HOLDOUT_FAMILIES.read_text(encoding="utf-8"))["source_family_ids"]
    )
    examples = _sample(_read_jsonl(DATASET), args.per_disposition, heldout_families)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = args.output_dir / "source_classifier_predictions.jsonl"
    existing = (
        {row["example_id"]: row for row in _read_jsonl(predictions_path)}
        if args.resume and predictions_path.exists()
        else {}
    )
    completed = []
    for index, row in enumerate(examples, start=1):
        result = existing.get(row["example_id"])
        if result is None:
            kinds = _kinds_for(row)
            llm = llm_client.complete_json(
                _llm_messages(row, kinds), response_schema=_request_schema(kinds)
            )
            jev = _call_jev(row, kinds, jev_key, args.timeout)
            decisions = {
                kind: {
                    "llm": {
                        "value": (
                            llm["decisions"][kind]["value"]
                            if llm["decisions"][kind]["status"] == "resolved"
                            else "unresolved"
                        ),
                        "reason_code": llm["decisions"][kind]["reason_code"],
                    },
                    "jev": jev["answers"][kind],
                }
                for kind in kinds
            }
            result = {
                "example_id": row["example_id"],
                "task_id": row["source"]["task_id"],
                "source_family_id": row["source"]["source_family_id"],
                "root_disposition_teacher_label": row["labels"]["class"],
                "proposition": row["inputs"]["proposition"],
                "llm_provider": credentials.provider,
                "llm_model": llm_mapping.model,
                "jev_model": jev["model"],
                "decisions": decisions,
            }
            existing[row["example_id"]] = result
            predictions_path.write_text(
                "".join(
                    json.dumps(existing[key], ensure_ascii=False, sort_keys=True) + "\n"
                    for key in existing
                ),
                encoding="utf-8",
            )
        completed.append(result)
        print(f"[{index}/{len(examples)}] {row['example_id']}", flush=True)

    report = {
        "schema_version": "jev-other-source-classifiers-comparison-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "reference": "fresh LLM outputs; not human gold labels",
        "llm_provider": credentials.provider,
        "llm_model": llm_mapping.model,
        "jev_model": JEV_MODEL,
        "example_count": len(completed),
        "sampling": {
            "seed": SEED,
            "per_teacher_disposition": args.per_disposition,
            "source_split": "non-held-out source families",
        },
        "tested_kinds": _comparison_summary(completed),
        "not_tested": {
            "candidate_relation": "requires a repository or ontology candidate pair",
            "entailment": "requires a source proposition and field assertion pair",
            "proposition_coverage": (
                "requires source-parent and child proposition pairs"
            ),
            "contract_observation_relation": "requires a pair of bound contracts",
            "source_extractors": (
                "span extraction is outside this categorical decision pilot"
            ),
        },
        "limitations": [
            "Agreement is between Jev and a fresh LLM reference, not accuracy against "
            "human labels.",
            "The LLM reference is a fresh run of DeepSeek V4 Flash, not an archived "
            "disposition label.",
            "The exact proposition is used as the behavior phrase for behavior_family "
            "in this pilot.",
            "Multiple typed decisions are requested in one call per provider for cost "
            "and latency control.",
        ],
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["tested_kinds"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
