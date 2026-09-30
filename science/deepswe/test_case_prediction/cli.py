"""Command line tools for the DeepSWE test prediction pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .evaluation import prepare_review, score_files, write_json
from .predictor import predict_file, write_predictions
from .records import collect_records, load_json, load_task_record

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser(
        "collect", help="Build task records and audit data coverage."
    )
    collect.add_argument("--tasks-dir", required=True, type=Path)
    collect.add_argument("--repository-roots", type=Path)
    collect.add_argument("--output-dir", required=True, type=Path)

    predict = commands.add_parser(
        "predict", help="Predict scenarios for one task record."
    )
    predict.add_argument("--record", required=True, type=Path)
    predict.add_argument("--output", required=True, type=Path)
    predict.add_argument("--provider", default="deepinfra")
    predict.add_argument("--model", default=DEFAULT_MODEL)

    review = commands.add_parser(
        "prepare-review", help="Create a pairwise human semantic review sheet."
    )
    review.add_argument("--record", required=True, type=Path)
    review.add_argument("--predictions", required=True, type=Path)
    review.add_argument("--output", required=True, type=Path)

    score = commands.add_parser("score", help="Score completed review sheets.")
    score.add_argument("--records-dir", required=True, type=Path)
    score.add_argument("--predictions-dir", required=True, type=Path)
    score.add_argument("--reviews-dir", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            from .records import load_repository_roots

            audit = collect_records(
                tasks_dir=args.tasks_dir,
                repository_roots=load_repository_roots(args.repository_roots),
                output_dir=args.output_dir,
            )
            _print(
                {
                    "unique_task_count": audit["unique_task_count"],
                    "tasks_with_patch_test_cases": audit["tasks_with_patch_test_cases"],
                    "validation_metadata_available": audit[
                        "validation_metadata_available"
                    ],
                    "ground_truth_availability": audit["ground_truth_availability"],
                    "audit_path": str(args.output_dir / "audit.json"),
                }
            )
            return 0
        if args.command == "predict":
            predictions = predict_file(
                args.record,
                provider=args.provider,
                model=args.model,
            )
            write_predictions(predictions, args.output)
            _print(
                {
                    "task_id": predictions["task_id"],
                    "prediction_count": len(predictions["cases"]),
                    "estimated_test_case_count": predictions["test_count_prediction"][
                        "estimated_test_case_count"
                    ],
                    "obligation_count": len(predictions["obligations"]),
                }
            )
            return 0
        if args.command == "prepare-review":
            record = load_task_record(args.record)
            predictions = load_json(args.predictions)
            review_sheet = prepare_review(record, predictions)
            write_json(review_sheet, args.output)
            _print(
                {
                    "task_id": record["task_id"],
                    "pair_count": len(review_sheet["pairs"]),
                    "output": str(args.output),
                }
            )
            return 0
        if args.command == "score":
            report = score_files(
                _task_record_paths(args.records_dir),
                _json_paths(args.predictions_dir),
                _json_paths(args.reviews_dir),
            )
            write_json(report, args.output)
            _print(report["aggregate"])
            return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _task_record_paths(directory: Path) -> list[Path]:
    paths = []
    for path in _json_paths(directory):
        try:
            load_task_record(path)
        except ValueError:
            continue
        paths.append(path)
    return paths


def _json_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f"directory does not exist: {directory}")
    return sorted(directory.glob("*.json"))


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
