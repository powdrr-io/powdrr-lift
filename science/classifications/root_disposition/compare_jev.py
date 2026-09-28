#!/usr/bin/env python3
"""Compare Jev root disposition predictions with teacher and human labels.

The default input is the existing double-label packet. The human labels are
currently single-reviewer labels until the second sheet is completed and
disagreements are adjudicated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
BATCH_DIR = Path(__file__).resolve().parent / "adjudication"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "jev-comparison"
API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
LABELS = (
    "entity",
    "feature",
    "interface",
    "invariant",
    "guidance",
    "non_goal",
    "nonactionable",
    "context",
    "unresolved",
)
CRITERIA = {
    "entity": (
        "Names or defines a product concept, domain object, actor, type, or component."
    ),
    "feature": "Describes a concrete product capability or behavior.",
    "interface": (
        "Specifies an externally visible boundary contract, API shape, or "
        "caller communication."
    ),
    "invariant": (
        "States a rule or property that must hold generally across cases or "
        "a lifecycle."
    ),
    "guidance": (
        "Expresses a preference or recommendation without establishing "
        "definite behavior."
    ),
    "non_goal": "Explicitly excludes or prohibits product behavior.",
    "nonactionable": (
        "Concerns only process, delivery, repository handling, or tools, "
        "with no product semantics."
    ),
    "context": (
        "Gives background, motivation, or a problem statement without "
        "requesting or defining product behavior."
    ),
    "unresolved": (
        "Does not support one defensible label, is underspecified, mixes incompatible "
        "roles after atomic splitting, or is only an incomplete extraction fragment "
        "or structural debris."
    ),
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def request_jev(proposition: str, api_key: str, *, timeout: float) -> dict[str, Any]:
    body = {
        "model": MODEL,
        "state": {"proposition": proposition},
        "questions": {
            "disposition": {
                "type": "choice",
                "instructions": (
                    "Classify the exact proposition in isolation using the given "
                    "criteria. Do not infer unstated context. Choose unresolved "
                    "when no single label is defensible or the text is only "
                    "an incomplete extraction fragment."
                ),
                "criteria": CRITERIA,
            }
        },
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
            answer = payload["answers"]["disposition"]
            label = answer.get("choice")
            if label not in LABELS:
                raise ValueError(f"Jev returned unsupported disposition: {label!r}")
            return {
                "label": label,
                "confidence": answer.get("confidence"),
                "probabilities": answer.get("probabilities"),
                "model": payload.get("model", MODEL),
                "usage": payload.get("usage", {}),
            }
        except HTTPError as exc:
            if exc.code not in (429, 529) or attempt == 4:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Jev API returned HTTP {exc.code}: {detail}"
                ) from None
        except (URLError, TimeoutError) as exc:
            if attempt == 4:
                raise RuntimeError(
                    "Jev API request failed: "
                    f"{exc.reason if isinstance(exc, URLError) else exc}"
                ) from None
        time.sleep(2**attempt)
    raise RuntimeError("Jev API retry budget exhausted")


def score(
    rows: list[dict[str, Any]], prediction_field: str, gold_field: str
) -> dict[str, Any]:
    comparable = [row for row in rows if row.get(gold_field) in LABELS]
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for row in comparable:
        matrix[row[gold_field]][row[prediction_field]] += 1
    return {
        "n": len(comparable),
        "accuracy": (
            sum(row[prediction_field] == row[gold_field] for row in comparable)
            / len(comparable)
            if comparable
            else None
        ),
        "confusion_matrix": {
            gold: dict(sorted(counts.items()))
            for gold, counts in sorted(matrix.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotator", choices=("a", "b"), default="a")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.model != MODEL:
        parser.error("this pilot currently pins the documented jev-latest alias")
    api_key = os.environ.get("TYPESAFEAI_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        parser.error("set TYPESAFEAI_API_KEY (or the SDK-documented TYPESAFE_API_KEY)")

    reviewer = read_jsonl(BATCH_DIR / f"annotator_{args.annotator}.jsonl")
    selection = {
        row["review_id"]: row for row in read_jsonl(BATCH_DIR / "selection_key.jsonl")
    }
    dataset = {
        row["example_id"]: row
        for row in read_jsonl(
            ROOT
            / "science/classifications/root_disposition/data/root_disposition.jsonl"
        )
    }
    examples: list[dict[str, Any]] = []
    for review in reviewer:
        if not review.get("label"):
            continue
        key = selection[review["review_id"]]
        source = dataset[key["example_id"]]
        teacher = key["teacher_stratum"]
        human_label = (
            "unresolved"
            if review["label"] == "formatting_artifact"
            else review["label"]
        )
        examples.append(
            {
                "review_id": review["review_id"],
                "example_id": key["example_id"],
                "proposition": review["proposition"],
                "teacher_label": teacher,
                "human_label": human_label,
                "human_raw_label": review["label"],
                "human_confidence": review.get("confidence"),
                "source_family_id": source["source"]["source_family_id"],
            }
        )
    if args.limit is not None:
        examples = examples[: args.limit]
    if not examples:
        parser.error(f"annotator {args.annotator} has no completed labels to compare")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = args.output_dir / f"annotator_{args.annotator}_jev.jsonl"
    existing = (
        {row["review_id"]: row for row in read_jsonl(predictions_path)}
        if args.resume and predictions_path.exists()
        else {}
    )
    results: list[dict[str, Any]] = []
    for index, item in enumerate(examples, start=1):
        result = existing.get(item["review_id"])
        if result is None:
            prediction = request_jev(item["proposition"], api_key, timeout=args.timeout)
            result = {**item, "jev_label": prediction["label"], **prediction}
            existing[item["review_id"]] = result
            predictions_path.write_text(
                "".join(
                    json.dumps(existing[key], ensure_ascii=False, sort_keys=True) + "\n"
                    for key in existing
                ),
                encoding="utf-8",
            )
        results.append(result)
        print(
            f"[{index}/{len(examples)}] {item['review_id']}: {result['jev_label']}",
            flush=True,
        )

    report = {
        "schema_version": "jev-root-disposition-comparison-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "model": MODEL,
        "endpoint": API_URL,
        "human_source": f"annotator_{args.annotator}",
        "human_status": "single_reviewer_not_adjudicated",
        "human_label_normalization": {
            "formatting_artifact": "unresolved",
            "reason": (
                "The production root-disposition schema has no "
                "formatting_artifact value."
            ),
        },
        "example_count": len(results),
        "agreement": {
            "jev_vs_teacher_llm": score(results, "jev_label", "teacher_label"),
            "jev_vs_human": score(results, "jev_label", "human_label"),
            "teacher_llm_vs_human": score(results, "teacher_label", "human_label"),
        },
        "label_counts": {
            source: dict(sorted(Counter(row[field] for row in results).items()))
            for source, field in (
                ("jev", "jev_label"),
                ("teacher_llm", "teacher_label"),
                ("human", "human_label"),
            )
        },
        "mean_jev_confidence": sum(float(row["confidence"]) for row in results)
        / len(results),
        "mean_input_tokens": sum(
            row.get("usage", {}).get("input_tokens", 0) for row in results
        )
        / len(results),
    }
    (args.output_dir / f"annotator_{args.annotator}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["agreement"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
