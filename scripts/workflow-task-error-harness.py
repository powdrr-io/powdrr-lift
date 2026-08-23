#!/usr/bin/env python3
"""Run a durable workflow to completion or a human handoff with bounded repair."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
from powdrr_lift.workflow_git import (
    load_workflow_git_state,
    resolve_git_repository_root,
    slugify_workflow_id,
    workflow_worktree_path,
)

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
        "--iterations",
        type=int,
        default=10,
        help="Maximum bounded workflow runs, including repair retries.",
    )
    parser.add_argument(
        "--repair-command",
        help=(
            "Command run after a failed or diagnostic iteration. It receives "
            "HARNESS_* variables, including workflow state, errors, corrections, "
            "the instantiated workflow, and source template paths."
        ),
    )
    parser.add_argument(
        "--task-id",
        help=(
            "Run only this task; by default the durable runner processes the "
            "full workflow."
        ),
    )
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--max-roundtrips", type=int)
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help=(
            "Hard timeout for each full workflow run; the process group is "
            "killed on expiry."
        ),
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=15.0,
        help="Seconds between heartbeat messages when the task emits no output.",
    )
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


def _active_workflow_dir(repo_root: Path, workflow_dir: Path) -> Path:
    """Resolve the durable graph where process-workflow-task actually writes."""
    state = load_workflow_git_state(workflow_dir)
    if state is None:
        return workflow_dir
    try:
        project_root = resolve_git_repository_root(repo_root)
        integration_dir = workflow_worktree_path(
            project_root,
            state.proposed_pr_id,
        )
    except (OSError, RuntimeError, ValueError):
        return workflow_dir
    active_dir = integration_dir / state.workflow_relative_directory
    return active_dir if active_dir.is_dir() else workflow_dir


def _workflow_state(workflow_dir: Path) -> dict[str, Any]:
    """Return a stable, JSON-serializable snapshot of the durable workflow."""
    workflow = WorkflowInstance.from_directory(workflow_dir)
    tasks = [
        {
            "task_id": task.task_id,
            "status": task.status.value,
            "assignee_type": task.assignee_type.value,
            "upstream_task_ids": list(task.upstream_task_ids),
        }
        for task in workflow.tasks
    ]
    ready_agent = [
        task.task_id for task in workflow.ready_tasks(assignee_type=AssigneeType.AGENT)
    ]
    ready_human = [
        task.task_id for task in workflow.ready_tasks(assignee_type=AssigneeType.HUMAN)
    ]
    locked = [task["task_id"] for task in tasks if task["status"] == "locked"]
    all_completed = bool(tasks) and all(
        task["status"] == TaskStatus.COMPLETED.value for task in tasks
    )
    if all_completed:
        outcome = "completed"
    elif ready_human:
        outcome = "human_handoff"
    elif ready_agent:
        outcome = "agent_work_remaining"
    elif locked:
        outcome = "agent_task_locked"
    else:
        outcome = "stalled"
    return {
        "outcome": outcome,
        "tasks": tasks,
        "ready_agent_task_ids": ready_agent,
        "ready_human_task_ids": ready_human,
        "locked_task_ids": locked,
    }


def _reopen_locked_tasks(
    repo_root: Path,
    workflow_dir: Path,
    task_ids: list[str],
) -> None:
    """Make failed agent tasks retryable without touching human handoffs."""
    workflow = WorkflowInstance.from_directory(workflow_dir)
    state = load_workflow_git_state(workflow_dir)
    for task_id in task_ids:
        task = next((item for item in workflow.tasks if item.task_id == task_id), None)
        if task is None or task.assignee_type is not AssigneeType.AGENT:
            continue
        if task.status is TaskStatus.LOCKED:
            _reopen_task(workflow_dir, task_id)
        if state is not None:
            claim_ref = (
                f"refs/agents/claims/{slugify_workflow_id(state.proposed_pr_id)}/"
                f"{slugify_workflow_id(task_id)}"
            )
            subprocess.run(
                ["git", "update-ref", "-d", claim_ref],
                cwd=repo_root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _state_fingerprint(state: dict[str, Any]) -> str:
    return json.dumps(
        {
            "outcome": state["outcome"],
            "tasks": state["tasks"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


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


def _build_task_command(
    *,
    repo_root: Path,
    workflow_dir: Path,
    task: WorkflowTask | None,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "powdrr_lift.cli",
        "process-workflow-task",
        "--repo-root",
        str(repo_root),
        "--workflow-dir",
        str(workflow_dir),
        "--provider",
        args.provider,
    ]
    if task is not None:
        command.extend(["--task-id", task.task_id])
    if args.max_roundtrips is not None:
        command.extend(["--max-roundtrips", str(args.max_roundtrips)])
    if args.verbose:
        command.append("--verbose")
    return command


def _run_iteration(
    *,
    repo_root: Path,
    workflow_dir: Path,
    task: WorkflowTask | None,
    error_logs: tuple[Path, ...],
    transcript: Path,
    args: argparse.Namespace,
    state_workflow_dir: Path | None = None,
) -> dict[str, Any]:
    previous_error_counts = {path: len(_read_jsonl(path)) for path in error_logs}
    command = _build_task_command(
        repo_root=repo_root,
        workflow_dir=workflow_dir,
        task=task,
        args=args,
    )
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        text=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    interrupted = False
    output_parts: list[str] = []
    transcript.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    last_heartbeat = started
    captured_bytes = 0
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    with transcript.open("w", encoding="utf-8") as transcript_file:
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= args.timeout:
                timed_out = True
                print(
                    f"Harness timeout after {now - started:.0f}s; terminating "
                    f"process group (captured={captured_bytes} bytes).",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            try:
                ready = selector.select(timeout=1.0)
            except KeyboardInterrupt:
                interrupted = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break
            if ready:
                chunk = process.stdout.read1(65536)
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                output = _text_output(chunk)
                output_parts.append(output)
                captured_bytes += len(chunk)
                transcript_file.write(output)
                transcript_file.flush()
                sys.stdout.write(output)
                sys.stdout.flush()
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= args.progress_interval:
                elapsed = time.monotonic() - started
                task_label = task.task_id if task is not None else "full workflow"
                print(
                    f"Harness progress: {task_label} still running after "
                    f"{elapsed:.0f}s; captured={captured_bytes} bytes.",
                    file=sys.stderr,
                    flush=True,
                )
                last_heartbeat = time.monotonic()
        if process.poll() is None:
            process.wait()
        while process.stdout is not None:
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            output = _text_output(chunk)
            output_parts.append(output)
            captured_bytes += len(chunk)
            transcript_file.write(output)
            transcript_file.flush()
            sys.stdout.write(output)
            sys.stdout.flush()
    selector.close()
    output = "".join(output_parts)
    new_errors: list[dict[str, Any]] = []
    for path in error_logs:
        new_errors.extend(_read_jsonl(path)[previous_error_counts[path] :])
    lowered = (output or "").lower()
    corrections = [marker for marker in _FAILURE_MARKERS if marker in lowered]
    workflow_state = _workflow_state(state_workflow_dir or workflow_dir)
    status = "clean"
    if timed_out:
        status = "timeout"
    elif interrupted:
        status = "interrupted"
    elif process.returncode != 0 or new_errors:
        status = "failed"
    elif workflow_state["outcome"] == "stalled":
        status = "stalled"
    elif workflow_state["outcome"] == "agent_task_locked":
        status = "failed"
    elif workflow_state["outcome"] == "agent_work_remaining":
        status = "incomplete"
    return {
        "status": status,
        "returncode": process.returncode,
        "task_id": task.task_id if task is not None else None,
        "task_path": (
            str(_task_path(state_workflow_dir or workflow_dir, task))
            if task is not None
            else None
        ),
        "workflow_dir": str(workflow_dir),
        "transcript": str(transcript),
        "error_count": len(new_errors),
        "errors": new_errors,
        "corrections": corrections,
        "workflow_state": workflow_state,
    }


def _run_repair_command(
    command_text: str,
    *,
    repo_root: Path,
    workflow_dir: Path,
    task: WorkflowTask | None,
    template_path: Path,
    error_log: Path,
    result: dict[str, Any],
    iteration: int,
    timeout: float,
) -> int:
    task_root = _active_workflow_dir(repo_root, workflow_dir)
    environment = {
        "HARNESS_ITERATION": str(iteration),
        "HARNESS_REPO_ROOT": str(repo_root),
        "HARNESS_WORKFLOW_DIR": str(workflow_dir),
        "HARNESS_TASK_ID": task.task_id if task is not None else "",
        "HARNESS_TASK_PATH": (
            str(_task_path(task_root, task)) if task is not None else ""
        ),
        "HARNESS_TEMPLATE_PATH": str(template_path),
        "HARNESS_TRANSCRIPT": result["transcript"],
        "HARNESS_ERROR_LOG": str(error_log),
        "HARNESS_ERROR_LOGS": os.pathsep.join(
            str(path) for path in result.get("error_logs", [])
        ),
        "HARNESS_WORKFLOW_STATE": json.dumps(
            result.get("workflow_state_after", result.get("workflow_state", {})),
            ensure_ascii=False,
        ),
        "HARNESS_CORRECTIONS": json.dumps(
            result.get("corrections", []), ensure_ascii=False
        ),
        "HARNESS_IMPROVEMENT_TARGETS": json.dumps(
            {
                "template": str(template_path),
                "workflow": str(workflow_dir),
                "task": (
                    str(_task_path(task_root, task)) if task is not None else None
                ),
            },
            ensure_ascii=False,
        ),
        "HARNESS_RESULT_JSON": json.dumps(result, ensure_ascii=False),
    }
    print(
        f"Running repair command after iteration {iteration}: {command_text}",
        file=sys.stderr,
    )
    process = subprocess.Popen(
        shlex.split(command_text),
        cwd=repo_root,
        env={**os.environ, **environment},
        start_new_session=(os.name != "nt"),
    )
    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(
            f"Repair command timed out after {timeout:g}s; terminating process group.",
            file=sys.stderr,
        )
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        return 124
    except KeyboardInterrupt:
        if os.name == "nt":
            process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.wait()
        return 130
    return process.returncode


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
        runner_error_logs = (
            log_root / DEFAULT_ERROR_LOG,
            run_root / DEFAULT_ERROR_LOG,
            run_root.parent / DEFAULT_ERROR_LOG,
            run_root.parent.parent / DEFAULT_ERROR_LOG,
        )
        error_logs = tuple(dict.fromkeys((error_log, *runner_error_logs)))
        transcript_dir = _resolve_path(args.transcript_dir, log_root=log_root)
        report_path = _resolve_path(args.report, log_root=log_root)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        reports: list[dict[str, Any]] = []
        previous_fingerprint: str | None = None
        repeated_state_count = 0
        for iteration in range(1, args.iterations + 1):
            observed_workflow_dir = _active_workflow_dir(run_root, workflow_dir)
            before = _workflow_state(observed_workflow_dir)
            if before["outcome"] in {"completed", "human_handoff"}:
                reports.append(
                    {
                        "iteration": iteration,
                        "status": "clean",
                        "workflow_state": before,
                    }
                )
                report_path.write_text(
                    json.dumps(reports, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(
                    "Harness stopped at "
                    f"{before['outcome']} after {iteration - 1} workflow run(s)."
                )
                return 0
            if before["outcome"] == "stalled":
                reports.append(
                    {
                        "iteration": iteration,
                        "status": "stalled",
                        "workflow_state": before,
                    }
                )
                break
            selected_task = _select_task(observed_workflow_dir, args.task_id)
            # A full run is the default. The explicit task option remains useful
            # for isolating one failing task while debugging a template.
            task_for_run = selected_task if args.task_id is not None else None
            result = _run_iteration(
                repo_root=run_root,
                workflow_dir=workflow_dir,
                task=task_for_run,
                error_logs=error_logs,
                transcript=transcript_dir / f"iteration-{iteration:02d}.log",
                args=args,
                state_workflow_dir=observed_workflow_dir,
            )
            result["iteration"] = iteration
            result["template_path"] = str(template_path)
            result["error_logs"] = [str(path) for path in error_logs]
            result["workflow_state_before"] = before
            result["workflow_state_after"] = _workflow_state(observed_workflow_dir)
            result["workflow_fingerprint"] = _state_fingerprint(
                result["workflow_state_after"]
            )
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
                f"(outcome={result['workflow_state_after']['outcome']}, "
                f"errors={result['error_count']}, "
                f"corrections={len(result['corrections'])})",
                file=sys.stderr,
            )
            after = result["workflow_state_after"]
            fingerprint = result["workflow_fingerprint"]
            if fingerprint == previous_fingerprint:
                repeated_state_count += 1
            else:
                repeated_state_count = 0
            previous_fingerprint = fingerprint
            if repeated_state_count >= 2:
                result["status"] = "stalled"
                print(
                    "Workflow state repeated without progress three times; stopping.",
                    file=sys.stderr,
                )
            if result["status"] == "clean" and after["outcome"] in {
                "completed",
                "human_handoff",
            }:
                report_path.write_text(
                    json.dumps(reports, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(f"Harness completed cleanly after {iteration} iteration(s).")
                return 0
            if result["status"] in {"timeout", "interrupted"}:
                break
            _reopen_locked_tasks(
                run_root,
                observed_workflow_dir,
                list(after.get("locked_task_ids", [])),
            )
            if not args.repair_command:
                print(
                    "Iteration failed; inspect the transcript and structured errors.",
                    file=sys.stderr,
                )
                break
            repair_task = selected_task
            if repair_task is None:
                repair_task = _select_task(observed_workflow_dir, args.task_id)
            repair_returncode = _run_repair_command(
                args.repair_command,
                repo_root=run_root,
                workflow_dir=observed_workflow_dir,
                task=repair_task,
                template_path=template_path,
                error_log=runner_error_logs[-1],
                result=result,
                iteration=iteration,
                timeout=args.timeout,
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
        report_path.write_text(
            json.dumps(reports, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            f"Harness did not complete cleanly; report: {report_path}", file=sys.stderr
        )
        return 1
    finally:
        if isolated:
            _remove_run_worktree(repo_root, run_root)


if __name__ == "__main__":
    raise SystemExit(main())
