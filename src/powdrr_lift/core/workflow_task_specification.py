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


@dataclass(frozen=True, slots=True)
class WorkflowTaskValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTaskValidationReport:
    validation_successful: bool
    task_ids: list[str] = field(default_factory=list)
    issues: list[WorkflowTaskValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    task_id: str
    description: str
    complexity: TaskComplexity
    input_state: Any
    upstream_task_ids: tuple[str, ...] = field(default_factory=tuple)
    dependent_state: tuple[str, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "upstream_task_ids": list(self.upstream_task_ids),
            "dependent_state": list(self.dependent_state),
            "complexity": self.complexity.value,
            "input_state": self.input_state,
            "description": self.description,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkflowTask:
        task_id = _required_string(data, "task_id")
        description = _required_string(data, "description")
        complexity = _required_complexity(data, "complexity")
        upstream_task_ids = _required_string_sequence(data, "upstream_task_ids")
        dependent_state = _required_string_sequence(data, "dependent_state")
        if "input_state" not in data:
            raise ValueError("Workflow task entries must include input_state.")

        return cls(
            task_id=task_id,
            description=description,
            complexity=complexity,
            input_state=data["input_state"],
            upstream_task_ids=upstream_task_ids,
            dependent_state=dependent_state,
        )


@dataclass(frozen=True, slots=True)
class WorkflowTaskDocument:
    tasks: tuple[WorkflowTask, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        return {"tasks": [task.to_data() for task in self.tasks]}

    def to_json(self) -> str:
        return workflow_task_document_to_json(self)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkflowTaskDocument:
        return workflow_task_document_from_data(data)

    @classmethod
    def from_json(cls, json_content: str) -> WorkflowTaskDocument:
        return workflow_task_document_from_json(json_content)


def workflow_task_document_to_json(document: WorkflowTaskDocument) -> str:
    return json.dumps(document.to_data(), indent=2, ensure_ascii=False) + "\n"


def workflow_task_document_from_json(json_content: str) -> WorkflowTaskDocument:
    loaded_content = json.loads(json_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Workflow task JSON must decode to a mapping.")
    return workflow_task_document_from_data(cast("Mapping[str, Any]", loaded_content))


def workflow_task_document_from_data(data: Mapping[str, Any]) -> WorkflowTaskDocument:
    tasks = data.get("tasks")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes, bytearray)):
        raise ValueError("Workflow task documents must include a tasks array.")

    return WorkflowTaskDocument(
        tasks=tuple(
            WorkflowTask.from_data(_require_mapping(task_data, path="tasks[]"))
            for task_data in tasks
        ),
    )


def load_workflow_task_document(path: str | Path) -> WorkflowTaskDocument:
    return workflow_task_document_from_json(Path(path).read_text(encoding="utf-8"))


def save_workflow_task_document(
    document: WorkflowTaskDocument,
    path: str | Path,
) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(
        workflow_task_document_to_json(document),
        encoding="utf-8",
    )
    return resolved_path


def build_workflow_task_validation_report(
    json_content: str,
) -> WorkflowTaskValidationReport:
    try:
        loaded_content = json.loads(json_content)
    except Exception as exc:  # noqa: BLE001
        return WorkflowTaskValidationReport(
            validation_successful=False,
            task_ids=[],
            issues=[
                WorkflowTaskValidationIssue(
                    code="invalid_json",
                    message=f"Could not parse workflow task JSON: {exc}",
                )
            ],
        )

    if not isinstance(loaded_content, Mapping):
        return WorkflowTaskValidationReport(
            validation_successful=False,
            task_ids=[],
            issues=[
                WorkflowTaskValidationIssue(
                    code="invalid_root_type",
                    message="Workflow task JSON must decode to an object.",
                )
            ],
        )

    raw_document = cast("Mapping[str, Any]", loaded_content)
    issues: list[WorkflowTaskValidationIssue] = []

    allowed_top_level_keys = {"tasks"}
    _validate_unknown_keys(
        raw_document,
        allowed_top_level_keys,
        issues,
        path="",
        subject="workflow task document",
    )

    raw_tasks = raw_document.get("tasks")
    if not isinstance(raw_tasks, Sequence) or isinstance(
        raw_tasks,
        (str, bytes, bytearray),
    ):
        issues.append(
            WorkflowTaskValidationIssue(
                code="invalid_tasks_type",
                message="Workflow task documents must include a tasks array.",
                path="tasks",
            )
        )
        return WorkflowTaskValidationReport(
            validation_successful=False,
            task_ids=[],
            issues=issues,
        )

    task_ids: list[str] = []
    task_ids_by_index: list[str | None] = []
    seen_task_ids: set[str] = set()
    tasks_by_id: dict[str, int] = {}

    for index, raw_task in enumerate(raw_tasks):
        task_path = f"tasks[{index}]"
        if not isinstance(raw_task, Mapping):
            issues.append(
                WorkflowTaskValidationIssue(
                    code="invalid_task_type",
                    message="Workflow task entries must be objects.",
                    path=task_path,
                )
            )
            task_ids_by_index.append(None)
            continue

        raw_task_mapping = cast("Mapping[str, Any]", raw_task)
        _validate_unknown_keys(
            raw_task_mapping,
            {
                "task_id",
                "upstream_task_ids",
                "dependent_state",
                "complexity",
                "input_state",
                "description",
            },
            issues,
            path=task_path,
            subject="workflow task",
        )

        task_id = _optional_string(raw_task_mapping.get("task_id"))
        if task_id is None:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="missing_task_id",
                    message="Workflow task entries must include a non-empty task_id.",
                    path=f"{task_path}.task_id",
                )
            )
            task_ids_by_index.append(None)
        else:
            task_ids_by_index.append(task_id)
            task_ids.append(task_id)
            if task_id in seen_task_ids:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="duplicate_task_id",
                        message=f"Workflow task id {task_id!r} appears more than once.",
                        path=f"{task_path}.task_id",
                    )
                )
            else:
                seen_task_ids.add(task_id)
                tasks_by_id[task_id] = index

        description = _optional_string(raw_task_mapping.get("description"))
        if description is None:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="missing_description",
                    message=(
                        "Workflow task entries must include a non-empty description."
                    ),
                    path=f"{task_path}.description",
                )
            )

        complexity = _optional_string(raw_task_mapping.get("complexity"))
        if complexity is None:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="missing_complexity",
                    message=(
                        "Workflow task entries must include a non-empty complexity."
                    ),
                    path=f"{task_path}.complexity",
                )
            )
        elif complexity not in {member.value for member in TaskComplexity}:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="invalid_complexity",
                    message=(
                        "Workflow task complexity must be one of low, medium, or high."
                    ),
                    path=f"{task_path}.complexity",
                )
            )

        if "input_state" not in raw_task_mapping:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="missing_input_state",
                    message="Workflow task entries must include input_state.",
                    path=f"{task_path}.input_state",
                )
            )
        elif raw_task_mapping.get("input_state") is None:
            issues.append(
                WorkflowTaskValidationIssue(
                    code="null_input_state",
                    message="Workflow task input_state must not be null.",
                    path=f"{task_path}.input_state",
                )
            )

        upstream_task_ids = raw_task_mapping.get("upstream_task_ids")
        if not isinstance(upstream_task_ids, Sequence) or isinstance(
            upstream_task_ids,
            (str, bytes, bytearray),
        ):
            issues.append(
                WorkflowTaskValidationIssue(
                    code="invalid_upstream_task_ids_type",
                    message="Workflow task upstream_task_ids must be an array.",
                    path=f"{task_path}.upstream_task_ids",
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
                            path=f"{task_path}.upstream_task_ids[{upstream_index}]",
                        )
                    )
                    continue
                if normalized_upstream_id == task_id:
                    issues.append(
                        WorkflowTaskValidationIssue(
                            code="self_dependency",
                            message=(
                                "A workflow task cannot list itself as an upstream "
                                "task."
                            ),
                            path=f"{task_path}.upstream_task_ids[{upstream_index}]",
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
                            path=f"{task_path}.upstream_task_ids[{upstream_index}]",
                        )
                    )
                    continue
                seen_upstream_ids.add(normalized_upstream_id)

        dependent_state = raw_task_mapping.get("dependent_state")
        if not isinstance(dependent_state, Sequence) or isinstance(
            dependent_state,
            (str, bytes, bytearray),
        ):
            issues.append(
                WorkflowTaskValidationIssue(
                    code="invalid_dependent_state_type",
                    message="Workflow task dependent_state must be an array.",
                    path=f"{task_path}.dependent_state",
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
                            path=f"{task_path}.dependent_state[{state_index}]",
                        )
                    )
                    continue
                if normalized_state in seen_dependent_states:
                    issues.append(
                        WorkflowTaskValidationIssue(
                            code="duplicate_dependent_state",
                            message=(
                                "Workflow task dependent_state must not contain "
                                "duplicates."
                            ),
                            path=f"{task_path}.dependent_state[{state_index}]",
                        )
                    )
                    continue
                seen_dependent_states.add(normalized_state)

    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, Mapping):
            continue
        raw_task_mapping = cast("Mapping[str, Any]", raw_task)
        task_id = task_ids_by_index[index]
        if task_id is None:
            continue
        upstream_task_ids = raw_task_mapping.get("upstream_task_ids")
        if not isinstance(upstream_task_ids, Sequence) or isinstance(
            upstream_task_ids,
            (str, bytes, bytearray),
        ):
            continue
        for upstream_index, upstream_id in enumerate(upstream_task_ids):
            normalized_upstream_id = _optional_string(upstream_id)
            if normalized_upstream_id is None:
                continue
            if normalized_upstream_id not in tasks_by_id:
                issues.append(
                    WorkflowTaskValidationIssue(
                        code="missing_upstream_task",
                        message=(
                            f"Workflow task {task_id!r} references unknown upstream "
                            f"task {normalized_upstream_id!r}."
                        ),
                        path=f"tasks[{index}].upstream_task_ids[{upstream_index}]",
                    )
                )

    return WorkflowTaskValidationReport(
        validation_successful=not issues,
        task_ids=task_ids,
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


def _report_to_data(report: WorkflowTaskValidationReport) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "task_ids": report.task_ids,
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


def _require_mapping(value: object, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Expected a mapping at {path}.")
    return cast("Mapping[str, Any]", value)


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
