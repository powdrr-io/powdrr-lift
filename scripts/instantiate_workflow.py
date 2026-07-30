#!/usr/bin/env python3
"""Instantiate a workflow template as validated durable task JSON files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from powdrr_lift.core.workflow_task_specification import (
    TaskStatus,
    WorkflowTask,
    build_workflow_task_directory_validation_report,
    save_workflow_task,
    select_ready_workflow_tasks,
)
from powdrr_lift.core.workflow_template_specification import load_workflow_template


def _work_item_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("work-item-name must contain at least one letter or digit")
    return slug


def instantiate_workflow(
    *,
    template_path: Path,
    work_item_name: str,
    output_root: Path,
) -> tuple[Path, tuple[WorkflowTask, ...]]:
    template = load_workflow_template(template_path)
    output_dir = output_root / _work_item_slug(work_item_name)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"Workflow output directory is not empty: {output_dir}. "
            "Choose a new work item or remove it with explicit approval."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    task_ids = tuple(
        f"task-{index + 1:03d}" for index in range(len(template.task_templates))
    )
    tasks: list[WorkflowTask] = []
    for index, task_template in enumerate(template.task_templates):
        upstream_task_ids = tuple(
            task_ids[upstream_index]
            for upstream_index in task_template.upstream_task_template_indexes
        )
        task = WorkflowTask(
            task_id=task_ids[index],
            status=TaskStatus.OPEN,
            description=task_template.description,
            complexity=task_template.complexity,
            input_state=task_template.input_state,
            output_state_type=task_template.output_state_type,
            upstream_task_ids=upstream_task_ids,
            dependent_state=task_template.dependent_state,
        )
        save_workflow_task(task, output_dir / f"{task.task_id}.json")
        tasks.append(task)

    report = build_workflow_task_directory_validation_report(output_dir)
    if not report.validation_successful:
        issues = [issue.message for issue in report.issues]
        raise ValueError("Generated workflow failed validation: " + "; ".join(issues))

    ready_tasks = select_ready_workflow_tasks(tuple(tasks))
    if len(ready_tasks) != 1 or ready_tasks[0].task_id != task_ids[0]:
        raise ValueError(
            "Generated workflow must have exactly one ready first task; "
            f"found {[task.task_id for task in ready_tasks]}"
        )
    return output_dir, tuple(tasks)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Instantiate a workflow template as durable task JSON files."
    )
    parser.add_argument("--work-item-name", required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("docs") / "workflows",
    )
    args = parser.parse_args()

    template_path = args.template.resolve()
    output_root = args.output_root.resolve()
    try:
        output_dir, tasks = instantiate_workflow(
            template_path=template_path,
            work_item_name=args.work_item_name,
            output_root=output_root,
        )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"Could not instantiate workflow: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "workflow_directory": str(output_dir),
                "task_count": len(tasks),
                "first_task": str(output_dir / f"{tasks[0].task_id}.json"),
                "first_task_id": tasks[0].task_id,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
