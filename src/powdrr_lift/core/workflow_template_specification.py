from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.core.delivery_profile import PhaseType
from powdrr_lift.core.skill_specification import (
    SUPPORTED_INTERACTION_STYLES,
    SUPPORTED_STEP_TYPES,
    SkillStepGate,
    SkillStepPreStep,
    SkillToolInvocation,
    skill_step_from_data,
)
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)
from powdrr_lift.core.workflow_task_specification import (
    AgentRole,
    AssigneeRole,
    AssigneeType,
    TaskComplexity,
    TaskStatus,
    WorkflowTask,
    build_workflow_task_directory_validation_report,
    save_workflow_task,
    select_ready_workflow_tasks,
    validate_assignee,
)


@dataclass(frozen=True, slots=True)
class WorkflowTemplateValidationIssue(ValidationError):
    pass


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
    assignee_type: AssigneeType = AssigneeType.AGENT
    assignee_role: AssigneeRole = AgentRole.CODER
    details: str | None = None
    llm_type: str | None = None
    interaction_style: str | None = None
    uses_skills: tuple[str, ...] = field(default_factory=tuple)
    tool_invocations: tuple[SkillToolInvocation, ...] = field(default_factory=tuple)
    prompt_catalogs: tuple[str, ...] = field(default_factory=tuple)
    actions: tuple[str, ...] = field(default_factory=tuple)
    actions_declared: bool = False
    output_state_type: str = "state"
    dependent_state: tuple[str, ...] = field(default_factory=tuple)
    generation: WorkflowTaskTemplateGeneration | None = None
    step_type: str = "freeform"
    pre_step: SkillStepPreStep | None = None
    gate: SkillStepGate | None = None
    phase_type: PhaseType | None = None
    persona_id: str | None = None

    def __post_init__(self) -> None:
        if self.actions:
            object.__setattr__(self, "actions_declared", True)
        assignee_type, assignee_role = validate_assignee(
            self.assignee_type, self.assignee_role
        )
        object.__setattr__(self, "assignee_type", assignee_type)
        object.__setattr__(self, "assignee_role", assignee_role)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": self.description,
            "complexity": self.complexity.value,
            "input_state": self.input_state,
            "assignee_type": self.assignee_type.value,
            "assignee_role": self.assignee_role.value,
            "output_state_type": self.output_state_type,
            "dependent_state": list(self.dependent_state),
        }
        step_data: dict[str, Any] = {
            "step_type": self.step_type,
            "details": self.details,
            "llm_type": self.llm_type,
            "interaction_style": self.interaction_style,
            "uses_skills": list(self.uses_skills),
            "tool_invocations": [
                invocation.to_data() for invocation in self.tool_invocations
            ],
        }
        if self.prompt_catalogs:
            step_data["prompt_catalogs"] = list(self.prompt_catalogs)
        if self.actions:
            step_data["actions"] = list(self.actions)
        elif self.actions_declared:
            step_data["actions"] = []
        if self.pre_step is not None:
            step_data["pre_step"] = self.pre_step.to_data()
        if self.gate is not None:
            step_data["gate"] = self.gate.to_data()
        if self.phase_type is not None:
            data["phase_type"] = self.phase_type.value
        if self.persona_id is not None:
            data["persona_id"] = self.persona_id
        data.update({key: value for key, value in step_data.items() if value})
        if self.actions_declared and not self.actions:
            data["actions"] = []
        if self.generation is not None:
            data["generation"] = self.generation.to_data()
        return data


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    when_to_use: tuple[str, ...]
    how_to_fill_this_out: tuple[str, ...]
    task_templates: tuple[WorkflowTaskTemplate, ...]
    invariants: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    id: str | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "when_to_use": list(self.when_to_use),
            "how_to_fill_this_out": list(self.how_to_fill_this_out),
            "task_templates": [
                task_template.to_data() for task_template in self.task_templates
            ],
        }
        if self.id is not None:
            data["id"] = self.id
        if self.invariants:
            data["invariants"] = [dict(item) for item in self.invariants]
        return data

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


def workflow_template_to_yaml(template: WorkflowTemplate) -> str:
    return yaml.safe_dump(template.to_data(), sort_keys=False)


def workflow_template_from_json(json_content: str) -> WorkflowTemplate:
    loaded_content = json.loads(json_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Workflow template JSON must decode to an object.")
    return workflow_template_from_data(cast("Mapping[str, Any]", loaded_content))


def workflow_template_from_yaml(yaml_content: str) -> WorkflowTemplate:
    loaded_content = yaml.safe_load(yaml_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Workflow template YAML must decode to an object.")
    return workflow_template_from_data(cast("Mapping[str, Any]", loaded_content))


def workflow_template_from_data(data: Mapping[str, Any]) -> WorkflowTemplate:
    when_to_use = _required_string_sequence(data, "when_to_use")
    how_to_fill_this_out = _required_string_sequence(data, "how_to_fill_this_out")
    task_templates = _parse_task_templates(data.get("task_templates"))
    raw_invariants = data.get("invariants", [])
    if not isinstance(raw_invariants, list) or not all(
        isinstance(item, Mapping) for item in raw_invariants
    ):
        raise ValueError("invariants must be an array of objects.")
    return WorkflowTemplate(
        when_to_use=when_to_use,
        how_to_fill_this_out=how_to_fill_this_out,
        task_templates=task_templates,
        invariants=tuple(dict(item) for item in raw_invariants),
        id=data.get("id") if isinstance(data.get("id"), str) else None,
    )


def load_workflow_template(path: str | Path) -> WorkflowTemplate:
    template_path = Path(path)
    template_text = template_path.read_text(encoding="utf-8")
    if template_path.suffix.lower() in {".yaml", ".yml"}:
        return workflow_template_from_yaml(template_text)
    return workflow_template_from_json(template_text)


def instantiated_workflow_relationships(
    template_path: str | Path,
    *,
    work_item_name: str,
    workflow_instance_name: str | None,
    template_values: Mapping[str, str] | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Resolve template relationship invariants into durable workflow metadata."""
    template = load_workflow_template(template_path)
    substitutions = {
        "work-item-name": work_item_name,
        "workflow-instance-name": workflow_instance_name or work_item_name,
        **dict(template_values or {}),
    }
    resolved_invariants: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for raw_invariant in template.invariants:
        invariant = _substitute_workflow_placeholders(raw_invariant, substitutions)
        if not isinstance(invariant, dict):
            raise ValueError("Resolved relationship invariant must be an object.")
        invariant_id = invariant.get("id")
        if not isinstance(invariant_id, str) or not invariant_id.strip():
            raise ValueError("Every relationship invariant requires a non-empty id.")
        resolved_invariants.append(invariant)
        relation = dict(invariant)
        relation["invariant_id"] = invariant_id
        relation["source_id"] = relation.get(
            "source_id", substitutions["workflow-instance-name"]
        )
        relation.pop("id", None)
        relationships.append(relation)
    return tuple(resolved_invariants), tuple(relationships)


def instantiate_workflow_template(
    template_path: str | Path,
    work_item_name: str,
    output_root: str | Path = Path("docs") / "workflows",
    workflow_instance_name: str | None = None,
    template_values: Mapping[str, str] | None = None,
) -> tuple[Path, tuple[WorkflowTask, ...]]:
    """Materialize a workflow template as validated durable task documents."""
    template = load_workflow_template(template_path)
    template_identity = template.id or Path(template_path).stem
    slug = re.sub(r"[^a-z0-9]+", "-", work_item_name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("work-item-name must contain at least one letter or digit")

    output_directory = Path(output_root) / slug
    if (
        workflow_instance_name is None
        and output_directory.exists()
        and any(output_directory.iterdir())
    ):
        raise FileExistsError(
            f"Workflow output directory is not empty: {output_directory}. "
            "Choose a new work item or remove it with explicit approval."
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    instance_slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        (workflow_instance_name or work_item_name).strip().lower(),
    ).strip("-")
    if not instance_slug:
        raise ValueError(
            "workflow-instance-name must contain at least one letter or digit"
        )
    task_prefix = f"{instance_slug}-" if workflow_instance_name else ""
    substitutions = {
        "work-item-name": work_item_name,
        "workflow-instance-name": workflow_instance_name or work_item_name,
    }
    explicit_substitutions = dict(template_values or {})
    substitutions.update(explicit_substitutions)
    task_ids = tuple(
        f"{task_prefix}task-{index + 1:03d}"
        for index in range(len(template.task_templates))
    )
    substitutions.update(
        {f"upstream-task-{index}": task_id for index, task_id in enumerate(task_ids)}
    )
    tasks: list[WorkflowTask] = []
    for index, task_template in enumerate(template.task_templates):
        upstream_task_indexes = _upstream_task_template_indexes(
            task_template.input_state
        )
        for upstream_index in upstream_task_indexes:
            if upstream_index >= len(task_ids):
                raise ValueError(
                    f"Task template {index} references unknown upstream task "
                    f"template index: {upstream_index}"
                )
        task = WorkflowTask(
            task_id=task_ids[index],
            status=TaskStatus.OPEN,
            description=task_template.description,
            complexity=task_template.complexity,
            input_state=_substitute_workflow_placeholders(
                task_template.input_state, substitutions
            ),
            assignee_type=task_template.assignee_type,
            assignee_role=task_template.assignee_role,
            details=_add_instantiation_context(
                task_template.details,
                work_item_name=work_item_name,
                workflow_instance_name=workflow_instance_name,
            ),
            llm_type=task_template.llm_type,
            interaction_style=task_template.interaction_style,
            uses_skills=task_template.uses_skills,
            tool_invocations=tuple(
                _substitute_tool_invocation(invocation, explicit_substitutions)
                for invocation in task_template.tool_invocations
            ),
            output_state_type=task_template.output_state_type,
            step_type=task_template.step_type,
            pre_step=task_template.pre_step,
            upstream_task_ids=tuple(
                task_ids[upstream_index] for upstream_index in upstream_task_indexes
            ),
            dependent_state=task_template.dependent_state,
            workflow_template=template_identity,
            phase_type=task_template.phase_type,
            persona_id=task_template.persona_id,
        )
        task_path = output_directory / f"{task.task_id}.yaml"
        if task_path.exists():
            raise FileExistsError(
                f"Workflow task already exists: {task_path}. Choose a new "
                "workflow-instance-name."
            )
        save_workflow_task(task, task_path)
        tasks.append(task)

    report = build_workflow_task_directory_validation_report(output_directory)
    if not report.validation_successful:
        issues = "; ".join(issue.message for issue in report.issues)
        raise ValueError(f"Generated workflow failed validation: {issues}")

    ready_tasks = select_ready_workflow_tasks(tuple(tasks))
    if len(ready_tasks) != 1 or ready_tasks[0].task_id != task_ids[0]:
        raise ValueError(
            "Generated workflow must have exactly one ready first task; "
            f"found {[task.task_id for task in ready_tasks]}"
        )
    return output_directory, tuple(tasks)


def _substitute_workflow_placeholders(
    value: Any,
    substitutions: Mapping[str, str],
) -> Any:
    if isinstance(value, str):
        result = value
        for placeholder, replacement in substitutions.items():
            result = result.replace(f"<{placeholder}>", replacement)
        return result
    if isinstance(value, Mapping):
        return {
            key: _substitute_workflow_placeholders(item, substitutions)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(
            _substitute_workflow_placeholders(item, substitutions) for item in value
        )
    if isinstance(value, list):
        return [
            _substitute_workflow_placeholders(item, substitutions) for item in value
        ]
    return value


def _substitute_tool_invocation(
    invocation: SkillToolInvocation,
    substitutions: Mapping[str, str],
) -> SkillToolInvocation:
    return SkillToolInvocation(
        tool=invocation.tool,
        command=tuple(
            _substitute_workflow_placeholders(item, substitutions)
            for item in invocation.command
        ),
        cwd=(
            _substitute_workflow_placeholders(invocation.cwd, substitutions)
            if invocation.cwd is not None
            else None
        ),
        env=tuple(
            (
                key,
                _substitute_workflow_placeholders(value, substitutions),
            )
            for key, value in invocation.env
        ),
    )


def _add_instantiation_context(
    details: str | None,
    *,
    work_item_name: str,
    workflow_instance_name: str | None,
) -> str | None:
    """Append dynamic workflow context as instructions for the task's LLM."""
    context = (
        "Instantiation context: work item name is "
        f"{work_item_name.strip()!r}; workflow instance name is "
        f"{(workflow_instance_name or work_item_name).strip()!r}. "
        "Use these values as search context and verify repository documents "
        "before deciding what they identify."
    )
    if details is None:
        return context
    return f"{details}\n\n{context}"


def save_workflow_template(template: WorkflowTemplate, path: str | Path) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        workflow_template_to_yaml(template)
        if resolved_path.suffix.lower() in {".yaml", ".yml"}
        else workflow_template_to_json(template)
    )
    resolved_path.write_text(content, encoding="utf-8")
    return resolved_path


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

    if "id" in raw_template and (
        not isinstance(raw_template["id"], str) or not raw_template["id"].strip()
    ):
        issues.append(
            WorkflowTemplateValidationIssue(
                code="invalid_id",
                message="Workflow template id must be a non-empty string.",
                path=_child_path(source_path, "id"),
            )
        )

    _validate_unknown_keys(
        raw_template,
        {
            "id",
            "when_to_use",
            "how_to_fill_this_out",
            "task_templates",
            "invariants",
        },
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
    raw_invariants = raw_template.get("invariants", [])
    if (
        not isinstance(raw_invariants, Sequence)
        or isinstance(raw_invariants, (str, bytes, bytearray))
        or not all(isinstance(item, Mapping) for item in raw_invariants)
    ):
        issues.append(
            WorkflowTemplateValidationIssue(
                code="invalid_invariants",
                message="invariants must be an array of objects.",
                path=_child_path(source_path, "invariants"),
            )
        )
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
                "step_type",
                "complexity",
                "input_state",
                "assignee_type",
                "assignee_role",
                "details",
                "llm_type",
                "interaction_style",
                "uses_skills",
                "tool_invocations",
                "prompt_catalogs",
                "actions",
                "pre_step",
                "output_state_type",
                "dependent_state",
                "generation",
                "gate",
                "phase_type",
                "persona_id",
            },
            issues,
            path=task_template_path or "",
            subject="workflow task template",
        )

        raw_phase_type = raw_task_template_mapping.get("phase_type")
        if raw_phase_type is not None:
            try:
                _optional_phase_type(raw_phase_type)
            except ValueError as exc:
                issues.append(
                    WorkflowTemplateValidationIssue(
                        code="invalid_phase_type",
                        message=str(exc),
                        path=_child_path(task_template_path, "phase_type"),
                    )
                )
        raw_persona_id = raw_task_template_mapping.get("persona_id")
        if raw_persona_id is not None and _optional_persona_id(raw_persona_id) is None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="invalid_persona_id",
                    message=(
                        "Workflow task template persona_id must be a non-empty string."
                    ),
                    path=_child_path(task_template_path, "persona_id"),
                )
            )

        step_type = raw_task_template_mapping.get("step_type", "freeform")
        if not isinstance(step_type, str) or step_type not in SUPPORTED_STEP_TYPES:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="invalid_step_type_value",
                    message=(
                        "Workflow task template step_type must be "
                        "freeform, invoke_tool, or gate."
                    ),
                    path=_child_path(task_template_path, "step_type"),
                )
            )
        interaction_style = raw_task_template_mapping.get("interaction_style")
        if interaction_style is not None and (
            not isinstance(interaction_style, str)
            or interaction_style.strip() not in SUPPORTED_INTERACTION_STYLES
        ):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="invalid_interaction_style",
                    message=(
                        "Workflow task template interaction_style must be one of: "
                        + ", ".join(sorted(SUPPORTED_INTERACTION_STYLES))
                        + "."
                    ),
                    path=_child_path(task_template_path, "interaction_style"),
                )
            )
        pre_step = raw_task_template_mapping.get("pre_step")
        uses_skills = raw_task_template_mapping.get("uses_skills")
        if (
            pre_step is not None
            and isinstance(uses_skills, Sequence)
            and not isinstance(uses_skills, (str, bytes, bytearray))
            and uses_skills
        ):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="uses_skills_with_pre_step",
                    message=(
                        "Workflow task templates must use either uses_skills or "
                        "pre_step, not both."
                    ),
                    path=_child_path(task_template_path, "pre_step"),
                )
            )
        if step_type in {"invoke_tool", "gate"} and not isinstance(pre_step, Mapping):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_pre_step",
                    message=(
                        "invoke_tool and gate task templates must declare a pre_step."
                    ),
                    path=_child_path(task_template_path, "pre_step"),
                )
            )
        elif step_type == "invoke_tool" and raw_task_template_mapping.get(
            "tool_invocations"
        ):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="unexpected_tool_invocations",
                    message=(
                        "invoke_tool task templates must use pre_step instead of "
                        "tool_invocations."
                    ),
                    path=_child_path(task_template_path, "tool_invocations"),
                )
            )
        elif step_type == "freeform" and pre_step is not None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="unexpected_pre_step",
                    message=(
                        "Only invoke_tool and gate task templates may declare pre_step."
                    ),
                    path=_child_path(task_template_path, "pre_step"),
                )
            )

        prompt_catalogs = raw_task_template_mapping.get("prompt_catalogs")
        if (
            isinstance(prompt_catalogs, Sequence)
            and not isinstance(prompt_catalogs, (str, bytes, bytearray))
            and not prompt_catalogs
        ):
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="empty_prompt_catalogs",
                    message=(
                        "Workflow task template prompt_catalogs must omit the field "
                        "when no prompt catalog is needed."
                    ),
                    path=_child_path(task_template_path, "prompt_catalogs"),
                )
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

        assignee_type = _optional_string(raw_task_template_mapping.get("assignee_type"))
        assignee_role = _optional_string(raw_task_template_mapping.get("assignee_role"))
        if assignee_type is None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_assignee_type",
                    message=(
                        "Workflow task templates must include a non-empty "
                        "assignee_type."
                    ),
                    path=_child_path(task_template_path, "assignee_type"),
                )
            )
        if assignee_role is None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_assignee_role",
                    message=(
                        "Workflow task templates must include a non-empty "
                        "assignee_role."
                    ),
                    path=_child_path(task_template_path, "assignee_role"),
                )
            )
        if assignee_type is not None and assignee_role is not None:
            try:
                validate_assignee(assignee_type, assignee_role)
            except ValueError as exc:
                issues.append(
                    WorkflowTemplateValidationIssue(
                        code="invalid_assignee_role",
                        message=str(exc),
                        path=_child_path(task_template_path, "assignee_role"),
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
        else:
            _validate_detail_input_placeholders(
                raw_task_template_mapping.get("details"),
                raw_task_template_mapping.get("input_state"),
                issues,
                path=_child_path(task_template_path, "details"),
            )

        output_state_type = _optional_string(
            raw_task_template_mapping.get("output_state_type")
        )
        if output_state_type is None:
            issues.append(
                WorkflowTemplateValidationIssue(
                    code="missing_output_state_type",
                    message=(
                        "Workflow task templates must include a non-empty "
                        "output_state_type."
                    ),
                    path=_child_path(task_template_path, "output_state_type"),
                )
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
        input_state = raw_task_template.get("input_state")
        for upstream_index in _upstream_task_template_indexes(input_state):
            if upstream_index >= task_template_count:
                issues.append(
                    WorkflowTemplateValidationIssue(
                        code="missing_upstream_task_template",
                        message=(
                            "Workflow task input references an unknown upstream "
                            "task template index."
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
                        message=("A workflow task template cannot depend on itself."),
                        path=_sequence_path(
                            source_path,
                            "task_templates",
                            index,
                        ),
                    )
                )

    for index, raw_task_template in task_template_reports:
        if raw_task_template is None:
            continue
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
    assignee_type, assignee_role = validate_assignee(
        _required_string(data, "assignee_type"),
        _required_string(data, "assignee_role"),
    )
    step = skill_step_from_data(data)
    output_state_type = _required_string(data, "output_state_type")
    dependent_state = _required_string_sequence(data, "dependent_state")
    phase_type = _optional_phase_type(data.get("phase_type"))
    persona_id = _optional_persona_id(data.get("persona_id"))
    generation = data.get("generation")
    parsed_generation = None
    if generation is not None:
        parsed_generation = _parse_generation(generation)
    return WorkflowTaskTemplate(
        description=description,
        complexity=complexity,
        input_state=input_state,
        assignee_type=assignee_type,
        assignee_role=assignee_role,
        details=step.details,
        llm_type=step.llm_type,
        interaction_style=step.interaction_style,
        uses_skills=step.uses_skills,
        tool_invocations=step.tool_invocations,
        prompt_catalogs=step.prompt_catalogs,
        actions=step.actions,
        actions_declared=step.actions_declared,
        step_type=step.step_type,
        pre_step=step.pre_step,
        gate=step.gate,
        output_state_type=output_state_type,
        dependent_state=dependent_state,
        generation=parsed_generation,
        phase_type=phase_type,
        persona_id=persona_id,
    )


def _upstream_task_template_indexes(value: Any) -> tuple[int, ...]:
    indexes: list[int] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            match = re.fullmatch(r"<upstream-task-(\d+)>\..+", item)
            if match is not None:
                index = int(match.group(1))
                if index not in indexes:
                    indexes.append(index)
            return
        if isinstance(item, Mapping):
            for nested_item in item.values():
                visit(nested_item)
            return
        if isinstance(item, (list, tuple)):
            for nested_item in item:
                visit(nested_item)

    visit(value)
    return tuple(indexes)


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
        "issues": [validation_error_to_data(issue) for issue in report.issues],
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


def _optional_phase_type(value: object) -> PhaseType | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Workflow task template phase_type must be a string.")
    try:
        return PhaseType(value)
    except ValueError as exc:
        allowed = ", ".join(phase.value for phase in PhaseType)
        raise ValueError(
            "Workflow task template phase_type must be one of " + allowed + "."
        ) from exc


def _optional_persona_id(value: object) -> str | None:
    return _optional_string(value)


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


def _validate_detail_input_placeholders(
    details: object,
    input_state: object,
    issues: list[WorkflowTemplateValidationIssue],
    *,
    path: str | None,
) -> None:
    if not isinstance(details, str) or not isinstance(input_state, Mapping):
        return

    declared_inputs = {str(key) for key in input_state}
    upstream_inputs = {
        f"upstream-task-{index}"
        for index in _upstream_task_template_indexes(input_state)
    }
    seen: set[str] = set()
    for match in re.finditer(r"<([^<>]+)>", details):
        placeholder = match.group(1)
        if placeholder in seen or placeholder in declared_inputs:
            seen.add(placeholder)
            continue
        if placeholder in upstream_inputs:
            seen.add(placeholder)
            continue
        seen.add(placeholder)
        issues.append(
            WorkflowTemplateValidationIssue(
                code="undeclared_detail_input",
                message=(
                    f"Workflow task details reference placeholder "
                    f"<{placeholder}> but {placeholder!r} is not listed in input_state."
                ),
                path=path,
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
