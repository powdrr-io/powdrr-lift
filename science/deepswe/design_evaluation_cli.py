"""Run offline DeepSWE reference-based design and prompt evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.chat_agent import resolve_workflow_provider
from powdrr_lift.workrr.provider_config import default_llm_mappings
from powdrr_lift.workrr.providers import (
    build_workflow_client,
    resolve_provider_credentials,
)

from .design_evaluation import (
    DEFAULT_STATE_DATA_RUBRIC,
    DeepSWEEvaluationError,
    evaluate_deepswe_design,
    evaluate_deepswe_worker_prompt,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("design", "Evaluate a completed design against reference patches."),
        ("prompt", "Evaluate captured worker prompts against reference patches."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--task-dir", required=True, type=Path)
        command.add_argument("--run-dir", required=True, type=Path)
        command.add_argument("--rubric", type=Path, default=DEFAULT_STATE_DATA_RUBRIC)
        command.add_argument("--report", type=Path)
        command.add_argument("--judge-provider", default="deepinfra-cheap")
        command.add_argument("--judge-model")
        command.add_argument("--judge-api-key")
        command.add_argument("--judge-base-url")
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    provider = resolve_workflow_provider(args.judge_provider)
    mapping = default_llm_mappings(provider)["standard_reasoning"]
    try:
        credentials = resolve_provider_credentials(
            mapping.provider, args.judge_api_key, args.judge_base_url
        )
        model = args.judge_model or mapping.model
        judge = build_workflow_client(
            credentials,
            model=model,
            model_cache_dir=args.run_dir / ".models",
            progress_stream=sys.stderr,
        )
        evaluator = (
            evaluate_deepswe_design
            if args.command == "design"
            else evaluate_deepswe_worker_prompt
        )
        report = evaluator(
            task_dir=args.task_dir,
            run_dir=args.run_dir,
            judge=judge,
            rubric_path=args.rubric,
            judge_id=f"{mapping.provider}/{model}",
        )
    except (DeepSWEEvaluationError, PowdrrExecutionError) as error:
        print(f"DeepSWE {args.command} evaluation failed: {error}", file=sys.stderr)
        return 1

    default_name = (
        "design-quality-evaluation.json"
        if args.command == "design"
        else "prompt-quality-evaluation.json"
    )
    report_path = args.report or args.run_dir / default_name
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        summary = report["summary"]
        label = "design" if args.command == "design" else "worker-prompt"
        print(
            f"DeepSWE {label} evaluation {report['task_id']}: "
            f"{summary['supported_count']}/{summary['criterion_count']} supported; "
            f"weighted coverage {summary['weighted_coverage']:.1%}; "
            f"critical failures {len(summary['critical_failures'])}."
        )
        print(f"Report: {report_path}")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
