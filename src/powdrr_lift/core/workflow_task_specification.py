from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast


class TaskComplexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CLOSED = "closed"
    SUPERCEDED = "superceded"
    ABANDONED = "abandoned"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class WorkflowTaskValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTaskValidationReport:
    validation_successful: bool
    task_ids: list[str] = field(default_factory=list)
    task_paths: list[str] = field(default_factory=list)
    issues: list[WorkflowTaskValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    task_id: str
    status: TaskStatus
    description: str
    complexity: TaskComplexity
    input_state: Any
    upstream_task_ids: tuple[str, ...] = field(default_factory=tuple)
    dependent_state: tuple[str, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "upstream_task_ids": list(self.upstream_task_ids),
            "dependent_state": list(self.dependent_state),
            "complexity": self.complexity.value,
            "input_state": self.input_state,
            "description": self.description,
        }

    def to_json(self) -> str:
        return workflow_task_to_json(self)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkflowTask:
        return workflow_task_from_data(data)

    @classmethod
    def from_json(cls, json_content: str) -> WorkflowTask:
        return workflow_task_from_json(json_content)

    @classmethod
    def from_file(cls, path: str | Path) -> WorkflowTask:
        return load_workflow_task(path)

    def save(self, path: str | Path) -> Path:
        return save_workflow_task(self, path)


WorkflowTaskDocument = WorkflowTask


def workflow_task_to_json(task: WorkflowTask) -> str:
    return json.dumps(task.to_data(), indent=2, ensure_ascii=False) + "\n"


def workflow_task_from_json(json_content: str) -> WorkflowTask:
    loaded_content = json.loads(json_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Workflow task JSON must decode to an object.")
    return workflow_task_from_data(cast("Mapping[str, Any]", loaded_content))


def workflow_task_from_data(data: Mapping[str, Any]) -> WorkflowTask:
    task_id = _required_string(data, "task_id")
    status = _required_status(data, "status")
    description = _required_string(data, "description")
    complexity = _required_complexity(data, "complexity")
    upstream_task_ids = _required_string_sequence(data, "upstream_task_ids")
    dependent_state = _required_string_sequence(data, "dependent_state")
    if "input_state" not in data:
        raise ValueError("Workflow task entries must include input_state.")

    return WorkflowTask(
        task_id=task_id,
        status=status,
        description=description,
        complexity=complexity,
        input_state=data["input_state"],
        upstream_task_ids=upstream_task_ids,
        dependent_state=dependent_state,
    )


def load_workflow_task(path: str | Path) -> WorkflowTask:
    return workflow_task_from_json(Path(path).read_text(encoding="utf-8"))


def save_workflow_task(task: WorkflowTask, path: str | Path) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(workflow_task_to_json(task), encoding="utf-8")
    return resolved_path


def load_workflow_tasks(directory: str | Path) -> tuple[WorkflowTask, ...]:
    directory_path = Path(directory)
    return tuple(
        load_workflow_task(task_path)
        for task_path in sorted(directory_path.glob("*.json"))
        if task_path.is_file()
    )


def select_ready_workflow_tasks(
    tasks: Sequence[WorkflowTask],
) -> tuple[WorkflowTask, ...]:
    tasks_by_id = {task.task_id: task for task in tasks}
    return tuple(
        task
        for task in tasks
        if task.status is TaskStatus.OPEN
        and all(
            tasks_by_id.get(upstream_task_id) is not None
            and tasks_by_id[upstream_task_id].status is TaskStatus.COMPLETED
            for upstream_task_id in task.upstream_task_ids
        )
    )


def build_workflow_task_validation_report(
    json_content: str,
    *,
    source_path: str | Path | None = None,
) -> WorkflowTaskValidationReport:
    try:
        loaded_content = json.loads(json_content)
    except Exception as exc:  # noqa: BLE001
        return WorkflowTaskValidationReport(
            validation_successful=False,
            task_ids=[],
            task_paths=_task_paths_list(source_path),
            issues=[
                WorkflowTaskValidationIssue(
                    code="invalid_json",
                    message=f"Could not parse workflow task JSON: {exc}",
                    path=_format_path(source_path),
                )
            ],
        )

    if not isinstance(loaded_content, Mapping):
        return WorkflowTaskValidationReport(
            validation_successful=False,
            task_ids=[],
            task_paths=_task_paths_list(source_path),
            issues=[
                WorkflowTaskValidationIssue(
                    code="invalid_root_type",
                    message="Workflow task JSON must decode to an object.",
                    path=_format_path(source_path),
                )
            ],
        )

    raw_task = cast("Mapping[str, Any]", loaded_content)
    issues: list[WorkflowTaskValidationIssue] = []

    _validate_unknown_keys(
        raw_task,
        {
            "task_id",
            "status",
            "upstream_task_ids",
            "dependent_state",
            "complexity",
            "input_state",
            "description",
        },
        issues,
        path=_format_path(source_path) or "",
        subject="workflow task",
    )

    task_id = _optional_string(raw_task.get("task_id"))
    if task_id is None:
        issues.append(
            WorkflowTaskValidationIssue(
                code="missing_task_id",
                message="Workflow task entries must include a non-empty task_id.",
                path=_format_child_path(source_path, "task_id"),
            )
        )

    description = _optional_string(raw_task.get("description"))
    if description is None:
        issues.append(
            WorkflowTaskValidationIssue(
                code="missing_description",
                message="Workflow task entries must include a non-empty description.",
                path=_format_child_path(source_path, "description"),
            )
        )

    status = _optional_string(raw_task.get("status"))
    if status is None:
        issues.append(
            WorkflowTaskValidationIssue(
                code="missing_status",
                message="Workflow task entries must include a non-empty status.",
                path=_format_child_path(source_path, "status"),
            )
        )
    elif status not in {member.value for member in TaskStatus}:
        issues.append(
            WorkflowTaskValidationIssue(
                code="invalid_status",
                message=(
                    "Workflow task status must be one of open, completed, closed, "
                    "superceded, abandoned, or locked."
                ),
                path=_format_child_path(source_path, "status"),
            )
        )

    complexity = _optional_string(raw_task.get("complexity"))
    if complexity is None:
        issues.append(
            WorkflowTaskValidationIssue(
                code="missing_complexity",
                message="Workflow task entries must include a non-empty complexity.",
                path=_format_child_path(source_path, "complexity"),
            )
        )
    elif complexity not in {member.value for member in TaskComplexity}:
        issues.append(
            WorkflowTaskValidationIssue(
                code="invalid_complexity",
                message=(
                    "Workflow task complexity must be one of low, medium, or high."
                ),
                path=_format_child_path(source_path, "complexity"),
            )
        )

    if "input_state" not in raw_task:
        issues.append(
            WorkflowTaskValidationIssue(
                code="missing_input_state",
                message="Workflow task entries must include input_state.",
                path=_format_child_path(source_path, "input_state"),
            )
        )
    elif raw_task.get("input_state") is None:
        issues.append(
            WorkflowTaskValidationIssue(
                code="null_input_state",
                message="Workflow task input_state must not be null.",
                path=_format_child_path(source_path, "input_state"),
            )
        )

    upstream_task_ids = raw_task.get("upstream_task_ids")
    if not isinstance(upstream_task_ids, Sequence) or isinstance(
        upstream_task_ids,
        (str, bytes, bytearray),
    ):
        issues.append(
            WorkflowTaskValidationIssue(
                code="invalid_upstream_task_ids_type",
                message="Workflow task upstream_task_ids must be an array.",
                path=_format_child_path(source_path, "upstream_task_ids"),
            )
        )
    else:
        seen_upstream_ids: set[str] = set()
        for upstream_index, upstream_id in enumerate(upstream_task_ids):
            normalized_upstream_id = _optional_string(upstream_id)
            if normalized_upstream_id is None:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="invalid_upstream_task_id",
                        message=(
                            "Workflow task upstream_task_ids must contain "
                            "non-empty strings."
                        ),
                        path=_format_sequence_path(
                            source_path,
                            "upstream_task_ids",
                            upstream_index,
                        ),
                    )
                )
                continue
            if task_id is not None and normalized_upstream_id == task_id:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="self_dependency",
                        message=(
                            "A workflow task cannot list itself as an upstream task."
                        ),
                        path=_format_sequence_path(
                            source_path,
                            "upstream_task_ids",
                            upstream_index,
                        ),
                    )
                )
            if normalized_upstream_id in seen_upstream_ids:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="duplicate_upstream_task_id",
                        message=(
                            "Workflow task upstream_task_ids must not contain "
                            "duplicates."
                        ),
                        path=_format_sequence_path(
                            source_path,
                            "upstream_task_ids",
                            upstream_index,
                        ),
                    )
                )
                continue
            seen_upstream_ids.add(normalized_upstream_id)

    dependent_state = raw_task.get("dependent_state")
    if not isinstance(dependent_state, Sequence) or isinstance(
        dependent_state,
        (str, bytes, bytearray),
    ):
        issues.append(
            WorkflowTaskValidationIssue(
                code="invalid_dependent_state_type",
                message="Workflow task dependent_state must be an array.",
                path=_format_child_path(source_path, "dependent_state"),
            )
        )
    else:
        seen_dependent_states: set[str] = set()
        for state_index, state_value in enumerate(dependent_state):
            normalized_state = _optional_string(state_value)
            if normalized_state is None:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="invalid_dependent_state_item",
                        message=(
                            "Workflow task dependent_state must contain "
                            "non-empty strings."
                        ),
                        path=_format_sequence_path(
                            source_path,
                            "dependent_state",
                            state_index,
                        ),
                    )
                )
                continue
            if normalized_state in seen_dependent_states:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="duplicate_dependent_state",
                        message=(
                            "Workflow task dependent_state must not contain duplicates."
                        ),
                        path=_format_sequence_path(
                            source_path,
                            "dependent_state",
                            state_index,
                        ),
                    )
                )
                continue
            seen_dependent_states.add(normalized_state)

    if task_id is None or status is None or description is None or complexity is None:
        return WorkflowTaskValidationReport(
            validation_successful=False,
            task_ids=[task_id] if task_id is not None else [],
            task_paths=_task_paths_list(source_path),
            issues=issues,
        )

    return WorkflowTaskValidationReport(
        validation_successful=not issues,
        task_ids=[task_id],
        task_paths=_task_paths_list(source_path),
        issues=issues,
    )


def build_workflow_task_directory_validation_report(
    directory: str | Path,
) -> WorkflowTaskValidationReport:
    directory_path = Path(directory)
    if not directory_path.exists():
        return WorkflowTaskValidationReport(
            validation_successful=False,
            issues=[
                WorkflowTaskValidationIssue(
                    code="missing_directory",
                    message=f"Workflow task directory does not exist: {directory_path}",
                    path=str(directory_path),
                )
            ],
        )
    if not directory_path.is_dir():
        return WorkflowTaskValidationReport(
            validation_successful=False,
            issues=[
                WorkflowTaskValidationIssue(
                    code="not_a_directory",
                    message=f"Workflow task path is not a directory: {directory_path}",
                    path=str(directory_path),
                )
            ],
        )

    issues: list[WorkflowTaskValidationIssue] = []
    task_ids: list[str] = []
    task_paths: list[str] = []
    tasks_by_id: dict[str, WorkflowTask] = {}
    task_paths_by_id: dict[str, Path] = {}
    upstream_references: list[tuple[Path, WorkflowTask]] = []

    for task_path in sorted(directory_path.glob("*.json")):
        if not task_path.is_file():
            continue
        task_paths.append(str(task_path))
        raw_content = task_path.read_text(encoding="utf-8")
        file_report = build_workflow_task_validation_report(
            raw_content,
            source_path=task_path,
        )
        issues.extend(file_report.issues)
        if not file_report.validation_successful or not file_report.task_ids:
            continue

        task = workflow_task_from_json(raw_content)
        task_id = task.task_id
        upstream_references.append((task_path, task))
        if task_id in tasks_by_id:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="duplicate_task_id",
                    message=(
                        f"Workflow task id {task_id!r} appears in both "
                        f"{task_paths_by_id[task_id]} and {task_path}."
                    ),
                    path=str(task_path),
                )
            )
        else:
            tasks_by_id[task_id] = task
            task_paths_by_id[task_id] = task_path
            task_ids.append(task_id)

    for task_path, task in upstream_references:
        for upstream_index, upstream_task_id in enumerate(task.upstream_task_ids):
            if upstream_task_id not in tasks_by_id:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="missing_upstream_task",
                        message=(
                            f"Workflow task {task.task_id!r} references unknown "
                            f"upstream task {upstream_task_id!r}."
                        ),
                        path=_format_sequence_path(
                            task_path,
                            "upstream_task_ids",
                            upstream_index,
                        ),
                    )
                )

    downstream_task_ids: dict[str, list[str]] = {task_id: [] for task_id in tasks_by_id}
    for _downstream_task_path, downstream_task in upstream_references:
        for upstream_task_id in downstream_task.upstream_task_ids:
            if upstream_task_id in downstream_task_ids:
                downstream_task_ids[upstream_task_id].append(downstream_task.task_id)

    for task_path, task in upstream_references:
        if task.status is not TaskStatus.OPEN:
            continue
        for downstream_task_id in downstream_task_ids.get(task.task_id, []):
            downstream_task_obj = tasks_by_id.get(downstream_task_id)
            if (
                downstream_task_obj is None
                or downstream_task_obj.status is not TaskStatus.CLOSED
            ):
                continue
            issues.append(
                WorkflowTaskValidationIssue(
                    code="open_task_has_closed_downstream_task",
                    message=(
                        f"Workflow task {task.task_id!r} is open while downstream "
                        f"task {downstream_task_obj.task_id!r} is closed."
                    ),
                    path=_format_source_path(task_path),
                )
            )

    return WorkflowTaskValidationReport(
        validation_successful=not issues,
        task_ids=task_ids,
        task_paths=task_paths,
        issues=issues,
    )


def validate_workflow_task_json(json_content: str) -> str:
    return (
        json.dumps(
            _report_to_data(build_workflow_task_validation_report(json_content)),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def validate_workflow_task_json_file(path: str | Path) -> str:
    return validate_workflow_task_json(Path(path).read_text(encoding="utf-8"))


def validate_workflow_task_directory(directory: str | Path) -> str:
    return (
        json.dumps(
            _report_to_data(build_workflow_task_directory_validation_report(directory)),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


workflow_task_document_to_json = workflow_task_to_json
workflow_task_document_from_json = workflow_task_from_json
workflow_task_document_from_data = workflow_task_from_data
load_workflow_task_document = load_workflow_task
save_workflow_task_document = save_workflow_task
load_workflow_task_documents = load_workflow_tasks
validate_workflow_task_directory_json = validate_workflow_task_directory


def _report_to_data(report: WorkflowTaskValidationReport) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "task_ids": report.task_ids,
        "task_paths": report.task_paths,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in report.issues
        ],
    }


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = _optional_string(data.get(key))
    if value is None:
        raise ValueError(f"Workflow task entries must include a non-empty {key}.")
    return value


def _required_complexity(data: Mapping[str, Any], key: str) -> TaskComplexity:
    raw_complexity = _required_string(data, key)
    try:
        return TaskComplexity(raw_complexity)
    except ValueError as exc:
        raise ValueError(
            "Workflow task complexity must be one of low, medium, or high."
        ) from exc


def _required_status(data: Mapping[str, Any], key: str) -> TaskStatus:
    raw_status = _required_string(data, key)
    try:
        return TaskStatus(raw_status)
    except ValueError as exc:
        raise ValueError(
            "Workflow task status must be one of open, completed, superceded, "
            "abandoned, or locked."
        ) from exc


def _required_string_sequence(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw_value = data.get(key)
    if not isinstance(raw_value, Sequence) or isinstance(
        raw_value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"Workflow task entries must include an array for {key}.")
    values: list[str] = []
    for item in raw_value:
        normalized = _optional_string(item)
        if normalized is None:
            raise ValueError(
                f"Workflow task {key} entries must contain non-empty strings."
            )
        values.append(normalized)
    return tuple(values)


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _validate_unknown_keys(
    data: Mapping[str, Any],
    allowed_keys: set[str],
    issues: list[WorkflowTaskValidationIssue],
    *,
    path: str,
    subject: str,
) -> None:
    for key in data:
        if key in allowed_keys:
            continue
        issues.append(
            WorkflowTaskValidationIssue(
                code="unknown_key",
                message=f"{subject.title()} contains unknown field {key!r}.",
                path=f"{path}.{key}" if path else key,
            )
        )


def _format_source_path(source_path: str | Path | None) -> str | None:
    if source_path is None:
        return None
    return str(Path(source_path))


def _task_paths_list(source_path: str | Path | None) -> list[str]:
    formatted_source_path = _format_source_path(source_path)
    return [formatted_source_path] if formatted_source_path is not None else []


def _format_path(source_path: str | Path | None) -> str | None:
    if source_path is None:
        return None
    return str(Path(source_path))


def _format_child_path(source_path: str | Path | None, key: str) -> str | None:
    formatted_source_path = _format_path(source_path)
    if formatted_source_path is None:
        return key
    return f"{formatted_source_path}.{key}"


def _format_sequence_path(
    source_path: str | Path | None,
    key: str,
    index: int,
) -> str | None:
    formatted_source_path = _format_path(source_path)
    if formatted_source_path is None:
        return f"{key}[{index}]"
    return f"{formatted_source_path}.{key}[{index}]"
