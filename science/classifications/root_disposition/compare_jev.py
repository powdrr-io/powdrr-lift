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
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "jev-comparison-v2"
PROMPT_REVISION = "root-disposition-rubric-examples-v2"
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
    "feature": (
        "Describes a product capability or behavior. This is the default for "
        "a concrete product action that is not mainly an API contract or a "
        "rule that must hold across a population."
    ),
    "interface": (
        "Specifies an externally visible boundary contract: public API shape, "
        "signatures, inputs, outputs, callbacks, or how callers and the "
        "product communicate."
    ),
    "invariant": (
        "States a rule or property expected to hold generally across a population, "
        "operations, or lifecycle. Do not choose this only because the text says "
        "'all', 'every', or 'always'."
    ),
    "guidance": (
        "Expresses a preference or recommendation without establishing definite "
        "product behavior. Words such as 'should' alone do not decide this label."
    ),
    "non_goal": (
        "Explicitly excludes or prohibits product behavior. Product meaning "
        "takes precedence over delivery wording."
    ),
    "nonactionable": (
        "Concerns only process, delivery, repository handling, or tools, with "
        "no product semantics."
    ),
    "context": (
        "Gives factual background, motivation, or a problem statement without "
        "requesting or defining product behavior."
    ),
    "unresolved": (
        "Does not support one defensible label, is underspecified, mixes incompatible "
        "roles after atomic splitting, or is only an incomplete extraction fragment "
        "or structural debris."
    ),
}
BOUNDARY_RULES_AND_EXAMPLES = "\n".join(
    (
        "Apply these boundary rules:",
        "- Desired behavior can be phrased as a statement, not an imperative.",
        "- Classify the exact proposition in isolation; do not infer context.",
        "- Do not classify polarity, strength, quantifier, or implementation details.",
        "- Prefer interface for an external contract; feature for product behavior.",
        "- Invariant means a general property expected to hold across cases.",
        "  The words all, every, and always do not establish an invariant alone.",
        "- Guidance is a preference without definite behavior.",
        "  Should alone does not decide this label.",
        "- Context explains a problem; feature asks for product behavior or change.",
        "- Product meaning takes precedence over delivery wording.",
        "  A callback restriction is non_goal; a pull request is nonactionable.",
        "- formatting_artifact is a reviewer-only label mapped to unresolved here.",
        "",
        "Synthetic examples authored from the rubric, not the review batch:",
        "- Users can export reports. -> feature.",
        "- Every response has an ID. -> invariant.",
        "- Expose get_state_data(state). -> interface.",
        "- Callers can retrieve current state data. -> feature.",
        "- Prefer immutable defaults. -> guidance.",
        "- Defaults stay immutable throughout each request. -> invariant.",
        "- Without a lifecycle, callers manage values manually. -> context.",
        "- The system resets state data when a state is entered. -> feature.",
        "- Do not add CSV export. -> non_goal.",
        "- Run the unit tests before submitting. -> nonactionable.",
        "- 2. -> unresolved (incomplete extraction fragment).",
        "",
        "Choose one label. Use unresolved if no single label is defensible.",
    )
)


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
                "instructions": BOUNDARY_RULES_AND_EXAMPLES,
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


def confidence_policy_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure selective Jev and Jev-with-teacher-fallback agreement."""
    results: dict[str, Any] = {}
    for threshold in (0.5, 0.6, 0.7, 0.8, 0.9):
        accepted = [row for row in rows if float(row["confidence"]) >= threshold]
        hybrid_correct = sum(
            (
                row["jev_label"]
                if float(row["confidence"]) >= threshold
                else row["teacher_label"]
            )
            == row["human_label"]
            for row in rows
        )
        results[f"{threshold:.1f}"] = {
            "accepted_count": len(accepted),
            "coverage": len(accepted) / len(rows),
            "jev_accuracy_on_accepted": (
                sum(row["jev_label"] == row["human_label"] for row in accepted)
                / len(accepted)
                if accepted
                else None
            ),
            "teacher_fallback_hybrid_accuracy": hybrid_correct / len(rows),
        }
    return results


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
        "schema_version": "jev-root-disposition-comparison-v2",
        "prompt_revision": PROMPT_REVISION,
        "evaluation_status": "exploratory_same_batch_after_prompt_revision",
        "prompt_development": (
            "The revision uses the full written rubric and synthetic examples. "
            "Its boundaries were informed by disagreements in the baseline batch."
        ),
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
        "confidence_policy_analysis": confidence_policy_analysis(results),
        "confidence_policy_note": (
            "Threshold results are exploratory on a previously inspected batch; "
            "do not use them as a production threshold without a fresh "
            "human-labeled evaluation."
        ),
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
