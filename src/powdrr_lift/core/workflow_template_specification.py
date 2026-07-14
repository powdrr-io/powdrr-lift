from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from powdrr_lift.core.workflow_task_specification import TaskComplexity


@dataclass(frozen=True, slots=True)
class WorkflowTemplateValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTemplateValidationReport:
    validation_successful: bool
    task_template_count: int = 0
    issues: list[WorkflowTemplateValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WorkflowTaskTemplateGeneration:
    for_each: str
    downstream_task_template_indexes: tuple[int, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        return {
            "for_each": self.for_each,
            "downstream_task_template_indexes": list(
                self.downstream_task_template_indexes
            ),
        }


@dataclass(frozen=True, slots=True)
class WorkflowTaskTemplate:
    description: str
    complexity: TaskComplexity
    input_state: Any
    upstream_task_template_indexes: tuple[int, ...] = field(default_factory=tuple)
    dependent_state: tuple[str, ...] = field(default_factory=tuple)
    generation: WorkflowTaskTemplateGeneration | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": self.description,
            "complexity": self.complexity.value,
            "input_state": self.input_state,
            "upstream_task_template_indexes": list(self.upstream_task_template_indexes),
            "dependent_state": list(self.dependent_state),
        }
        if self.generation is not None:
            data["generation"] = self.generation.to_data()
        return data


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    when_to_use: tuple[str, ...]
    how_to_fill_this_out: tuple[str, ...]
    task_templates: tuple[WorkflowTaskTemplate, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "when_to_use": list(self.when_to_use),
            "how_to_fill_this_out": list(self.how_to_fill_this_out),
            "task_templates": [
                task_template.to_data() for task_template in self.task_templates
            ],
        }

    def to_json(self) -> str:
        return workflow_template_to_json(self)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> WorkflowTemplate:
        return workflow_template_from_data(data)

    @classmethod
    def from_json(cls, json_content: str) -> WorkflowTemplate:
        return workflow_template_from_json(json_content)

    @classmethod
    def from_file(cls, path: str | Path) -> WorkflowTemplate:
        return load_workflow_template(path)

    def save(self, path: str | Path) -> Path:
        return save_workflow_template(self, path)


WorkflowTemplateDocument = WorkflowTemplate


def workflow_template_to_json(template: WorkflowTemplate) -> str:
    return json.dumps(template.to_data(), indent=2, ensure_ascii=False) + "\n"


def workflow_template_from_json(json_content: str) -> WorkflowTemplate:
    loaded_content = json.loads(json_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Workflow template JSON must decode to an object.")
    return workflow_template_from_data(cast("Mapping[str, Any]", loaded_content))


def workflow_template_from_data(data: Mapping[str, Any]) -> WorkflowTemplate:
    when_to_use = _required_string_sequence(data, "when_to_use")
    how_to_fill_this_out = _required_string_sequence(data, "how_to_fill_this_out")
    task_templates = _parse_task_templates(data.get("task_templates"))
    return WorkflowTemplate(
        when_to_use=when_to_use,
        how_to_fill_this_out=how_to_fill_this_out,
        task_templates=task_templates,
    )


def load_workflow_template(path: str | Path) -> WorkflowTemplate:
    return workflow_template_from_json(Path(path).read_text(encoding="utf-8"))


def save_workflow_template(template: WorkflowTemplate, path: str | Path) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(workflow_template_to_json(template), encoding="utf-8")
    return resolved_path


def create_specify_feature_workflow_template() -> WorkflowTemplate:
    return WorkflowTemplate(
        when_to_use=(
            "When a feature needs to move from an idea to implementation-ready work.",
            "When the work should gather requirements, supporting context, intent, and "
            "proposed PRs in a single workflow.",
        ),
        how_to_fill_this_out=(
            "Fill in the steps in order and keep each step focused on the "
            "named output.",
            "Use the generation block to fan out follow-up tasks when one step needs "
            "to produce several related tasks.",
            "Keep dependencies explicit so later steps only rely on completed upstream "
            "templates.",
        ),
        task_templates=(
            WorkflowTaskTemplate(
                description="Gather the requirements and approach.",
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "feature": None,
                    "requirements": [],
                    "approach": [],
                },
                upstream_task_template_indexes=(),
                dependent_state=(
                    "requirements-captured",
                    "approach-defined",
                ),
            ),
            WorkflowTaskTemplate(
                description="Gather the entities, invariants, and guidance.",
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "entities": [],
                    "invariants": [],
                    "guidance": [],
                },
                upstream_task_template_indexes=(0,),
                dependent_state=(
                    "entities-captured",
                    "invariants-captured",
                    "guidance-captured",
                ),
            ),
            WorkflowTaskTemplate(
                description="Gather the features and decisions.",
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "features": [],
                    "decisions": [],
                },
                upstream_task_template_indexes=(1,),
                dependent_state=(
                    "features-captured",
                    "decisions-captured",
                ),
                generation=WorkflowTaskTemplateGeneration(
                    for_each="each feature or decision that needs dedicated follow-up",
                    downstream_task_template_indexes=(3, 4),
                ),
            ),
            WorkflowTaskTemplate(
                description="Gather the intent and reasoning.",
                complexity=TaskComplexity.MEDIUM,
                input_state={
                    "intent": None,
                    "reasoning": None,
                },
                upstream_task_template_indexes=(2,),
                dependent_state=(
                    "intent-captured",
                    "reasoning-captured",
                ),
            ),
            WorkflowTaskTemplate(
                description="Specify proposed PRs.",
                complexity=TaskComplexity.HIGH,
                input_state={
                    "proposed_prs": [],
                },
                upstream_task_template_indexes=(3,),
                dependent_state=("proposed-prs-specified",),
            ),
        ),
    )


def build_workflow_template_validation_report(
    json_content: str,
    *,
    source_path: str | Path | None = None,
) -> WorkflowTemplateValidationReport:
    try:
        loaded_content = json.loads(json_content)
    except Exception as exc:  # noqa: BLE001
        return WorkflowTemplateValidationReport(
            validation_successful=False,
            issues=[
                WorkflowTemplateValidationIssue(
                    code="invalid_json",
                    message=f"Could not parse workflow template JSON: {exc}",
                    path=_path_prefix(source_path),
                )
            ],
        )

    if not isinstance(loaded_content, Mapping):
        return WorkflowTemplateValidationReport(
            validation_successful=False,
            issues=[
                WorkflowTemplateValidationIssue(
                    code="invalid_root_type",
                    message="Workflow template JSON must decode to an object.",
                    path=_path_prefix(source_path),
                )
            ],
        )

    raw_template = cast("Mapping[str, Any]", loaded_content)
    issues: list[WorkflowTemplateValidationIssue] = []

    _validate_unknown_keys(
        raw_template,
        {"when_to_use", "how_to_fill_this_out", "task_templates"},
        issues,
        path=_path_prefix(source_path) or "",
        subject="workflow template",
    )

    for key, code in (
        ("when_to_use", "invalid_when_to_use"),
        ("how_to_fill_this_out", "invalid_how_to_fill_this_out"),
    ):
        try:
            values = _required_string_sequence(raw_template, key)
        except ValueError as exc:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code=code,
                    message=str(exc),
                    path=_child_path(source_path, key),
                )
            )
            continue
        if len(values) == 0:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code=f"missing_{key}",
                    message=f"Workflow template must include at least one {key}.",
                    path=_child_path(source_path, key),
                )
            )
    raw_task_templates = raw_template.get("task_templates")
    task_template_reports: list[tuple[int, Mapping[str, Any] | None]] = []

    if not isinstance(raw_task_templates, Sequence) or isinstance(
        raw_task_templates,
        (str, bytes, bytearray),
    ):
        issues.append(
            WorkflowTemplateValidationIssue(
                code="invalid_task_templates_type",
                message="Workflow template task_templates must be an array.",
                path=_child_path(source_path, "task_templates"),
            )
        )
        raw_task_templates = []
    if len(raw_task_templates) == 0:
        issues.append(
            WorkflowTemplateValidationIssue(
                code="missing_task_templates",
                message="Workflow template must include at least one task template.",
                path=_child_path(source_path, "task_templates"),
            )
        )

    for index, raw_task_template in enumerate(raw_task_templates):
        task_template_path = _sequence_path(source_path, "task_templates", index)
        if not isinstance(raw_task_template, Mapping):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="invalid_task_template_type",
                    message="Workflow template task templates must be objects.",
                    path=task_template_path,
                )
            )
            task_template_reports.append((index, None))
            continue

        raw_task_template_mapping = cast("Mapping[str, Any]", raw_task_template)
        task_template_reports.append((index, raw_task_template_mapping))
        _validate_unknown_keys(
            raw_task_template_mapping,
            {
                "description",
                "complexity",
                "input_state",
                "upstream_task_template_indexes",
                "dependent_state",
                "generation",
            },
            issues,
            path=task_template_path or "",
            subject="workflow task template",
        )

        description = _optional_string(raw_task_template_mapping.get("description"))
        if description is None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_description",
                    message=(
                        "Workflow task templates must include a non-empty description."
                    ),
                    path=_child_path(task_template_path, "description"),
                )
            )

        complexity = _optional_string(raw_task_template_mapping.get("complexity"))
        if complexity is None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_complexity",
                    message=(
                        "Workflow task templates must include a non-empty complexity."
                    ),
                    path=_child_path(task_template_path, "complexity"),
                )
            )
        elif complexity not in {member.value for member in TaskComplexity}:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="invalid_complexity",
                    message=(
                        "Workflow task template complexity must be one of low, "
                        "medium, or high."
                    ),
                    path=_child_path(task_template_path, "complexity"),
                )
            )

        if "input_state" not in raw_task_template_mapping:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_input_state",
                    message="Workflow task templates must include input_state.",
                    path=_child_path(task_template_path, "input_state"),
                )
            )

        upstream_indexes = _optional_int_sequence(
            raw_task_template_mapping.get("upstream_task_template_indexes"),
            path=_child_path(task_template_path, "upstream_task_template_indexes"),
            issue_code="invalid_upstream_task_template_index",
            issue_message=(
                "Workflow task template upstream_task_template_indexes must be "
                "an array of non-negative integers."
            ),
            issues=issues,
        )
        if upstream_indexes is not None:
            _validate_unique_indexes(
                upstream_indexes,
                issues,
                path=_child_path(task_template_path, "upstream_task_template_indexes"),
                duplicate_code="duplicate_upstream_task_template_index",
                duplicate_message=(
                    "Workflow task template upstream_task_template_indexes must "
                    "not contain duplicates."
                ),
            )

        dependent_state = raw_task_template_mapping.get("dependent_state")
        if not isinstance(dependent_state, Sequence) or isinstance(
            dependent_state,
            (str, bytes, bytearray),
        ):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="invalid_dependent_state_type",
                    message=(
                        "Workflow task template dependent_state must be an array."
                    ),
                    path=_child_path(task_template_path, "dependent_state"),
                )
            )
        else:
            for state_index, state_value in enumerate(dependent_state):
                if _optional_string(state_value) is None:
                    issues.append(
                        WorkflowTemplateValidationIssue(
                            code="invalid_dependent_state_item",
                            message=(
                                "Workflow task template dependent_state must "
                                "contain non-empty strings."
                            ),
                            path=_sequence_path(
                                task_template_path,
                                "dependent_state",
                                state_index,
                            ),
                        )
                    )

        generation = raw_task_template_mapping.get("generation")
        if generation is not None:
            if not isinstance(generation, Mapping):
                issues.append(
                    WorkflowTemplateValidationIssue(
                        code="invalid_generation_type",
                        message=(
                            "Workflow task template generation must be an object."
                        ),
                        path=_child_path(task_template_path, "generation"),
                    )
                )
            else:
                generation_mapping = cast("Mapping[str, Any]", generation)
                _validate_unknown_keys(
                    generation_mapping,
                    {
                        "for_each",
                        "downstream_task_template_indexes",
                    },
                    issues,
                    path=_child_path(task_template_path, "generation") or "",
                    subject="workflow task template generation",
                )
                for_each = _optional_string(generation_mapping.get("for_each"))
                if for_each is None:
                    issues.append(
                        WorkflowTemplateValidationIssue(
                            code="missing_for_each",
                            message=(
                                "Workflow task template generation must include a "
                                "non-empty for_each description."
                            ),
                            path=_child_path(
                                _child_path(task_template_path, "generation"),
                                "for_each",
                            ),
                        )
                    )
                downstream_indexes = _optional_int_sequence(
                    generation_mapping.get("downstream_task_template_indexes"),
                    path=_child_path(
                        _child_path(task_template_path, "generation"),
                        "downstream_task_template_indexes",
                    ),
                    issue_code="invalid_downstream_task_template_index",
                    issue_message=(
                        "Workflow task template generation downstream indexes "
                        "must be an array of non-negative integers."
                    ),
                    issues=issues,
                )
                if downstream_indexes is not None:
                    _validate_unique_indexes(
                        downstream_indexes,
                        issues,
                        path=_child_path(
                            _child_path(task_template_path, "generation"),
                            "downstream_task_template_indexes",
                        ),
                        duplicate_code="duplicate_downstream_task_template_index",
                        duplicate_message=(
                            "Workflow task template generation downstream indexes "
                            "must not contain duplicates."
                        ),
                    )

    task_template_count = len(task_template_reports)
    for index, raw_task_template in task_template_reports:
        if raw_task_template is None:
            continue
        upstream_indexes = _normalize_int_sequence(
            raw_task_template.get("upstream_task_template_indexes")
        )
        if upstream_indexes is not None:
            for upstream_index in upstream_indexes:
                if upstream_index < 0 or upstream_index >= task_template_count:
                    issues.append(
                        WorkflowTemplateValidationIssue(
                            code="missing_upstream_task_template",
                            message=(
                                "Workflow task template references an unknown "
                                "upstream task template index."
                            ),
                            path=_sequence_path(
                                source_path,
                                "task_templates",
                                index,
                            ),
                        )
                    )
                elif upstream_index == index:
                    issues.append(
                        WorkflowTemplateValidationIssue(
                            code="self_dependency",
                            message=(
                                "A workflow task template cannot depend on itself."
                            ),
                            path=_sequence_path(
                                source_path,
                                "task_templates",
                                index,
                            ),
                        )
                    )

        generation = raw_task_template.get("generation")
        if not isinstance(generation, Mapping):
            continue
        downstream_indexes = _normalize_int_sequence(
            generation.get("downstream_task_template_indexes")
        )
        if downstream_indexes is None:
            continue
        for downstream_index in downstream_indexes:
            if downstream_index < 0 or downstream_index >= task_template_count:
                issues.append(
                    WorkflowTemplateValidationIssue(
                        code="missing_downstream_task_template",
                        message=(
                            "Workflow task template generation references an "
                            "unknown downstream task template index."
                        ),
                        path=_sequence_path(
                            source_path,
                            "task_templates",
                            index,
                        ),
                    )
                )
            elif downstream_index == index:
                issues.append(
                    WorkflowTemplateValidationIssue(
                        code="self_generation_dependency",
                        message=(
                            "Workflow task template generation cannot point to "
                            "itself as downstream."
                        ),
                        path=_sequence_path(
                            source_path,
                            "task_templates",
                            index,
                        ),
                    )
                )

    return WorkflowTemplateValidationReport(
        validation_successful=not issues,
        task_template_count=task_template_count,
        issues=issues,
    )


def validate_workflow_template_json(json_content: str) -> str:
    return (
        json.dumps(
            _report_to_data(build_workflow_template_validation_report(json_content)),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def validate_workflow_template_json_file(path: str | Path) -> str:
    return validate_workflow_template_json(Path(path).read_text(encoding="utf-8"))


def _parse_task_templates(
    raw_task_templates: object | None,
) -> tuple[WorkflowTaskTemplate, ...]:
    if not isinstance(raw_task_templates, Sequence) or isinstance(
        raw_task_templates,
        (str, bytes, bytearray),
    ):
        raise ValueError("Workflow template must include a task_templates array.")
    return tuple(
        _parse_task_template(task_template_data)
        for task_template_data in raw_task_templates
    )


def _parse_task_template(raw_task_template: object) -> WorkflowTaskTemplate:
    if not isinstance(raw_task_template, Mapping):
        raise ValueError("Workflow template task templates must be objects.")
    data = cast("Mapping[str, Any]", raw_task_template)
    description = _required_string(data, "description")
    complexity = _required_complexity(data, "complexity")
    input_state = data.get("input_state", _MISSING)
    if input_state is _MISSING:
        raise ValueError("Workflow task templates must include input_state.")
    upstream_task_template_indexes = _required_int_sequence(
        data,
        "upstream_task_template_indexes",
    )
    dependent_state = _required_string_sequence(data, "dependent_state")
    generation = data.get("generation")
    parsed_generation = None
    if generation is not None:
        parsed_generation = _parse_generation(generation)
    return WorkflowTaskTemplate(
        description=description,
        complexity=complexity,
        input_state=input_state,
        upstream_task_template_indexes=upstream_task_template_indexes,
        dependent_state=dependent_state,
        generation=parsed_generation,
    )


def _parse_generation(
    raw_generation: object,
) -> WorkflowTaskTemplateGeneration:
    if not isinstance(raw_generation, Mapping):
        raise ValueError("Workflow task template generation must be an object.")
    data = cast("Mapping[str, Any]", raw_generation)
    for_each = _required_string(data, "for_each")
    downstream_task_template_indexes = _required_int_sequence(
        data,
        "downstream_task_template_indexes",
    )
    return WorkflowTaskTemplateGeneration(
        for_each=for_each,
        downstream_task_template_indexes=downstream_task_template_indexes,
    )


def _report_to_data(
    report: WorkflowTemplateValidationReport,
) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "task_template_count": report.task_template_count,
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
        raise ValueError(f"Workflow template entries must include a non-empty {key}.")
    return value


def _required_complexity(data: Mapping[str, Any], key: str) -> TaskComplexity:
    raw_complexity = _required_string(data, key)
    try:
        return TaskComplexity(raw_complexity)
    except ValueError as exc:
        raise ValueError(
            "Workflow task template complexity must be one of low, medium, or high."
        ) from exc


def _required_string_sequence(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw_value = data.get(key)
    if not isinstance(raw_value, Sequence) or isinstance(
        raw_value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"Workflow template entries must include an array for {key}.")
    values: list[str] = []
    for item in raw_value:
        normalized = _optional_string(item)
        if normalized is None:
            raise ValueError(
                f"Workflow template {key} entries must contain non-empty strings."
            )
        values.append(normalized)
    return tuple(values)


def _required_int_sequence(data: Mapping[str, Any], key: str) -> tuple[int, ...]:
    raw_value = data.get(key)
    if not isinstance(raw_value, Sequence) or isinstance(
        raw_value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"Workflow template entries must include an array for {key}.")
    values: list[int] = []
    for item in raw_value:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(
                f"Workflow template {key} entries must contain non-negative integers."
            )
        values.append(item)
    return tuple(values)


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _optional_int_sequence(
    raw_value: object | None,
    *,
    path: str | None,
    issue_code: str,
    issue_message: str,
    issues: list[WorkflowTemplateValidationIssue],
) -> tuple[int, ...] | None:
    if raw_value is None:
        return tuple()
    if not isinstance(raw_value, Sequence) or isinstance(
        raw_value,
        (str, bytes, bytearray),
    ):
        issues.append(
            WorkflowTemplateValidationIssue(
                code=issue_code,
                message=issue_message,
                path=path,
            )
        )
        return None
    values: list[int] = []
    for item_index, item in enumerate(raw_value):
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code=issue_code,
                    message=issue_message,
                    path=_sequence_path_from_path(path, item_index),
                )
            )
            return None
        values.append(item)
    return tuple(values)


def _normalize_int_sequence(raw_value: object | None) -> tuple[int, ...] | None:
    if raw_value is None:
        return tuple()
    if not isinstance(raw_value, Sequence) or isinstance(
        raw_value,
        (str, bytes, bytearray),
    ):
        return None
    values: list[int] = []
    for item in raw_value:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            return None
        values.append(item)
    return tuple(values)


def _validate_unique_indexes(
    indexes: Sequence[int],
    issues: list[WorkflowTemplateValidationIssue],
    *,
    path: str | None,
    duplicate_code: str,
    duplicate_message: str,
) -> None:
    seen: set[int] = set()
    for index, value in enumerate(indexes):
        if value in seen:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code=duplicate_code,
                    message=duplicate_message,
                    path=_sequence_path_from_path(path, index),
                )
            )
            continue
        seen.add(value)


def _validate_unknown_keys(
    data: Mapping[str, Any],
    allowed_keys: set[str],
    issues: list[WorkflowTemplateValidationIssue],
    *,
    path: str,
    subject: str,
) -> None:
    for key in data:
        if key in allowed_keys:
            continue
        issues.append(
            WorkflowTemplateValidationIssue(
                code="unknown_key",
                message=f"{subject.title()} contains unknown field {key!r}.",
                path=f"{path}.{key}" if path else key,
            )
        )


def _path_prefix(source_path: str | Path | None) -> str | None:
    if source_path is None:
        return None
    return str(Path(source_path))


def _child_path(source_path: str | Path | None, key: str) -> str | None:
    prefix = _path_prefix(source_path)
    if prefix is None:
        return key
    return f"{prefix}.{key}"


def _sequence_path(
    source_path: str | Path | None,
    key: str,
    index: int,
) -> str | None:
    prefix = _path_prefix(source_path)
    if prefix is None:
        return f"{key}[{index}]"
    return f"{prefix}.{key}[{index}]"


def _sequence_path_from_path(path: str | None, index: int) -> str | None:
    if path is None:
        return f"[{index}]"
    return f"{path}[{index}]"


_MISSING = object()
