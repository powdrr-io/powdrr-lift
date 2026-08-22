#!/usr/bin/env python3
"""Iteratively run the next workflow task and expose repair feedback."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from powdrr_lift.core import (
    AssigneeType,
    TaskStatus,
    WorkflowInstance,
    WorkflowTask,
    load_workflow_template,
)
from powdrr_lift.core.workflow_task_specification import save_workflow_task

DEFAULT_ERROR_LOG = Path("workflow-llm-errors.jsonl")
DEFAULT_TRANSCRIPT_DIR = Path("workflow-task-harness")
DEFAULT_REPORT = Path("workflow-task-harness-report.json")
_FAILURE_MARKERS = (
    "action failed",
    "validation_error",
    "validation error",
    "correction_required",
    "workflow task stopped",
    "workflow stopped",
    "repair failed",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-dir",
        type=Path,
        required=True,
        help="Directory containing the instantiated workflow task documents.",
    )
    parser.add_argument(
        "--template-path",
        type=Path,
        help=(
            "Workflow template used to create the instance. If omitted, the harness "
            "infers a unique template by matching task descriptions."
        ),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--iterations", type=int, default=10, help="Maximum repair iterations."
    )
    parser.add_argument(
        "--repair-command",
        help=(
            "Command run after a failed iteration. It receives HARNESS_* variables, "
            "including the instantiated task and source template paths."
        ),
    )
    parser.add_argument(
        "--task-id", help="Always retry this task instead of selecting the next one."
    )
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--max-roundtrips", type=int)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--no-isolate-run-worktree",
        action="store_true",
        help="Run directly in --repo-root instead of an ephemeral harness worktree.",
    )
    parser.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)
    parser.add_argument("--transcript-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def _log_root(repo_root: Path) -> Path:
    common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (repo_root / common_path).resolve()
    return common_path.parent


def _resolve_path(path: Path, *, log_root: Path) -> Path:
    return path if path.is_absolute() else log_root / path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


def _task_path(workflow_dir: Path, task: WorkflowTask) -> Path:
    for suffix in (".yaml", ".yml", ".json"):
        path = workflow_dir / f"{task.task_id}{suffix}"
        if path.is_file():
            return path
    raise SystemExit(f"Could not locate durable document for task {task.task_id!r}.")


def _select_task(workflow_dir: Path, task_id: str | None) -> WorkflowTask | None:
    workflow = WorkflowInstance.from_directory(workflow_dir)
    ready = workflow.ready_tasks(assignee_type=AssigneeType.AGENT)
    if task_id is not None:
        selected = next((task for task in ready if task.task_id == task_id), None)
        if selected is not None:
            return selected
        locked = next(
            (task for task in workflow.tasks if task.task_id == task_id), None
        )
        if locked is not None and locked.status is TaskStatus.LOCKED:
            return locked
        raise SystemExit(f"Task is not a ready or locked agent task: {task_id}")
    return ready[0] if ready else None


def _reopen_task(workflow_dir: Path, task_id: str) -> None:
    workflow = WorkflowInstance.from_directory(workflow_dir)
    task = next((item for item in workflow.tasks if item.task_id == task_id), None)
    if task is None:
        raise SystemExit(f"Task disappeared after execution: {task_id}")
    if task.status is not TaskStatus.LOCKED:
        return
    save_workflow_task(
        WorkflowTask(
            task_id=task.task_id,
            status=TaskStatus.OPEN,
            description=task.description,
            complexity=task.complexity,
            input_state=task.input_state,
            output_state=task.output_state,
            assignee_type=task.assignee_type,
            assignee_role=task.assignee_role,
            details=task.details,
            llm_type=task.llm_type,
            interaction_style=task.interaction_style,
            uses_skills=task.uses_skills,
            tool_invocations=task.tool_invocations,
            prompt_catalogs=task.prompt_catalogs,
            output_state_type=task.output_state_type,
            upstream_task_ids=task.upstream_task_ids,
            dependent_state=task.dependent_state,
            step_type=task.step_type,
            pre_step=task.pre_step,
            gate=task.gate,
        ),
        _task_path(workflow_dir, task),
    )


def _infer_template(repo_root: Path, workflow_dir: Path) -> Path:
    workflow = WorkflowInstance.from_directory(workflow_dir)
    descriptions = tuple(task.description for task in workflow.tasks)
    matches: list[Path] = []
    for path in sorted((*repo_root.rglob("*.yaml"), *repo_root.rglob("*.yml"))):
        if ".git" in path.parts or workflow_dir in path.parents:
            continue
        try:
            template = load_workflow_template(path)
        except (OSError, ValueError, TypeError):
            continue
        if tuple(task.description for task in template.task_templates) == descriptions:
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(
            "Could not infer the workflow template. Pass --template-path explicitly."
        )
    raise SystemExit(
        "Multiple workflow templates match this instance; pass --template-path: "
        + ", ".join(str(path) for path in matches)
    )


def _create_run_worktree(repo_root: Path) -> Path:
    run_root = Path(
        tempfile.mkdtemp(
            prefix=f"{repo_root.name}-workflow-task-run-", dir=repo_root.parent
        )
    )
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(run_root), "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return run_root


def _remove_run_worktree(repo_root: Path, run_root: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(run_root)],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if run_root.exists():
        shutil.rmtree(run_root, ignore_errors=True)


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        for child in source.rglob("*"):
            if child.is_file():
                target = destination / child.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _mirror_repair_changes(
    source_root: Path,
    destination_root: Path,
    *,
    workflow_dir: Path,
    template_path: Path,
) -> None:
    """Carry harness repairs and workflow state between isolated worktrees."""
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    allowed_prefixes = ("src/", "skill-definitions/", "scripts/", "templates/")
    for relative_name in changed:
        if not relative_name.startswith(allowed_prefixes):
            continue
        _copy_path(
            source_root / relative_name,
            destination_root / relative_name,
        )

    for path in (workflow_dir, template_path):
        try:
            relative_path = path.relative_to(source_root)
        except ValueError:
            continue
        _copy_path(path, destination_root / relative_path)


def _run_iteration(
    *,
    repo_root: Path,
    workflow_dir: Path,
    task: WorkflowTask,
    error_log: Path,
    transcript: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    previous_errors = len(_read_jsonl(error_log))
    command = [
        sys.executable,
        "-m",
        "powdrr_lift.cli",
        "process-workflow-task",
        "--repo-root",
        str(repo_root),
        "--workflow-dir",
        str(workflow_dir),
        "--task-id",
        task.task_id,
        "--provider",
        args.provider,
    ]
    if args.max_roundtrips is not None:
        command.extend(["--max-roundtrips", str(args.max_roundtrips)])
    if args.verbose:
        command.append("--verbose")
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    interrupted = False
    try:
        output, _ = process.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
        output = _text_output(exc.output) + _text_output(output)
    except KeyboardInterrupt:
        interrupted = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
    transcript.write_text(output or "", encoding="utf-8")
    new_errors = _read_jsonl(error_log)[previous_errors:]
    lowered = (output or "").lower()
    corrections = [marker for marker in _FAILURE_MARKERS if marker in lowered]
    status = "clean"
    if timed_out:
        status = "timeout"
    elif interrupted:
        status = "interrupted"
    elif process.returncode != 0 or new_errors or corrections:
        status = "failed"
    return {
        "status": status,
        "returncode": process.returncode,
        "task_id": task.task_id,
        "task_path": str(_task_path(workflow_dir, task)),
        "workflow_dir": str(workflow_dir),
        "transcript": str(transcript),
        "error_count": len(new_errors),
        "errors": new_errors,
        "corrections": corrections,
    }


def _run_repair_command(
    command_text: str,
    *,
    repo_root: Path,
    workflow_dir: Path,
    task: WorkflowTask,
    template_path: Path,
    error_log: Path,
    result: dict[str, Any],
    iteration: int,
) -> int:
    environment = {
        "HARNESS_ITERATION": str(iteration),
        "HARNESS_REPO_ROOT": str(repo_root),
        "HARNESS_WORKFLOW_DIR": str(workflow_dir),
        "HARNESS_TASK_ID": task.task_id,
        "HARNESS_TASK_PATH": str(_task_path(workflow_dir, task)),
        "HARNESS_TEMPLATE_PATH": str(template_path),
        "HARNESS_TRANSCRIPT": result["transcript"],
        "HARNESS_ERROR_LOG": str(error_log),
        "HARNESS_RESULT_JSON": json.dumps(result, ensure_ascii=False),
    }
    print(
        f"Running repair command after iteration {iteration}: {command_text}",
        file=sys.stderr,
    )
    return subprocess.run(
        shlex.split(command_text),
        cwd=repo_root,
        env={**os.environ, **environment},
        check=False,
    ).returncode


def main() -> int:
    args = _parse_args()
    if args.iterations < 1:
        raise SystemExit("--iterations must be positive")
    repo_root = args.repo_root.resolve()
    source_workflow_dir = args.workflow_dir
    if not source_workflow_dir.is_absolute():
        source_workflow_dir = repo_root / source_workflow_dir
    source_template_path = args.template_path
    if source_template_path is not None and not source_template_path.is_absolute():
        source_template_path = repo_root / source_template_path
    run_root = repo_root
    isolated = not args.no_isolate_run_worktree
    if isolated:
        run_root = _create_run_worktree(repo_root)
    try:
        workflow_dir = args.workflow_dir
        if not workflow_dir.is_absolute():
            workflow_dir = run_root / workflow_dir
        if isolated:
            _mirror_repair_changes(
                repo_root,
                run_root,
                workflow_dir=source_workflow_dir,
                template_path=source_template_path
                or repo_root / "__missing-template__",
            )
        template_path = args.template_path
        if template_path is not None and not template_path.is_absolute():
            template_path = run_root / template_path
        if template_path is None:
            template_path = _infer_template(run_root, workflow_dir)
        if not template_path.is_file():
            raise SystemExit(f"Workflow template does not exist: {template_path}")

        log_root = _log_root(run_root)
        error_log = _resolve_path(args.error_log, log_root=log_root)
        transcript_dir = _resolve_path(args.transcript_dir, log_root=log_root)
        report_path = _resolve_path(args.report, log_root=log_root)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        reports: list[dict[str, Any]] = []
        for iteration in range(1, args.iterations + 1):
            task = _select_task(workflow_dir, args.task_id)
            if task is None:
                print("No ready agent task found; workflow may be complete.")
                report_path.write_text(
                    json.dumps(reports, indent=2) + "\n", encoding="utf-8"
                )
                return 0
            result = _run_iteration(
                repo_root=run_root,
                workflow_dir=workflow_dir,
                task=task,
                error_log=error_log,
                transcript=transcript_dir / f"iteration-{iteration:02d}.log",
                args=args,
            )
            result["iteration"] = iteration
            result["template_path"] = str(template_path)
            reports.append(result)
            if isolated:
                _mirror_repair_changes(
                    run_root,
                    repo_root,
                    workflow_dir=workflow_dir,
                    template_path=template_path,
                )
            print(
                f"Iteration {iteration}: {result['status']} "
                f"(task={task.task_id}, errors={result['error_count']})",
                file=sys.stderr,
            )
            if result["status"] == "clean":
                report_path.write_text(
                    json.dumps(reports, indent=2) + "\n", encoding="utf-8"
                )
                print(f"Harness completed cleanly after {iteration} iteration(s).")
                return 0
            _reopen_task(workflow_dir, task.task_id)
            if result["status"] in {"timeout", "interrupted"}:
                break
            if not args.repair_command:
                print(
                    "Iteration failed; inspect the transcript and structured errors.",
                    file=sys.stderr,
                )
                break
            repair_returncode = _run_repair_command(
                args.repair_command,
                repo_root=run_root,
                workflow_dir=workflow_dir,
                task=task,
                template_path=template_path,
                error_log=error_log,
                result=result,
                iteration=iteration,
            )
            if isolated:
                _mirror_repair_changes(
                    run_root,
                    repo_root,
                    workflow_dir=workflow_dir,
                    template_path=template_path,
                )
            if repair_returncode != 0:
                print("Repair command failed; stopping harness.", file=sys.stderr)
                break
        report_path.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
        print(
            f"Harness did not complete cleanly; report: {report_path}", file=sys.stderr
        )
        return 1
    finally:
        if isolated:
            _remove_run_worktree(repo_root, run_root)


if __name__ == "__main__":
    raise SystemExit(main())
