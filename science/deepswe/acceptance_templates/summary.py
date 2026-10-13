"""Combine disjoint pilot cohorts without hiding missing pairs or label provenance."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import digest, load_json, write_json
from .evaluation import aggregate, report_markdown


def combine(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ValueError("no reports")
    seen = set()
    scores = []
    failures = []
    task_ids = set()
    configs = []
    for report in reports:
        run = report["run"]
        configs.append(
            {
                key: run[key]
                for key in (
                    "provider",
                    "model",
                    "catalog_sha256",
                    "batch_size",
                    "output_tokens",
                    "repairs",
                )
            }
        )
        task_ids.update(run["tasks"])
        failures.extend(report["failures"])
        for row in report["scores"]:
            pair = (row["task_id"], row["arm"])
            if pair in seen:
                raise ValueError("repeated task/arm in combined reports")
            seen.add(pair)
            scores.append(row)
    if len({digest(config) for config in configs}) != 1:
        raise ValueError(
            "cohorts use different generation settings; compare separately"
        )
    paired = []
    for tid in sorted(task_ids):
        rows = [row for row in scores if row["task_id"] == tid]
        if {row["arm"] for row in rows} == {"direct", "templates"}:
            if (
                len({row["input_sha256"] for row in rows}) != 1
                or len({row["reference_sha256"] for row in rows}) != 1
            ):
                raise ValueError(
                    "paired arms must have identical inputs and references"
                )
            paired.append(tid)
    levels = sorted(
        {(row["review_status"], row["reference_label_status"]) for row in scores}
    )
    return {
        "cohort_report_sha256": [digest(report) for report in reports],
        "generation_settings": configs[0],
        "eligible_task_count": len(task_ids),
        "paired_task_ids": paired,
        "aggregate": aggregate(scores),
        "paired_aggregate": aggregate(
            [row for row in scores if row["task_id"] in paired]
        ),
        "scores_by_evidence_level": {
            f"{review}/{reference}": aggregate(
                [
                    row
                    for row in scores
                    if row["review_status"] == review
                    and row["reference_label_status"] == reference
                ]
            )
            for review, reference in levels
        },
        "scores": scores,
        "failures": failures,
        "limitations": reports[0]["limitations"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = combine([load_json(path) for path in args.reports])
    write_json(args.output_dir / "report.json", report)
    report_markdown(report, args.output_dir / "report.md")
    print(report["paired_aggregate"])


if __name__ == "__main__":
    main()
