from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from powdrr_lift.basedpyright_tools import BASEDPYRIGHT_TOOLS
from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)

SUPPORTED_SKILL_TOOL_TYPES = (
    frozenset(
        {
            "shell",
            "internal",
            "git",
            "gh",
            "fuzzy-match",
            "enrich",
            "validate_edit",
            "apply_edit",
            "ref",
        }
    )
    | BASEDPYRIGHT_TOOLS
)
TOOL_INVOCATION_PACKAGES: dict[str, tuple[dict[str, Any], ...]] = {
    "git_readonly": (
        {"tool": "git", "operation": "status"},
        {"tool": "git", "operation": "remote"},
        {"tool": "git", "operation": "branch_current"},
        {"tool": "git", "operation": "default_branch"},
        {"tool": "git", "operation": "show_ref"},
    ),
    "git_additive": (
        {"tool": "git", "operation": "add"},
        {"tool": "git", "operation": "commit"},
        {"tool": "git", "operation": "push"},
        {"tool": "git", "operation": "switch"},
        {"tool": "git", "operation": "switch_create"},
        {"tool": "git", "operation": "move"},
        {"tool": "git", "operation": "rename"},
    ),
    "git_readonly_and_additive": (
        {"tool": "git", "operation": "status"},
        {"tool": "git", "operation": "remote"},
        {"tool": "git", "operation": "branch_current"},
        {"tool": "git", "operation": "default_branch"},
        {"tool": "git", "operation": "show_ref"},
        {"tool": "git", "operation": "add"},
        {"tool": "git", "operation": "commit"},
        {"tool": "git", "operation": "push"},
        {"tool": "git", "operation": "switch"},
        {"tool": "git", "operation": "switch_create"},
        {"tool": "git", "operation": "move"},
        {"tool": "git", "operation": "rename"},
    ),
    "gh_readonly": (
        {"tool": "gh", "operation": "pr_view"},
        {"tool": "gh", "operation": "pr_diff"},
        {"tool": "gh", "operation": "pr_checks"},
        {"tool": "gh", "operation": "pr_comments"},
    ),
    "gh_write": (
        {"tool": "gh", "operation": "pr_create"},
        {"tool": "gh", "operation": "pr_edit"},
        {"tool": "gh", "operation": "pr_review_comment"},
    ),
    "gh_readonly_and_write": (
        {"tool": "gh", "operation": "pr_view"},
        {"tool": "gh", "operation": "pr_diff"},
        {"tool": "gh", "operation": "pr_checks"},
        {"tool": "gh", "operation": "pr_comments"},
        {"tool": "gh", "operation": "pr_create"},
        {"tool": "gh", "operation": "pr_edit"},
        {"tool": "gh", "operation": "pr_review_comment"},
    ),
}
SUPPORTED_TOOL_INVOCATION_PACKAGES = frozenset(TOOL_INVOCATION_PACKAGES)
SUPPORTED_PROMPT_CATALOGS = frozenset(
    {
        "context_types",
        "skills",
        "actions",
        # Delivery profiles may attach durable persona responsibility catalogs
        # to generated workflow tasks.  Keep these names in the same validated
        # namespace as the original prompt catalogs so compiled tasks can be
        # loaded again without losing persona context.
        "architect-responsibilities",
        "engineering-manager-responsibilities",
        "engineer-responsibilities",
        "specification-reviewer-responsibilities",
        "code-reviewer-responsibilities",
    }
)
SUPPORTED_STEP_ACTIONS = frozenset(
    {
        "gather_context",
        "prompt_user",
        "edit",
        "yaml_edit",
        "file_management",
        "delete_file",
        "invoke_skill",
        "invoke_tool",
        "read_document",
        "list_files",
        "goto_step",
        "next_step",
        "complete",
    }
)
UNIVERSAL_STEP_ACTIONS = frozenset({"prompt_user", "next_step", "emit_outputs"})
SUPPORTED_STEP_TYPES = frozenset(
    {
        "governed",
        "freeform",
        "predicated",
        "uses_skill",
        "invoke_tool",
        "gate",
        "coding_loop",
    }
)
SUPPORTED_INTERACTION_STYLES = frozenset(
    {"engineering", "observational_review", "devils_advocate"}
)


@dataclass(frozen=True, slots=True)
class SkillValidationIssue(ValidationError):
    pass


@dataclass(frozen=True, slots=True)
class SkillValidationReport:
    validation_successful: bool
    skill_names: list[str] = field(default_factory=list)
    skill_paths: list[str] = field(default_factory=list)
    issues: list[SkillValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _SkillDirectoryContents:
    skill_names: list[str]
    skill_paths: list[str]
    skills_by_name: dict[str, Skill]
    skill_paths_by_name: dict[str, Path]
    step_references: list[tuple[Path, Skill]]


@dataclass(frozen=True, slots=True)
class SkillToolInvocation:
    tool: str
    command: tuple[str, ...] = field(default_factory=tuple)
    operation: str | None = None
    label: str | None = None
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tool": self.tool,
        }
        if self.command:
            data["command"] = list(self.command)
        if self.operation is not None:
            data["operation"] = self.operation
        if self.label is not None:
            data["label"] = self.label
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.env:
            data["env"] = {key: value for key, value in self.env}
        return data


@dataclass(frozen=True, slots=True)
class SkillStepInput:
    name: str
    type: str = "any"
    required: bool = True
    source: str = "previous_step"

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SkillStepOutput:
    name: str
    type: str = "any"
    required_for_next_step: bool = False
    scope: str = "skill"
    schema: Mapping[str, Any] | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required_for_next_step": self.required_for_next_step,
            "scope": self.scope,
        }
        if self.schema is not None:
            data["schema"] = dict(self.schema)
        return data


@dataclass(frozen=True, slots=True)
class SkillStepPreStep:
    action: str
    template: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "template": dict(self.template),
        }


@dataclass(frozen=True, slots=True)
class SkillStepGate:
    outcome: Mapping[str, Any]
    goto_step: str
    retry_context: str
    success_goto_step: str | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "outcome": dict(self.outcome),
            "goto_step": self.goto_step,
            "retry_context": self.retry_context,
        }
        if self.success_goto_step is not None:
            data["success_goto_step"] = self.success_goto_step
        return data


@dataclass(frozen=True, slots=True)
class CodingLoopVerification:
    """One deterministic command used to verify a coding-loop iteration."""

    id: str
    command: str | tuple[str, ...]
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "command": (
                self.command if isinstance(self.command, str) else list(self.command)
            ),
        }
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.env:
            data["env"] = {key: value for key, value in self.env}
        return data


@dataclass(frozen=True, slots=True)
class CodingLoopSpec:
    """Harness policy for an inspect, edit, verify, and repair loop."""

    goal: str
    verification: tuple[CodingLoopVerification, ...] = field(default_factory=tuple)
    stopping_conditions: tuple[str, ...] = field(default_factory=tuple)
    max_iterations: int = 12

    def to_data(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "verification": [item.to_data() for item in self.verification],
            "stopping_conditions": list(self.stopping_conditions),
            "max_iterations": self.max_iterations,
        }


@dataclass(frozen=True, slots=True)
class SkillStepRequiredAction:
    """Action evidence required before a predicated step may complete."""

    action: str
    targets_from: str | None = None
    match_field: str | None = None
    exactly: int | None = None
    parameters: Mapping[str, Any] | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.action}
        if self.targets_from is not None:
            data["targets_from"] = self.targets_from
        if self.match_field is not None:
            data["match_field"] = self.match_field
        if self.exactly is not None:
            data["exactly"] = self.exactly
        if self.parameters is not None:
            data["parameters"] = dict(self.parameters)
        return data


@dataclass(frozen=True, slots=True)
class SkillStepCompletion:
    """Deterministic completion conditions for a predicated step."""

    required_outputs: tuple[str, ...]
    required_actions: tuple[SkillStepRequiredAction, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"required_outputs": list(self.required_outputs)}
        if self.required_actions:
            data["required_actions"] = [
                item.to_data() for item in self.required_actions
            ]
        return data


@dataclass(frozen=True, slots=True)
class SkillUsesSkillBinding:
    """One explicit handoff binding for a deterministic nested skill call."""

    name: str
    ref: str
    schema: Mapping[str, Any] | None = None

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "ref": self.ref}
        if self.schema is not None:
            data["schema"] = dict(self.schema)
        return data


@dataclass(frozen=True, slots=True)
class SkillUsesSkill:
    """A typed, single nested-skill invocation owned by a uses_skill step."""

    skill: str
    inputs: tuple[SkillUsesSkillBinding, ...] = field(default_factory=tuple)
    outputs: tuple[SkillUsesSkillBinding, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"skill": self.skill}
        if self.inputs:
            data["inputs"] = [binding.to_data() for binding in self.inputs]
        if self.outputs:
            data["outputs"] = [binding.to_data() for binding in self.outputs]
        return data


@dataclass(frozen=True, slots=True)
class SkillStep:
    description: str
    details: str | None = None
    llm_type: str | None = None
    interaction_style: str | None = None
    tool_invocations: tuple[SkillToolInvocation, ...] = field(default_factory=tuple)
    tool_invocation_packages: tuple[str, ...] = field(default_factory=tuple)
    prompt_catalogs: tuple[str, ...] = field(default_factory=tuple)
    actions: tuple[str, ...] = field(default_factory=tuple)
    # An explicit empty list is a closed contract; omission remains legacy.
    actions_declared: bool = False
    next_step_override: str | None = None
    id: str | None = None
    inputs: tuple[SkillStepInput, ...] = field(default_factory=tuple)
    outputs: tuple[SkillStepOutput, ...] = field(default_factory=tuple)
    step_type: str = "governed"
    pre_step: SkillStepPreStep | None = None
    gate: SkillStepGate | None = None
    validation_gate: Mapping[str, Any] | None = None
    coding_loop: CodingLoopSpec | None = None
    completion: SkillStepCompletion | None = None
    uses_skill: SkillUsesSkill | None = None

    def __post_init__(self) -> None:
        if self.actions:
            object.__setattr__(self, "actions_declared", True)
        if self.uses_skill is not None and self.step_type in {"freeform", "governed"}:
            object.__setattr__(self, "step_type", "uses_skill")

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": self.description,
            "step_type": self.step_type,
        }
        if self.id is not None:
            data["id"] = self.id
        if self.details is not None:
            data["details"] = self.details
        if self.llm_type is not None:
            data["llm_type"] = self.llm_type
        if self.interaction_style is not None:
            data["interaction_style"] = self.interaction_style
        if self.uses_skill is not None:
            data["uses_skill"] = self.uses_skill.to_data()
        if self.tool_invocations:
            data["tool_invocations"] = [
                tool_invocation.to_data() for tool_invocation in self.tool_invocations
            ]
        if self.tool_invocation_packages:
            data["tool_invocation_packages"] = list(self.tool_invocation_packages)
        if self.prompt_catalogs:
            data["prompt_catalogs"] = list(self.prompt_catalogs)
        if self.actions:
            data["actions"] = list(self.actions)
        elif self.actions_declared:
            data["actions"] = []
        if self.next_step_override is not None:
            data["next_step_override"] = self.next_step_override
        if self.pre_step is not None:
            data["pre_step"] = self.pre_step.to_data()
        if self.gate is not None:
            data["gate"] = self.gate.to_data()
        if self.coding_loop is not None:
            data["coding_loop"] = self.coding_loop.to_data()
        if self.completion is not None:
            data["completion"] = self.completion.to_data()
        if self.validation_gate is not None:
            data["validation_gate"] = self.validation_gate
        if self.inputs:
            data["inputs"] = [item.to_data() for item in self.inputs]
        if self.outputs:
            data["outputs"] = [item.to_data() for item in self.outputs]
        return data


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    when_to_use: tuple[str, ...]
    steps: tuple[SkillStep, ...]
    inputs: tuple[SkillStepInput, ...] = field(default_factory=tuple)
    adversarial: bool | None = None
    interaction_style: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "when_to_use": list(self.when_to_use),
            "steps": [step.to_data() for step in self.steps],
            **(
                {"inputs": [item.to_data() for item in self.inputs]}
                if self.inputs
                else {}
            ),
            **(
                {"interaction_style": self.interaction_style}
                if self.interaction_style is not None
                else {}
            ),
            **(
                {"adversarial": self.adversarial}
                if self.adversarial is not None
                else {}
            ),
        }

    def to_json(self) -> str:
        return skill_to_json(self)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Skill:
        return skill_from_data(data)

    @classmethod
    def from_json(cls, json_content: str) -> Skill:
        return skill_from_json(json_content)

    @classmethod
    def from_file(cls, path: str | Path) -> Skill:
        return load_skill(path)

    def save(self, path: str | Path) -> Path:
        return save_skill(self, path)


SkillDocument = Skill


def skill_to_json(skill: Skill) -> str:
    return json.dumps(skill.to_data(), indent=2, ensure_ascii=False) + "\n"


def skill_to_yaml(skill: Skill) -> str:
    return yaml.safe_dump(skill.to_data(), sort_keys=False)


def skill_from_json(json_content: str) -> Skill:
    loaded_content = json.loads(json_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Skill JSON must decode to an object.")
    return skill_from_data(cast("Mapping[str, Any]", loaded_content))


def skill_from_yaml(yaml_content: str) -> Skill:
    loaded_content = yaml.safe_load(yaml_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Skill YAML must decode to an object.")
    return skill_from_data(cast("Mapping[str, Any]", loaded_content))


def skill_from_data(data: Mapping[str, Any]) -> Skill:
    name = _required_string(data, "name")
    when_to_use = _required_string_sequence(data, "when_to_use")
    steps = _parse_steps(data.get("steps"))
    inputs = _parse_step_inputs(data.get("inputs", []))
    adversarial = data.get("adversarial")
    if adversarial is not None and not isinstance(adversarial, bool):
        raise ValueError("Skill adversarial must be a boolean.")
    raw_interaction_style = data.get("interaction_style")
    interaction_style = _optional_interaction_style(raw_interaction_style)
    if raw_interaction_style is not None and interaction_style is None:
        raise ValueError(
            "Skill interaction_style must be one of: "
            + ", ".join(sorted(SUPPORTED_INTERACTION_STYLES))
            + "."
        )
    return Skill(
        name=name,
        when_to_use=when_to_use,
        steps=steps,
        inputs=inputs,
        adversarial=adversarial,
        interaction_style=interaction_style,
    )


def load_skill(path: str | Path) -> Skill:
    skill_path = Path(path)
    skill_content = skill_path.read_text(encoding="utf-8")
    if skill_path.suffix.lower() in {".yaml", ".yml"}:
        return skill_from_yaml(skill_content)
    return skill_from_json(skill_content)


def save_skill(skill: Skill, path: str | Path) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        skill_to_yaml(skill)
        if resolved_path.suffix.lower() in {".yaml", ".yml"}
        else skill_to_json(skill)
    )
    resolved_path.write_text(content, encoding="utf-8")
    return resolved_path


def load_skills(directory: str | Path) -> tuple[Skill, ...]:
    directory_path = Path(directory)
    skill_paths = sorted(
        path
        for pattern in ("*.yaml", "*.yml", "*.json")
        for path in directory_path.glob(pattern)
        if path.is_file()
    )
    return tuple(load_skill(skill_path) for skill_path in skill_paths)


def build_skill_validation_report(
    json_content: str,
    *,
    source_path: str | Path | None = None,
) -> SkillValidationReport:
    try:
        if source_path is not None and Path(source_path).suffix.lower() in {
            ".yaml",
            ".yml",
        }:
            loaded_content = yaml.safe_load(json_content)
        else:
            loaded_content = json.loads(json_content)
    except Exception as exc:  # noqa: BLE001
        document_type = (
            "YAML"
            if source_path is not None
            and Path(source_path).suffix.lower() in {".yaml", ".yml"}
            else "JSON"
        )
        return SkillValidationReport(
            validation_successful=False,
            issues=[
                SkillValidationIssue(
                    code=(
                        "invalid_yaml" if document_type == "YAML" else "invalid_json"
                    ),
                    message=f"Could not parse {document_type} skill document: {exc}",
                    path=_path_prefix(source_path),
                )
            ],
        )

    if not isinstance(loaded_content, Mapping):
        return SkillValidationReport(
            validation_successful=False,
            issues=[
                SkillValidationIssue(
                    code="invalid_root_type",
                    message="Skill document must decode to an object.",
                    path=_path_prefix(source_path),
                )
            ],
        )

    raw_skill = cast("Mapping[str, Any]", loaded_content)
    issues: list[SkillValidationIssue] = []

    _validate_unknown_keys(
        raw_skill,
        {
            "name",
            "when_to_use",
            "steps",
            "inputs",
            "adversarial",
            "interaction_style",
        },
        issues,
        path=_path_prefix(source_path) or "",
        subject="skill",
    )

    name = _optional_string(raw_skill.get("name"))
    if name is None:
        issues.append(
            SkillValidationIssue(
                code="missing_name",
                message="Skill entries must include a non-empty name.",
                path=_child_path(source_path, "name"),
            )
        )

    adversarial = raw_skill.get("adversarial")
    if adversarial is not None and not isinstance(adversarial, bool):
        issues.append(
            SkillValidationIssue(
                code="invalid_adversarial_type",
                message="Skill adversarial must be a boolean.",
                path=_child_path(source_path, "adversarial"),
            )
        )

    interaction_style = raw_skill.get("interaction_style")
    if interaction_style is not None:
        normalized_style = _optional_interaction_style(interaction_style)
        if normalized_style is None:
            issues.append(
                SkillValidationIssue(
                    code="invalid_interaction_style",
                    message=(
                        "Skill interaction_style must be one of: "
                        + ", ".join(sorted(SUPPORTED_INTERACTION_STYLES))
                        + "."
                    ),
                    path=_child_path(source_path, "interaction_style"),
                )
            )

    when_to_use = raw_skill.get("when_to_use")
    if not isinstance(when_to_use, Sequence) or isinstance(
        when_to_use,
        (str, bytes, bytearray),
    ):
        issues.append(
            SkillValidationIssue(
                code="invalid_when_to_use_type",
                message="Skill when_to_use must be an array.",
                path=_child_path(source_path, "when_to_use"),
            )
        )
    else:
        if len(when_to_use) == 0:
            issues.append(
                SkillValidationIssue(
                    code="missing_when_to_use",
                    message="Skill entries must include at least one when_to_use item.",
                    path=_child_path(source_path, "when_to_use"),
                )
            )
        for index, item in enumerate(when_to_use):
            if _optional_string(item) is None:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_when_to_use_item",
                        message="Skill when_to_use items must be non-empty strings.",
                        path=_sequence_path(source_path, "when_to_use", index),
                    )
                )

    steps = raw_skill.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes, bytearray)):
        issues.append(
            SkillValidationIssue(
                code="invalid_steps_type",
                message="Skill steps must be an array.",
                path=_child_path(source_path, "steps"),
            )
        )
    else:
        if len(steps) == 0:
            issues.append(
                SkillValidationIssue(
                    code="missing_steps",
                    message="Skill entries must include at least one step.",
                    path=_child_path(source_path, "steps"),
                )
            )

    raw_inputs = raw_skill.get("inputs", [])
    if raw_inputs is not None:
        try:
            _parse_step_inputs(raw_inputs)
        except ValueError as exc:
            issues.append(
                SkillValidationIssue(
                    code="invalid_inputs",
                    message=str(exc),
                    path=_child_path(source_path, "inputs"),
                )
            )
        seen_step_ids: set[str] = set()
        for index, step in enumerate(cast("Sequence[Any]", steps)):
            step_path = _sequence_path(source_path, "steps", index)
            if not isinstance(step, Mapping):
                issues.append(
                    SkillValidationIssue(
                        code="invalid_step_type",
                        message="Skill steps must be objects.",
                        path=step_path,
                    )
                )
                continue
            step_mapping = cast("Mapping[str, Any]", step)
            _validate_unknown_keys(
                step_mapping,
                {
                    "id",
                    "description",
                    "step_type",
                    "details",
                    "llm_type",
                    "interaction_style",
                    "uses_skill",
                    "tool_invocations",
                    "tool_invocation_packages",
                    "prompt_catalogs",
                    "actions",
                    "next_step_override",
                    "pre_step",
                    "gate",
                    "validation_gate",
                    "coding_loop",
                    "completion",
                    "inputs",
                    "outputs",
                },
                issues,
                path=step_path or "",
                subject="skill step",
            )

            description = _optional_string(step_mapping.get("description"))
            if description is None:
                issues.append(
                    SkillValidationIssue(
                        code="missing_description",
                        message="Skill steps must include a non-empty description.",
                        path=_child_path(step_path, "description"),
                    )
                )

            step_id = step_mapping.get("id")
            normalized_step_id = _optional_string(step_id)
            if step_id is not None and normalized_step_id is None:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_step_id",
                        message="Skill step id must be a non-empty string.",
                        path=_child_path(step_path, "id"),
                    )
                )
            elif normalized_step_id is not None:
                if normalized_step_id in seen_step_ids:
                    issues.append(
                        SkillValidationIssue(
                            code="duplicate_step_id",
                            message=(
                                f"Skill step id {normalized_step_id!r} must be "
                                "unique within the skill."
                            ),
                            path=_child_path(step_path, "id"),
                        )
                    )
                seen_step_ids.add(normalized_step_id)

            details = step_mapping.get("details")
            if details is not None and _optional_string(details) is None:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_details",
                        message="Skill step details must be a non-empty string.",
                        path=_child_path(step_path, "details"),
                    )
                )

            llm_type = step_mapping.get("llm_type")
            if llm_type is not None and _optional_string(llm_type) is None:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_llm_type",
                        message="Skill step llm_type must be a non-empty string.",
                        path=_child_path(step_path, "llm_type"),
                    )
                )

            next_step_override = step_mapping.get("next_step_override")
            if (
                next_step_override is not None
                and _optional_string(next_step_override) is None
            ):
                issues.append(
                    SkillValidationIssue(
                        code="invalid_next_step_override",
                        message=(
                            "Skill step next_step_override must be a non-empty string."
                        ),
                        path=_child_path(step_path, "next_step_override"),
                    )
                )

            interaction_style = step_mapping.get("interaction_style")
            if interaction_style is not None:
                normalized_style = _optional_interaction_style(interaction_style)
                if normalized_style is None:
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_interaction_style",
                            message=(
                                "Skill step interaction_style must be one of: "
                                + ", ".join(sorted(SUPPORTED_INTERACTION_STYLES))
                                + "."
                            ),
                            path=_child_path(step_path, "interaction_style"),
                        )
                    )

            step_type = step_mapping.get("step_type", "governed")
            normalized_step_type = _optional_string(step_type)
            if normalized_step_type not in SUPPORTED_STEP_TYPES:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_step_type_value",
                        message=(
                            "Skill step step_type must be governed, predicated, "
                            "uses_skill, invoke_tool, gate, or coding_loop."
                        ),
                        path=_child_path(step_path, "step_type"),
                    )
                )

            pre_step = step_mapping.get("pre_step")
            if pre_step is not None and not isinstance(pre_step, Mapping):
                issues.append(
                    SkillValidationIssue(
                        code="invalid_pre_step_type",
                        message="Skill step pre_step must be an object.",
                        path=_child_path(step_path, "pre_step"),
                    )
                )
            elif normalized_step_type in {
                "governed",
                "predicated",
                "uses_skill",
                "invoke_tool",
                "gate",
            } and isinstance(pre_step, Mapping):
                _validate_gather_context_pre_step(pre_step, step_path, issues)
                if pre_step.get("action") == "gather_context":
                    outputs = step_mapping.get("outputs")
                    if (
                        not isinstance(outputs, Sequence)
                        or isinstance(outputs, (str, bytes, bytearray))
                        or not outputs
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="missing_pre_step_outputs",
                                message=(
                                    "gather_context invoke_tool steps must declare "
                                    "at least one output."
                                ),
                                path=_child_path(step_path, "outputs"),
                            )
                        )
            elif pre_step is not None:
                issues.append(
                    SkillValidationIssue(
                        code="unexpected_pre_step",
                        message="Skill step pre_step must be an object.",
                        path=_child_path(step_path, "pre_step"),
                    )
                )

            raw_tool_invocations = step_mapping.get("tool_invocations")
            if normalized_step_type in {"invoke_tool", "gate"}:
                if pre_step is None:
                    issues.append(
                        SkillValidationIssue(
                            code="missing_pre_step",
                            message=(
                                "invoke_tool and gate steps must declare an "
                                "invoke_tool or "
                                "gather_context pre_step."
                            ),
                            path=_child_path(step_path, "pre_step"),
                        )
                    )
                if raw_tool_invocations:
                    issues.append(
                        SkillValidationIssue(
                            code="unexpected_tool_invocations",
                            message=(
                                "invoke_tool and gate steps must use pre_step "
                                "instead of "
                                "tool_invocations."
                            ),
                            path=_child_path(step_path, "tool_invocations"),
                        )
                    )

            raw_gate = step_mapping.get("gate")
            if normalized_step_type == "gate":
                if not isinstance(raw_gate, Mapping):
                    issues.append(
                        SkillValidationIssue(
                            code="missing_gate",
                            message="gate steps must declare a gate object.",
                            path=_child_path(step_path, "gate"),
                        )
                    )
                else:
                    outcome = raw_gate.get("outcome")
                    outcome_path = (
                        outcome.get("path") if isinstance(outcome, Mapping) else None
                    )
                    if (
                        not isinstance(outcome, Mapping)
                        or not isinstance(outcome_path, str)
                        or not outcome_path.strip()
                        or "equals" not in outcome
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_gate_outcome",
                                message="gate outcome must declare path and equals.",
                                path=_child_path(step_path, "gate.outcome"),
                            )
                        )
                    if _optional_string(raw_gate.get("goto_step")) is None:
                        issues.append(
                            SkillValidationIssue(
                                code="missing_gate_goto_step",
                                message="gate must declare a non-empty goto_step.",
                                path=_child_path(step_path, "gate.goto_step"),
                            )
                        )
                    if _optional_string(raw_gate.get("retry_context")) is None:
                        issues.append(
                            SkillValidationIssue(
                                code="missing_gate_retry_context",
                                message="gate must declare non-empty retry_context.",
                                path=_child_path(step_path, "gate.retry_context"),
                            )
                        )
                    if (
                        "success_goto_step" in raw_gate
                        and _optional_string(raw_gate.get("success_goto_step")) is None
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_gate_success_goto_step",
                                message=(
                                    "gate success_goto_step must be a non-empty "
                                    "step id when provided."
                                ),
                                path=_child_path(step_path, "gate.success_goto_step"),
                            )
                        )
            elif raw_gate is not None:
                issues.append(
                    SkillValidationIssue(
                        code="unexpected_gate",
                        message="Only gate steps may declare gate.",
                        path=_child_path(step_path, "gate"),
                    )
                )
            raw_coding_loop = step_mapping.get("coding_loop")
            if normalized_step_type == "coding_loop":
                _validate_coding_loop(raw_coding_loop, step_path, issues)
            elif raw_coding_loop is not None:
                issues.append(
                    SkillValidationIssue(
                        code="unexpected_coding_loop",
                        message="Only coding_loop steps may declare coding_loop.",
                        path=_child_path(step_path, "coding_loop"),
                    )
                )
            raw_completion = step_mapping.get("completion")
            if normalized_step_type == "predicated":
                if not isinstance(raw_completion, Mapping):
                    issues.append(
                        SkillValidationIssue(
                            code="missing_completion",
                            message=(
                                "predicated steps must declare a completion object."
                            ),
                            path=_child_path(step_path, "completion"),
                        )
                    )
                else:
                    unsupported_completion_fields = set(raw_completion) - {
                        "required_outputs",
                        "required_actions",
                    }
                    if unsupported_completion_fields:
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_completion",
                                message=(
                                    "predicated completion contains unsupported "
                                    "fields: "
                                    + ", ".join(
                                        sorted(
                                            str(item)
                                            for item in unsupported_completion_fields
                                        )
                                    )
                                ),
                                path=_child_path(step_path, "completion"),
                            )
                        )
                    required_outputs = raw_completion.get("required_outputs")
                    required_output_names = (
                        list(required_outputs)
                        if isinstance(required_outputs, Sequence)
                        and not isinstance(required_outputs, (str, bytes, bytearray))
                        else []
                    )
                    if (
                        not required_output_names
                        or any(
                            _optional_string(item) is None
                            for item in required_output_names
                        )
                        or len(
                            {_optional_string(item) for item in required_output_names}
                        )
                        != len(required_output_names)
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_completion",
                                message=(
                                    "predicated completion.required_outputs must be a "
                                    "non-empty array of unique strings."
                                ),
                                path=_child_path(
                                    step_path, "completion.required_outputs"
                                ),
                            )
                        )
                    else:
                        raw_outputs = step_mapping.get("outputs")
                        declared_output_names = (
                            {
                                _optional_string(item.get("name"))
                                for item in raw_outputs
                                if isinstance(item, Mapping)
                            }
                            if isinstance(raw_outputs, Sequence)
                            and not isinstance(raw_outputs, (str, bytes, bytearray))
                            else set()
                        )
                        missing_outputs = sorted(
                            name
                            for name in required_output_names
                            if name not in declared_output_names
                        )
                        if missing_outputs:
                            issues.append(
                                SkillValidationIssue(
                                    code="invalid_completion",
                                    message=(
                                        "predicated completion references undeclared "
                                        "outputs: " + ", ".join(missing_outputs)
                                    ),
                                    path=_child_path(
                                        step_path, "completion.required_outputs"
                                    ),
                                )
                            )
                    raw_required_actions = raw_completion.get("required_actions", [])
                    if not isinstance(raw_required_actions, Sequence) or isinstance(
                        raw_required_actions, (str, bytes, bytearray)
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_completion",
                                message=(
                                    "predicated completion.required_actions must be "
                                    "an array."
                                ),
                                path=_child_path(
                                    step_path, "completion.required_actions"
                                ),
                            )
                        )
                    else:
                        for action_index, required_action in enumerate(
                            raw_required_actions
                        ):
                            if (
                                not isinstance(required_action, Mapping)
                                or _optional_string(required_action.get("action"))
                                is None
                                or (
                                    ("targets_from" in required_action)
                                    != ("match_field" in required_action)
                                )
                                or any(
                                    _optional_string(required_action.get(field_name))
                                    is None
                                    for field_name in ("targets_from", "match_field")
                                    if field_name in required_action
                                )
                                or (
                                    "exactly" in required_action
                                    and (
                                        isinstance(required_action["exactly"], bool)
                                        or not isinstance(
                                            required_action["exactly"], int
                                        )
                                        or required_action["exactly"] < 1
                                    )
                                )
                                or (
                                    "parameters" in required_action
                                    and not isinstance(
                                        required_action["parameters"], Mapping
                                    )
                                )
                            ):
                                issues.append(
                                    SkillValidationIssue(
                                        code="invalid_completion",
                                        message=(
                                            "predicated required_actions entries must "
                                            "declare action, or both targets_from "
                                            "and match_field strings, with optional "
                                            "positive exactly and object parameters."
                                        ),
                                        path=_child_path(
                                            step_path,
                                            f"completion.required_actions[{action_index}]",
                                        ),
                                    )
                                )
            elif raw_completion is not None:
                issues.append(
                    SkillValidationIssue(
                        code="unexpected_completion",
                        message="Only predicated steps may declare completion.",
                        path=_child_path(step_path, "completion"),
                    )
                )
            raw_validation_gate = step_mapping.get("validation_gate")
            if raw_validation_gate is not None:
                if not isinstance(raw_validation_gate, Mapping):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_validation_gate",
                            message="validation_gate must be an object.",
                            path=_child_path(step_path, "validation_gate"),
                        )
                    )
                else:
                    gate_id = raw_validation_gate.get("id")
                    discovery = raw_validation_gate.get("discovery")
                    if not isinstance(gate_id, str) or not gate_id.strip():
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_validation_gate_id",
                                message="validation_gate.id must be non-empty.",
                                path=_child_path(step_path, "validation_gate.id"),
                            )
                        )
                    discovery_handoff = (
                        discovery.get("input_ref")
                        if isinstance(discovery, Mapping)
                        else None
                    )
                    if not isinstance(discovery, Mapping) or (
                        not isinstance(discovery.get("action"), Mapping)
                        and not isinstance(discovery_handoff, str)
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_validation_gate_discovery",
                                message=(
                                    "validation_gate.discovery must declare an action "
                                    "or input_ref."
                                ),
                                path=_child_path(
                                    step_path, "validation_gate.discovery"
                                ),
                            )
                        )
                    if not isinstance(raw_validation_gate.get("obligations"), Mapping):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_validation_gate_obligations",
                                message=(
                                    "validation_gate.obligations must be an object."
                                ),
                                path=_child_path(
                                    step_path, "validation_gate.obligations"
                                ),
                            )
                        )
                    elif not isinstance(
                        raw_validation_gate["obligations"].get("action"), str
                    ) or not isinstance(
                        raw_validation_gate["obligations"].get("id"), str
                    ):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_validation_gate_action_field",
                                message=(
                                    "validation_gate.obligations must declare "
                                    "string id and action projections."
                                ),
                                path=_child_path(
                                    step_path,
                                    "validation_gate.obligations",
                                ),
                            )
                        )

            raw_uses_skill = step_mapping.get("uses_skill")
            if normalized_step_type == "uses_skill":
                if not isinstance(raw_uses_skill, Mapping):
                    issues.append(
                        SkillValidationIssue(
                            code="missing_uses_skill",
                            message=(
                                "uses_skill steps must declare a uses_skill object."
                            ),
                            path=_child_path(step_path, "uses_skill"),
                        )
                    )
                elif (
                    not isinstance(raw_uses_skill.get("skill"), str)
                    or not raw_uses_skill.get("skill", "").strip()
                ):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_uses_skill",
                            message="uses_skill.skill must be a non-empty string.",
                            path=_child_path(step_path, "uses_skill.skill"),
                        )
                    )
            elif raw_uses_skill is not None:
                issues.append(
                    SkillValidationIssue(
                        code="unexpected_uses_skill",
                        message="Only uses_skill steps may declare uses_skill.",
                        path=_child_path(step_path, "uses_skill"),
                    )
                )
            if normalized_step_type == "uses_skill":
                if step_mapping.get("actions") not in (None, []):
                    issues.append(
                        SkillValidationIssue(
                            code="uses_skill_actions",
                            message="uses_skill steps cannot declare model actions.",
                            path=_child_path(step_path, "actions"),
                        )
                    )
                if step_mapping.get("prompt_catalogs"):
                    issues.append(
                        SkillValidationIssue(
                            code="uses_skill_prompt_catalogs",
                            message="uses_skill steps cannot declare prompt_catalogs.",
                            path=_child_path(step_path, "prompt_catalogs"),
                        )
                    )
                if raw_uses_skill is not None:
                    try:
                        _parse_uses_skill(raw_uses_skill)
                    except ValueError as exc:
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_uses_skill",
                                message=str(exc),
                                path=_child_path(step_path, "uses_skill"),
                            )
                        )

            prompt_catalogs = step_mapping.get("prompt_catalogs")
            if prompt_catalogs is not None:
                if not isinstance(prompt_catalogs, Sequence) or isinstance(
                    prompt_catalogs,
                    (str, bytes, bytearray),
                ):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_prompt_catalogs_type",
                            message="Skill step prompt_catalogs must be an array.",
                            path=_child_path(step_path, "prompt_catalogs"),
                        )
                    )
                else:
                    if not prompt_catalogs:
                        issues.append(
                            SkillValidationIssue(
                                code="empty_prompt_catalogs",
                                message=(
                                    "Skill step prompt_catalogs must omit the field "
                                    "when no prompt catalog is needed."
                                ),
                                path=_child_path(step_path, "prompt_catalogs"),
                            )
                        )
                    seen_catalogs: set[str] = set()
                    for catalog_index, catalog_value in enumerate(prompt_catalogs):
                        normalized_catalog = _optional_string(catalog_value)
                        catalog_path = _sequence_path(
                            step_path, "prompt_catalogs", catalog_index
                        )
                        if normalized_catalog is None:
                            issues.append(
                                SkillValidationIssue(
                                    code="invalid_prompt_catalog",
                                    message=(
                                        "Skill step prompt_catalogs must contain "
                                        "non-empty strings."
                                    ),
                                    path=catalog_path,
                                )
                            )
                        elif normalized_catalog not in SUPPORTED_PROMPT_CATALOGS:
                            issues.append(
                                SkillValidationIssue(
                                    code="unsupported_prompt_catalog",
                                    message=(
                                        "Skill step prompt_catalogs currently "
                                        "support context_types, skills, and actions."
                                    ),
                                    path=catalog_path,
                                )
                            )
                        elif normalized_catalog in seen_catalogs:
                            issues.append(
                                SkillValidationIssue(
                                    code="duplicate_prompt_catalog",
                                    message=(
                                        "Skill step prompt_catalogs must not contain "
                                        "duplicates."
                                    ),
                                    path=catalog_path,
                                )
                            )
                        else:
                            seen_catalogs.add(normalized_catalog)

            _validate_step_actions(step_mapping.get("actions"), step_path, issues)

            _validate_step_contracts(step_mapping, step_path, issues)

            packages = step_mapping.get("tool_invocation_packages")
            if packages is not None:
                if not isinstance(packages, Sequence) or isinstance(
                    packages, (str, bytes, bytearray)
                ):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_tool_invocation_packages_type",
                            message=(
                                "Skill step tool_invocation_packages must be an array."
                            ),
                            path=_child_path(step_path, "tool_invocation_packages"),
                        )
                    )
                else:
                    seen_packages: set[str] = set()
                    for package_index, package in enumerate(packages):
                        package_path = _sequence_path(
                            step_path, "tool_invocation_packages", package_index
                        )
                        normalized_package = _optional_string(package)
                        if normalized_package is None:
                            issues.append(
                                SkillValidationIssue(
                                    code="invalid_tool_invocation_package",
                                    message=(
                                        "Tool invocation packages must be non-empty "
                                        "strings."
                                    ),
                                    path=package_path,
                                )
                            )
                        elif (
                            normalized_package not in SUPPORTED_TOOL_INVOCATION_PACKAGES
                        ):
                            issues.append(
                                SkillValidationIssue(
                                    code="unsupported_tool_invocation_package",
                                    message=(
                                        "Supported tool invocation packages are: "
                                        + ", ".join(
                                            sorted(SUPPORTED_TOOL_INVOCATION_PACKAGES)
                                        )
                                        + "."
                                    ),
                                    path=package_path,
                                )
                            )
                        elif normalized_package in seen_packages:
                            issues.append(
                                SkillValidationIssue(
                                    code="duplicate_tool_invocation_package",
                                    message=(
                                        "Tool invocation packages must not contain "
                                        "duplicates."
                                    ),
                                    path=package_path,
                                )
                            )
                        else:
                            seen_packages.add(normalized_package)

            tool_invocations = step_mapping.get("tool_invocations")
            if tool_invocations is None and not packages:
                continue
            if tool_invocations is None:
                continue
            if not isinstance(tool_invocations, Sequence) or isinstance(
                tool_invocations,
                (str, bytes, bytearray),
            ):
                issues.append(
                    SkillValidationIssue(
                        code="invalid_tool_invocations_type",
                        message="Skill step tool_invocations must be an array.",
                        path=_child_path(step_path, "tool_invocations"),
                    )
                )
                continue
            if len(tool_invocations) == 0:
                issues.append(
                    SkillValidationIssue(
                        code="missing_tool_invocations",
                        message=(
                            "Skill steps with tool_invocations must include at "
                            "least one tool invocation."
                        ),
                        path=_child_path(step_path, "tool_invocations"),
                    )
                )
                continue
            for tool_index, tool_invocation in enumerate(tool_invocations):
                tool_path = _sequence_path(step_path, "tool_invocations", tool_index)
                if not isinstance(tool_invocation, Mapping):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_tool_invocation_type",
                            message="Skill tool invocations must be objects.",
                            path=tool_path,
                        )
                    )
                    continue
                tool_mapping = cast("Mapping[str, Any]", tool_invocation)
                _validate_unknown_keys(
                    tool_mapping,
                    {"tool", "command", "operation", "label", "cwd", "env"},
                    issues,
                    path=tool_path or "",
                    subject="skill tool invocation",
                )

                tool = _optional_string(tool_mapping.get("tool"))
                if tool is None:
                    issues.append(
                        SkillValidationIssue(
                            code="missing_tool",
                            message="Skill tool invocations must include a tool name.",
                            path=_child_path(tool_path, "tool"),
                        )
                    )
                elif tool not in SUPPORTED_SKILL_TOOL_TYPES:
                    issues.append(
                        SkillValidationIssue(
                            code="unsupported_tool",
                            message=(
                                "Skill tool invocations currently support "
                                f"{', '.join(sorted(SUPPORTED_SKILL_TOOL_TYPES))}."
                            ),
                            path=_child_path(tool_path, "tool"),
                        )
                    )

                command = tool_mapping.get("command")
                operation = _optional_string(tool_mapping.get("operation"))
                label = tool_mapping.get("label")
                if tool == "ref":
                    if _optional_string(label) is None:
                        issues.append(
                            SkillValidationIssue(
                                code="missing_tool_label",
                                message="Tool references must include a label.",
                                path=_child_path(tool_path, "label"),
                            )
                        )
                    if command is not None:
                        issues.append(
                            SkillValidationIssue(
                                code="unexpected_tool_command",
                                message="Tool references must not include a command.",
                                path=_child_path(tool_path, "command"),
                            )
                        )
                    continue
                if operation is not None:
                    if command is not None:
                        issues.append(
                            SkillValidationIssue(
                                code="unexpected_tool_command",
                                message=(
                                    "Structured tool invocations must not include "
                                    "command."
                                ),
                                path=_child_path(tool_path, "command"),
                            )
                        )
                elif not isinstance(command, Sequence) or isinstance(
                    command,
                    (str, bytes, bytearray),
                ):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_tool_command_type",
                            message="Skill tool invocation command must be an array.",
                            path=_child_path(tool_path, "command"),
                        )
                    )
                else:
                    for arg_index, arg in enumerate(command):
                        if _optional_string(arg) is None:
                            issues.append(
                                SkillValidationIssue(
                                    code="invalid_tool_command_item",
                                    message=(
                                        "Skill tool invocation command items must be "
                                        "non-empty strings."
                                    ),
                                    path=_sequence_path(
                                        tool_path,
                                        "command",
                                        arg_index,
                                    ),
                                )
                            )
                if tool == "internal" and (
                    not isinstance(command, Sequence)
                    or isinstance(command, (str, bytes, bytearray))
                    or not command
                    or command[0] != "powdrr-lift"
                ):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_internal_command",
                            message=(
                                "Internal tool commands must invoke the "
                                "powdrr-lift binary."
                            ),
                            path=_child_path(tool_path, "command"),
                        )
                    )

                cwd = tool_mapping.get("cwd")
                if cwd is not None and _optional_string(cwd) is None:
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_tool_cwd",
                            message=(
                                "Skill tool invocation cwd must be a non-empty string."
                            ),
                            path=_child_path(tool_path, "cwd"),
                        )
                    )

                env = tool_mapping.get("env")
                if env is not None:
                    if not isinstance(env, Mapping):
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_tool_env_type",
                                message="Skill tool invocation env must be an object.",
                                path=_child_path(tool_path, "env"),
                            )
                        )
                    else:
                        for env_key, env_value in env.items():
                            if _optional_string(env_key) is None:
                                issues.append(
                                    SkillValidationIssue(
                                        code="invalid_tool_env_key",
                                        message=(
                                            "Skill tool invocation env keys must be "
                                            "non-empty strings."
                                        ),
                                        path=_child_path(tool_path, "env"),
                                    )
                                )
                            if _optional_string(env_value) is None:
                                issues.append(
                                    SkillValidationIssue(
                                        code="invalid_tool_env_value",
                                        message=(
                                            "Skill tool invocation env values must be "
                                            "non-empty strings."
                                        ),
                                        path=_child_path(tool_path, "env"),
                                    )
                                )

    skill_names = [name] if name is not None else []
    return SkillValidationReport(
        validation_successful=not issues,
        skill_names=skill_names,
        skill_paths=_skill_paths_list(source_path),
        issues=issues,
    )


def build_skill_directory_validation_report(
    directory: str | Path,
) -> SkillValidationReport:
    directory_path = Path(directory)
    if not directory_path.exists():
        return SkillValidationReport(
            validation_successful=False,
            issues=[
                SkillValidationIssue(
                    code="missing_directory",
                    message=f"Skill directory does not exist: {directory_path}",
                    path=str(directory_path),
                )
            ],
        )
    if not directory_path.is_dir():
        return SkillValidationReport(
            validation_successful=False,
            issues=[
                SkillValidationIssue(
                    code="not_a_directory",
                    message=f"Skill path is not a directory: {directory_path}",
                    path=str(directory_path),
                )
            ],
        )

    contents, issues = _load_skill_directory_contents(directory_path)
    issues.extend(_validate_skill_references(contents))
    issues.extend(_validate_skill_dependency_cycles(contents))

    return SkillValidationReport(
        validation_successful=not issues,
        skill_names=contents.skill_names,
        skill_paths=contents.skill_paths,
        issues=issues,
    )


def _load_skill_directory_contents(
    directory_path: Path,
) -> tuple[_SkillDirectoryContents, list[SkillValidationIssue]]:
    issues: list[SkillValidationIssue] = []
    skill_names: list[str] = []
    skill_paths: list[str] = []
    skills_by_name: dict[str, Skill] = {}
    skill_paths_by_name: dict[str, Path] = {}
    step_references: list[tuple[Path, Skill]] = []

    discovered_skill_paths = sorted(
        path
        for pattern in ("*.yaml", "*.yml", "*.json")
        for path in directory_path.glob(pattern)
        if path.is_file()
    )
    for skill_path in discovered_skill_paths:
        skill_paths.append(str(skill_path))
        raw_content = skill_path.read_text(encoding="utf-8")
        file_report = build_skill_validation_report(raw_content, source_path=skill_path)
        issues.extend(file_report.issues)
        if not file_report.validation_successful or not file_report.skill_names:
            continue

        skill = load_skill(skill_path)
        skill_name = skill.name
        step_references.append((skill_path, skill))
        if skill_name in skills_by_name:
            issues.append(
                SkillValidationIssue(
                    code="duplicate_skill_name",
                    message=(
                        f"Skill name {skill_name!r} appears in both "
                        f"{skill_paths_by_name[skill_name]} and {skill_path}."
                    ),
                    path=str(skill_path),
                )
            )
        else:
            skills_by_name[skill_name] = skill
            skill_paths_by_name[skill_name] = skill_path
            skill_names.append(skill_name)

    return (
        _SkillDirectoryContents(
            skill_names=skill_names,
            skill_paths=skill_paths,
            skills_by_name=skills_by_name,
            skill_paths_by_name=skill_paths_by_name,
            step_references=step_references,
        ),
        issues,
    )


def _validate_skill_references(
    contents: _SkillDirectoryContents,
) -> list[SkillValidationIssue]:
    issues: list[SkillValidationIssue] = []
    for skill_path, skill in contents.step_references:
        for step_index, step in enumerate(skill.steps):
            if step.uses_skill is None:
                continue
            referenced_skill = step.uses_skill.skill
            reference_path = _child_path(
                _sequence_path(skill_path, "steps", step_index),
                "uses_skill.skill",
            )
            if referenced_skill == skill.name:
                issues.append(
                    SkillValidationIssue(
                        code="self_dependency",
                        message=(
                            f"Skill {skill.name!r} cannot reference itself from a step."
                        ),
                        path=reference_path,
                    )
                )
            elif referenced_skill not in contents.skills_by_name:
                issues.append(
                    SkillValidationIssue(
                        code="missing_skill_reference",
                        message=(
                            f"Skill {skill.name!r} references unknown skill "
                            f"{referenced_skill!r}."
                        ),
                        path=reference_path,
                    )
                )
    return issues


def _validate_skill_dependency_cycles(
    contents: _SkillDirectoryContents,
) -> list[SkillValidationIssue]:
    dependency_graph = {
        skill_name: {
            referenced_skill
            for step in skill.steps
            for referenced_skill in (
                [step.uses_skill.skill] if step.uses_skill is not None else []
            )
            if referenced_skill in contents.skills_by_name
        }
        for skill_name, skill in contents.skills_by_name.items()
    }
    cyclic_skills: set[str] = set()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(skill_name: str) -> None:
        if skill_name in visiting:
            cyclic_skills.add(skill_name)
            return
        if skill_name in visited:
            return
        visiting.add(skill_name)
        for dependency_name in dependency_graph.get(skill_name, set()):
            visit(dependency_name)
            if dependency_name in cyclic_skills:
                cyclic_skills.add(skill_name)
        visiting.remove(skill_name)
        visited.add(skill_name)

    for skill_name in dependency_graph:
        visit(skill_name)
    return [
        SkillValidationIssue(
            code="cyclic_skill_dependency",
            message=(
                f"Skill dependency graph contains a cycle involving {skill_name!r}."
            ),
            path=str(contents.skill_paths_by_name[skill_name]),
        )
        for skill_name in sorted(cyclic_skills)
    ]


def validate_skill_json(json_content: str) -> str:
    return (
        json.dumps(
            _report_to_data(build_skill_validation_report(json_content)),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def validate_skill_json_file(path: str | Path) -> str:
    return validate_skill_json(Path(path).read_text(encoding="utf-8"))


def validate_skill_directory(directory: str | Path) -> str:
    return (
        json.dumps(
            _report_to_data(build_skill_directory_validation_report(directory)),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


validate_skill_directory_json = validate_skill_directory


def _parse_steps(raw_steps: object) -> tuple[SkillStep, ...]:
    if not isinstance(raw_steps, Sequence) or isinstance(
        raw_steps,
        (str, bytes, bytearray),
    ):
        raise ValueError("Skill steps must be an array.")
    return tuple(_parse_step(raw_step) for raw_step in raw_steps)


def _validate_step_contracts(
    step_mapping: Mapping[str, Any],
    step_path: str,
    issues: list[SkillValidationIssue],
) -> None:
    for field_name, item_keys in (
        ("inputs", {"name", "type", "required", "source"}),
        (
            "outputs",
            {"name", "type", "required_for_next_step", "scope", "schema"},
        ),
    ):
        value = step_mapping.get(field_name)
        if value is None:
            continue
        field_path = _child_path(step_path, field_name)
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            issues.append(
                SkillValidationIssue(
                    code=f"invalid_{field_name}_type",
                    message=f"Skill step {field_name} must be an array.",
                    path=field_path,
                )
            )
            continue
        seen_names: set[str] = set()
        for index, item in enumerate(value):
            item_path = _sequence_path(step_path, field_name, index)
            if not isinstance(item, Mapping):
                issues.append(
                    SkillValidationIssue(
                        code=f"invalid_{field_name[:-1]}_type",
                        message=f"Skill step {field_name} must contain objects.",
                        path=item_path,
                    )
                )
                continue
            _validate_unknown_keys(
                item,
                item_keys,
                issues,
                path=item_path,
                subject=f"skill step {field_name[:-1]}",
            )
            name = _optional_string(item.get("name"))
            if name is None:
                issues.append(
                    SkillValidationIssue(
                        code=f"missing_{field_name[:-1]}_name",
                        message=f"Skill step {field_name} must include a name.",
                        path=_child_path(item_path, "name"),
                    )
                )
            elif name in seen_names:
                issues.append(
                    SkillValidationIssue(
                        code=f"duplicate_{field_name[:-1]}_name",
                        message=(
                            f"Skill step {field_name} names must be unique; "
                            f"{name!r} is repeated."
                        ),
                        path=_child_path(item_path, "name"),
                    )
                )
            else:
                seen_names.add(name)
            if (
                item.get("type") is not None
                and _optional_string(item.get("type")) is None
            ):
                issues.append(
                    SkillValidationIssue(
                        code=f"invalid_{field_name[:-1]}_type_name",
                        message=(
                            f"Skill step {field_name} type must be a non-empty string."
                        ),
                        path=_child_path(item_path, "type"),
                    )
                )
            boolean_key = (
                "required" if field_name == "inputs" else "required_for_next_step"
            )
            if boolean_key in item and not isinstance(item[boolean_key], bool):
                issues.append(
                    SkillValidationIssue(
                        code=f"invalid_{boolean_key}",
                        message=f"Skill step {boolean_key} must be a boolean.",
                        path=_child_path(item_path, boolean_key),
                    )
                )
            if field_name == "outputs" and "schema" in item:
                schema = item["schema"]
                if not isinstance(schema, Mapping):
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_output_schema",
                            message="Skill step output schema must be an object.",
                            path=_child_path(item_path, "schema"),
                        )
                    )
                elif schema.get("type") is None:
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_output_schema",
                            message="Skill step output schema must declare a type.",
                            path=_child_path(item_path, "schema.type"),
                        )
                    )
                else:
                    schema_error = _output_schema_declaration_error(schema)
                    if schema_error is not None:
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_output_schema",
                                message=schema_error,
                                path=_child_path(item_path, "schema"),
                            )
                        )


def _validate_coding_loop(
    value: object,
    step_path: str,
    issues: list[SkillValidationIssue],
) -> None:
    if not isinstance(value, Mapping):
        issues.append(
            SkillValidationIssue(
                code="invalid_coding_loop",
                message="coding_loop must be an object.",
                path=_child_path(step_path, "coding_loop"),
            )
        )
        return
    goal = value.get("goal")
    if _optional_string(goal) is None:
        issues.append(
            SkillValidationIssue(
                code="missing_coding_loop_goal",
                message="coding_loop.goal must be a non-empty string.",
                path=_child_path(step_path, "coding_loop.goal"),
            )
        )
    verification = value.get("verification", [])
    if (
        isinstance(verification, Sequence)
        and not isinstance(verification, (str, bytes, bytearray))
        and not verification
    ):
        issues.append(
            SkillValidationIssue(
                code="missing_coding_loop_verification",
                message="coding_loop.verification must contain at least one command.",
                path=_child_path(step_path, "coding_loop.verification"),
            )
        )
    if (
        not isinstance(verification, Sequence)
        or isinstance(verification, (str, bytes, bytearray))
        or any(
            not isinstance(item, Mapping)
            or _optional_string(item.get("id")) is None
            or _command_value(item.get("command")) is None
            for item in verification
        )
    ):
        issues.append(
            SkillValidationIssue(
                code="invalid_coding_loop_verification",
                message=(
                    "coding_loop.verification must be an array of objects with "
                    "non-empty id and command fields."
                ),
                path=_child_path(step_path, "coding_loop.verification"),
            )
        )
    stopping_conditions = value.get("stopping_conditions", [])
    if (
        not isinstance(stopping_conditions, Sequence)
        or isinstance(stopping_conditions, (str, bytes, bytearray))
        or any(_optional_string(item) is None for item in stopping_conditions)
    ):
        issues.append(
            SkillValidationIssue(
                code="invalid_coding_loop_stopping_conditions",
                message=(
                    "coding_loop.stopping_conditions must be an array of strings."
                ),
                path=_child_path(step_path, "coding_loop.stopping_conditions"),
            )
        )
    max_iterations = value.get("max_iterations", 12)
    if (
        not isinstance(max_iterations, int)
        or isinstance(max_iterations, bool)
        or not (1 <= max_iterations <= 100)
    ):
        issues.append(
            SkillValidationIssue(
                code="invalid_coding_loop_max_iterations",
                message="coding_loop.max_iterations must be an integer from 1 to 100.",
                path=_child_path(step_path, "coding_loop.max_iterations"),
            )
        )


def _parse_step(raw_step: object) -> SkillStep:
    if not isinstance(raw_step, Mapping):
        raise ValueError("Skill steps must be objects.")
    return skill_step_from_data(cast("Mapping[str, Any]", raw_step))


def skill_step_from_data(data: Mapping[str, Any]) -> SkillStep:
    """Parse the reusable executable-step shape used by skills and workflows."""
    step_id = _optional_string(data.get("id"))
    description = _required_string(data, "description")
    step_type = _optional_string(data.get("step_type")) or "governed"
    if step_type == "freeform":
        step_type = "governed"
    if step_type not in SUPPORTED_STEP_TYPES:
        raise ValueError(
            "Skill step step_type must be governed, predicated, uses_skill, "
            "invoke_tool, gate, or coding_loop."
        )
    details = _optional_string(data.get("details"))
    llm_type = _optional_string(data.get("llm_type"))
    raw_interaction_style = data.get("interaction_style")
    interaction_style = _optional_interaction_style(raw_interaction_style)
    if raw_interaction_style is not None and interaction_style is None:
        raise ValueError(
            "Skill step interaction_style must be one of: "
            + ", ".join(sorted(SUPPORTED_INTERACTION_STYLES))
            + "."
        )
    tool_invocations = _optional_tool_invocations(
        data.get("tool_invocations"),
    )
    tool_invocation_packages = _optional_tool_invocation_packages(
        data.get("tool_invocation_packages")
    )
    tool_invocations = _merge_tool_invocations(
        tool_invocations, tool_invocation_packages
    )
    prompt_catalogs = _optional_prompt_catalogs(data.get("prompt_catalogs"))
    actions = _optional_step_actions(data.get("actions"))
    actions_declared = "actions" in data
    next_step_override = _optional_string(data.get("next_step_override"))
    if data.get("next_step_override") is not None and next_step_override is None:
        raise ValueError("Skill step next_step_override must be a non-empty string.")
    pre_step = _parse_pre_step(data.get("pre_step"))
    gate = _parse_gate(data.get("gate"))
    raw_coding_loop = data.get("coding_loop")
    coding_loop = (
        _parse_coding_loop(raw_coding_loop) if raw_coding_loop is not None else None
    )
    raw_completion = data.get("completion")
    completion = _parse_step_completion(raw_completion)
    uses_skill = _parse_uses_skill(data.get("uses_skill"))
    raw_validation_gate = data.get("validation_gate")
    if raw_validation_gate is not None and not isinstance(raw_validation_gate, Mapping):
        raise ValueError("Skill step validation_gate must be an object.")
    validation_gate = (
        dict(cast("Mapping[str, Any]", raw_validation_gate))
        if raw_validation_gate is not None
        else None
    )
    if validation_gate is not None:
        gate_id = validation_gate.get("id")
        discovery = validation_gate.get("discovery")
        if not isinstance(gate_id, str) or not gate_id.strip():
            raise ValueError("Skill step validation_gate.id must be non-empty.")
        discovery_handoff = (
            discovery.get("input_ref") if isinstance(discovery, Mapping) else None
        )
        if not isinstance(discovery, Mapping) or (
            not isinstance(discovery.get("action"), Mapping)
            and not isinstance(discovery_handoff, str)
        ):
            raise ValueError(
                "Skill step validation_gate.discovery must declare an action "
                "or input_ref."
            )
        if not isinstance(validation_gate.get("obligations"), Mapping):
            raise ValueError(
                "Skill step validation_gate.obligations must be an object."
            )
        obligation_id = validation_gate["obligations"].get("id")
        action_field = validation_gate["obligations"].get("action")
        if (
            not isinstance(obligation_id, str)
            or not obligation_id.strip()
            or not isinstance(action_field, str)
            or not action_field.strip()
        ):
            raise ValueError(
                "Skill step validation_gate.obligations.action_field must be non-empty."
            )
    if step_type in {"invoke_tool", "gate"}:
        if pre_step is None:
            raise ValueError(
                "invoke_tool and gate steps must declare an invoke_tool or "
                "gather_context pre_step."
            )
        if tool_invocations:
            raise ValueError(
                "invoke_tool steps must use pre_step instead of tool_invocations."
            )
    # Freeform steps may use a deterministic pre-step when the result controls
    # whether the model performs optional work.  The runtime executes it before
    # prompting, just as it does for invoke_tool steps.
    if step_type == "gate" and gate is None:
        raise ValueError("gate steps must declare a gate object.")
    if step_type != "gate" and gate is not None:
        raise ValueError("Only gate steps may declare gate.")
    if step_type == "coding_loop" and coding_loop is None:
        raise ValueError("coding_loop steps must declare a coding_loop object.")
    if step_type != "coding_loop" and coding_loop is not None:
        raise ValueError("Only coding_loop steps may declare coding_loop.")
    if step_type == "predicated" and completion is None:
        raise ValueError("predicated steps must declare a completion object.")
    if step_type != "predicated" and completion is not None:
        raise ValueError("Only predicated steps may declare completion.")
    if step_type == "uses_skill" and uses_skill is None:
        raise ValueError("uses_skill steps must declare a uses_skill object.")
    if step_type != "uses_skill" and uses_skill is not None:
        raise ValueError("Only uses_skill steps may declare uses_skill.")
    if step_type == "uses_skill":
        if pre_step is not None:
            raise ValueError("uses_skill steps cannot declare pre_step.")
        if actions_declared and actions:
            raise ValueError("uses_skill steps cannot declare model actions.")
        if prompt_catalogs:
            raise ValueError("uses_skill steps cannot declare prompt_catalogs.")
    inputs = _parse_step_inputs(data.get("inputs"))
    outputs = _parse_step_outputs(data.get("outputs"))
    if completion is not None:
        declared_outputs = {output.name for output in outputs}
        missing_outputs = sorted(
            name for name in completion.required_outputs if name not in declared_outputs
        )
        if missing_outputs:
            raise ValueError(
                "predicated completion references undeclared outputs: "
                + ", ".join(missing_outputs)
            )
    return SkillStep(
        id=step_id,
        description=description,
        step_type=step_type,
        details=details,
        llm_type=llm_type,
        interaction_style=interaction_style,
        tool_invocations=tool_invocations,
        tool_invocation_packages=tool_invocation_packages,
        prompt_catalogs=prompt_catalogs,
        actions=actions,
        actions_declared=actions_declared,
        next_step_override=next_step_override,
        pre_step=pre_step,
        gate=gate,
        validation_gate=validation_gate,
        coding_loop=coding_loop,
        completion=completion,
        uses_skill=uses_skill,
        inputs=inputs,
        outputs=outputs,
    )


def _parse_uses_skill(value: object) -> SkillUsesSkill | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Skill step uses_skill must be an object.")
    unknown = set(value) - {"skill", "inputs", "outputs"}
    if unknown:
        raise ValueError(
            "uses_skill contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    skill = _required_string(value, "skill")

    def parse_bindings(raw: object, label: str) -> tuple[SkillUsesSkillBinding, ...]:
        if raw is None:
            return ()
        entries: list[tuple[str, object]]
        if isinstance(raw, Mapping):
            entries = [(str(name), item) for name, item in raw.items()]
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            entries = []
            for item in raw:
                if not isinstance(item, Mapping):
                    raise ValueError(f"uses_skill.{label} must contain objects.")
                entries.append((_required_string(item, "name"), item))
        else:
            raise ValueError(f"uses_skill.{label} must be an object or array.")
        result: list[SkillUsesSkillBinding] = []
        seen: set[str] = set()
        for name, item in entries:
            if not name.strip() or name in seen:
                raise ValueError(
                    f"uses_skill.{label} names must be unique and non-empty."
                )
            seen.add(name)
            ref: str | None
            if isinstance(item, str):
                ref = item.strip()
                schema = None
            elif isinstance(item, Mapping):
                unknown_binding_fields = set(item) - {"name", "ref", "source", "schema"}
                if unknown_binding_fields:
                    raise ValueError(
                        f"uses_skill.{label}.{name} contains unsupported fields: "
                        + ", ".join(
                            sorted(str(field) for field in unknown_binding_fields)
                        )
                    )
                ref = _optional_string(item.get("ref"))
                if ref is None:
                    ref = _optional_string(item.get("source"))
                if ref is None:
                    raise ValueError(f"uses_skill.{label}.{name} must declare ref.")
                raw_schema = item.get("schema")
                if raw_schema is not None and not isinstance(raw_schema, Mapping):
                    raise ValueError(
                        f"uses_skill.{label}.{name}.schema must be an object."
                    )
                schema = dict(raw_schema) if isinstance(raw_schema, Mapping) else None
                if schema is not None:
                    schema_error = _output_schema_declaration_error(schema)
                    if schema_error is not None:
                        raise ValueError(schema_error)
            else:
                raise ValueError(
                    f"uses_skill.{label}.{name} must be a string or object."
                )
            result.append(SkillUsesSkillBinding(name=name, ref=ref, schema=schema))
        return tuple(result)

    return SkillUsesSkill(
        skill=skill,
        inputs=parse_bindings(value.get("inputs"), "inputs"),
        outputs=parse_bindings(value.get("outputs"), "outputs"),
    )


def _parse_step_completion(value: object) -> SkillStepCompletion | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Skill step completion must be an object.")
    raw_outputs = value.get("required_outputs")
    if (
        not isinstance(raw_outputs, Sequence)
        or isinstance(raw_outputs, (str, bytes, bytearray))
        or not raw_outputs
    ):
        raise ValueError(
            "Skill step completion.required_outputs must be a non-empty array."
        )
    required_outputs: list[str] = []
    for item in raw_outputs:
        output_name = _optional_string(item)
        if output_name is None:
            raise ValueError(
                "Skill step completion.required_outputs must contain strings."
            )
        if output_name in required_outputs:
            raise ValueError(
                "Skill step completion.required_outputs must not contain duplicates."
            )
        required_outputs.append(output_name)
    raw_actions = value.get("required_actions", [])
    if not isinstance(raw_actions, Sequence) or isinstance(
        raw_actions, (str, bytes, bytearray)
    ):
        raise ValueError("Skill step completion.required_actions must be an array.")
    required_actions: list[SkillStepRequiredAction] = []
    for item in raw_actions:
        if not isinstance(item, Mapping):
            raise ValueError(
                "Skill step completion.required_actions entries must be objects."
            )
        action = _optional_string(item.get("action"))
        targets_from = _optional_string(item.get("targets_from"))
        match_field = _optional_string(item.get("match_field"))
        exactly = item.get("exactly")
        parameters = item.get("parameters")
        if action is None or (targets_from is None) != (match_field is None):
            raise ValueError(
                "Skill step completion.required_actions entries must declare "
                "action, or both targets_from and match_field strings."
            )
        if exactly is not None and (
            isinstance(exactly, bool) or not isinstance(exactly, int) or exactly < 1
        ):
            raise ValueError(
                "Skill step completion.required_actions.exactly must be a "
                "positive integer."
            )
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ValueError(
                "Skill step completion.required_actions.parameters must be an object."
            )
        if set(item) - {
            "action",
            "targets_from",
            "match_field",
            "exactly",
            "parameters",
        }:
            raise ValueError(
                "Skill step completion.required_actions entries contain "
                "unsupported fields."
            )
        required_actions.append(
            SkillStepRequiredAction(
                action,
                targets_from,
                match_field,
                exactly,
                dict(parameters) if isinstance(parameters, Mapping) else None,
            )
        )
    unknown = set(value) - {"required_outputs", "required_actions"}
    if unknown:
        raise ValueError(
            "Skill step completion contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    return SkillStepCompletion(tuple(required_outputs), tuple(required_actions))


def _parse_coding_loop(value: object) -> CodingLoopSpec:
    if not isinstance(value, Mapping):
        raise ValueError("Skill step coding_loop must be an object.")
    goal = _required_string(value, "goal")
    raw_verification = value.get("verification", [])
    if (
        not isinstance(raw_verification, Sequence)
        or isinstance(raw_verification, (str, bytes, bytearray))
        or not all(
            isinstance(item, Mapping)
            and _optional_string(item.get("id")) is not None
            and _command_value(item.get("command")) is not None
            for item in raw_verification
        )
    ):
        raise ValueError(
            "Skill step coding_loop.verification must contain objects with "
            "non-empty id and command fields."
        )
    if not raw_verification:
        raise ValueError(
            "Skill step coding_loop.verification must contain at least one command."
        )
    raw_stopping = value.get("stopping_conditions", [])
    stopping_conditions = _optional_string_sequence(raw_stopping)
    max_iterations = value.get("max_iterations", 12)
    if (
        not isinstance(max_iterations, int)
        or isinstance(max_iterations, bool)
        or not (1 <= max_iterations <= 100)
    ):
        raise ValueError("Skill step coding_loop.max_iterations must be from 1 to 100.")
    verification = tuple(
        CodingLoopVerification(
            id=_required_string(item, "id"),
            command=_required_command_value(item["command"]),
            cwd=_optional_string(item.get("cwd")),
            env=tuple(sorted(_string_mapping(item.get("env"), "env").items())),
        )
        for item in raw_verification
    )
    return CodingLoopSpec(
        goal=goal,
        verification=verification,
        stopping_conditions=stopping_conditions,
        max_iterations=max_iterations,
    )


def _parse_pre_step(value: object) -> SkillStepPreStep | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Skill step pre_step must be an object.")
    action = _required_string(value, "action")
    if action not in {"gather_context", "invoke_tool"}:
        raise ValueError(
            "Skill step pre_step action must be gather_context or invoke_tool."
        )
    template = value.get("template")
    if not isinstance(template, Mapping):
        raise ValueError("Skill step pre_step template must be an object.")
    if action == "invoke_tool":
        if template.get("tool") == "enrich":
            _parse_enrich_invocation(template)
        elif template.get("tool") in {"validate_edit", "apply_edit"}:
            _parse_deferred_edit_invocation(template)
        else:
            _parse_tool_invocation(template)
        return SkillStepPreStep(action=action, template=dict(template))
    feature_id = _optional_string(template.get("feature_id"))
    if feature_id is None:
        raise ValueError("Gather context templates must include feature_id.")
    types = template.get("types")
    if (
        not isinstance(types, Sequence)
        or isinstance(types, (str, bytes, bytearray))
        or not types
    ):
        raise ValueError(
            "Gather context templates must include a non-empty types array."
        )
    return SkillStepPreStep(
        action=action,
        template=dict(template),
    )


def _parse_gate(value: object) -> SkillStepGate | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Skill step gate must be an object.")
    outcome = value.get("outcome")
    if not isinstance(outcome, Mapping):
        raise ValueError("Gate outcome must be an object.")
    path = _optional_string(outcome.get("path"))
    if path is None or "equals" not in outcome:
        raise ValueError("Gate outcome must declare path and equals.")
    return SkillStepGate(
        outcome={**outcome, "path": path},
        goto_step=_required_string(value, "goto_step"),
        retry_context=_required_string(value, "retry_context"),
        success_goto_step=_optional_string(value.get("success_goto_step")),
    )


def _parse_step_inputs(value: object) -> tuple[SkillStepInput, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step inputs must be an array.")
    result: list[SkillStepInput] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Skill step inputs must contain objects.")
        name = _required_string(item, "name")
        item_type = _optional_string(item.get("type")) or "any"
        source = _optional_string(item.get("source")) or "previous_step"
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise ValueError("Skill step input required must be a boolean.")
        result.append(SkillStepInput(name, item_type, required, source))
    return tuple(result)


def _parse_step_outputs(value: object) -> tuple[SkillStepOutput, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step outputs must be an array.")
    result: list[SkillStepOutput] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Skill step outputs must contain objects.")
        name = _required_string(item, "name")
        item_type = _optional_string(item.get("type")) or "any"
        required_for_next_step = item.get("required_for_next_step", False)
        if not isinstance(required_for_next_step, bool):
            raise ValueError(
                "Skill step output required_for_next_step must be a boolean."
            )
        scope = _optional_string(item.get("scope")) or "skill"
        raw_schema = item.get("schema")
        if raw_schema is not None and not isinstance(raw_schema, Mapping):
            raise ValueError("Skill step output schema must be an object.")
        if isinstance(raw_schema, Mapping):
            schema_error = _output_schema_declaration_error(raw_schema)
            if schema_error is not None:
                raise ValueError(schema_error)
        schema = dict(raw_schema) if isinstance(raw_schema, Mapping) else None
        result.append(
            SkillStepOutput(name, item_type, required_for_next_step, scope, schema)
        )
    return tuple(result)


def _output_schema_declaration_error(schema: Mapping[str, Any]) -> str | None:
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "minLength",
        "enum",
    }
    unknown = sorted(str(key) for key in set(schema) - allowed)
    if unknown:
        return "Skill step output schema has unknown fields: " + ", ".join(unknown)
    schema_type = schema.get("type")
    if schema_type not in {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
        "null",
    }:
        return "Skill step output schema must declare a supported type."
    properties = schema.get("properties")
    if properties is not None:
        if schema_type != "object" or not isinstance(properties, Mapping):
            return "Skill step output schema properties must be an object."
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                return "Skill step output schema properties must contain schemas."
            error = _output_schema_declaration_error(child)
            if error is not None:
                return error
    items = schema.get("items")
    if items is not None:
        if schema_type != "array" or not isinstance(items, Mapping):
            return "Skill step output schema items must be an object."
        error = _output_schema_declaration_error(items)
        if error is not None:
            return error
    return None


def _report_to_data(report: SkillValidationReport) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "skill_names": report.skill_names,
        "skill_paths": report.skill_paths,
        "issues": [validation_error_to_data(issue) for issue in report.issues],
    }


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = _optional_string(data.get(key))
    if value is None:
        raise ValueError(f"Skill entries must include a non-empty {key}.")
    return value


def _required_string_sequence(
    data: Mapping[str, Any],
    key: str,
) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"Skill entries must include a {key} array.")
    return tuple(_required_string({key: item}, key) for item in value)


def _optional_string_sequence(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Value must be an array of strings.")
    return tuple(_required_string({"value": item}, "value") for item in value)


def _command_value(value: object) -> str | tuple[str, ...] | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if not value or not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return tuple(item.strip() for item in value)


def _required_command_value(value: object) -> str | tuple[str, ...]:
    command = _command_value(value)
    if command is None:
        raise ValueError(
            "Skill step coding_loop verification command must be a non-empty "
            "string or command array."
        )
    return command


def _string_mapping(value: object, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(
            f"Skill step coding_loop verification {field_name} must be an object."
        )
    if any(
        not isinstance(key, str) or not key.strip() or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(
            f"Skill step coding_loop verification {field_name} keys and values "
            "must be strings."
        )
    return {key.strip(): item for key, item in value.items()}


def _optional_prompt_catalogs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step prompt_catalogs must be an array.")
    if not value:
        raise ValueError(
            "Skill step prompt_catalogs must omit the field when no prompt catalog "
            "is needed."
        )
    result = tuple(_required_string({"value": item}, "value") for item in value)
    unsupported = sorted(set(result) - SUPPORTED_PROMPT_CATALOGS)
    if unsupported:
        raise ValueError(
            "Skill step prompt_catalogs contains unsupported values: "
            + ", ".join(unsupported)
        )
    if len(result) != len(set(result)):
        raise ValueError("Skill step prompt_catalogs must not contain duplicates.")
    return result


def _optional_step_actions(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step actions must be an array.")
    if not value:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = _optional_string(item)
        if name is None:
            raise ValueError("Skill step actions must contain non-empty strings.")
        if name in UNIVERSAL_STEP_ACTIONS:
            raise ValueError(f"Skill step actions must omit universal action: {name}.")
        if name not in SUPPORTED_STEP_ACTIONS:
            raise ValueError(f"Skill step actions contains unsupported action: {name}.")
        if name in seen:
            raise ValueError("Skill step actions must not contain duplicates.")
        seen.add(name)
        result.append(name)
    return tuple(result)


def _validate_step_actions(
    value: object,
    step_path: str,
    issues: list[SkillValidationIssue],
) -> None:
    if value is None:
        return
    try:
        _optional_step_actions(value)
    except ValueError as exc:
        issues.append(
            SkillValidationIssue(
                code="invalid_actions",
                message=str(exc),
                path=_child_path(step_path, "actions"),
            )
        )


def _validate_gather_context_pre_step(
    value: object,
    step_path: str,
    issues: list[SkillValidationIssue],
) -> None:
    pre_step_path = _child_path(step_path, "pre_step")
    if not isinstance(value, Mapping):
        issues.append(
            SkillValidationIssue(
                code="missing_pre_step",
                message=(
                    "invoke_tool steps with a pre_step must declare a valid pre_step."
                ),
                path=pre_step_path,
            )
        )
        return
    _validate_unknown_keys(
        value,
        {"action", "template"},
        issues,
        path=pre_step_path or "",
        subject="skill step pre_step",
    )
    action = _optional_string(value.get("action"))
    if action not in {"gather_context", "invoke_tool"}:
        issues.append(
            SkillValidationIssue(
                code="invalid_pre_step_action",
                message=(
                    "Skill step pre_step action must be gather_context or invoke_tool."
                ),
                path=_child_path(pre_step_path, "action"),
            )
        )
    if action == "invoke_tool":
        template = value.get("template")
        if not isinstance(template, Mapping):
            issues.append(
                SkillValidationIssue(
                    code="invalid_pre_step_template",
                    message="Invoke tool pre-steps must include a template object.",
                    path=_child_path(pre_step_path, "template"),
                )
            )
            return
        allowed_keys = {"tool", "command", "operation", "cwd", "env"}
        if template.get("tool") == "git":
            allowed_keys |= {"paths", "source", "destination", "branch", "message"}
        if template.get("tool") == "enrich":
            allowed_keys = {"tool", "format", "tool_output"}
        elif template.get("tool") in {"validate_edit", "apply_edit"}:
            allowed_keys = {"tool", "edit"}
        _validate_unknown_keys(
            template,
            allowed_keys,
            issues,
            path=_child_path(pre_step_path, "template") or "",
            subject="invoke_tool pre-step template",
        )
        tool = _optional_string(template.get("tool"))
        if tool is None or tool not in SUPPORTED_SKILL_TOOL_TYPES - {"ref"}:
            issues.append(
                SkillValidationIssue(
                    code="invalid_pre_step_tool",
                    message=(
                        "Invoke tool pre-steps must use a supported executable tool."
                    ),
                    path=_child_path(_child_path(pre_step_path, "template"), "tool"),
                )
            )
        if tool == "enrich":
            if template.get("format") != "pytest":
                issues.append(
                    SkillValidationIssue(
                        code="invalid_enrich_format",
                        message="Enrich pre-steps currently require format pytest.",
                        path=_child_path(
                            _child_path(pre_step_path, "template"), "format"
                        ),
                    )
                )
            tool_output = template.get("tool_output")
            valid_tool_output = tool_output == {"source": "previous_tool_output"}
            if isinstance(tool_output, Mapping):
                valid_tool_output = (
                    tool_output.get("source") == "handoff"
                    and isinstance(tool_output.get("name"), str)
                    and bool(tool_output.get("name"))
                )
            if not valid_tool_output:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_enrich_input",
                        message=(
                            "Enrich pre-steps must consume the previous tool result "
                            "with {source: previous_tool_output}."
                        ),
                        path=_child_path(
                            _child_path(pre_step_path, "template"), "tool_output"
                        ),
                    )
                )
            return
        if tool in {"validate_edit", "apply_edit"}:
            if not isinstance(template.get("edit"), (str, Mapping)):
                issues.append(
                    SkillValidationIssue(
                        code="invalid_deferred_edit_input",
                        message=(
                            "Deferred edit pre-steps must include an edit object "
                            "or a complete-value placeholder."
                        ),
                        path=_child_path(
                            _child_path(pre_step_path, "template"), "edit"
                        ),
                    )
                )
            return
        if tool in {"git", "gh"} and template.get("operation") is not None:
            if _optional_string(template.get("operation")) is None:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_pre_step_operation",
                        message=(
                            "Intrinsic pre-step operations must be non-empty strings."
                        ),
                        path=_child_path(
                            _child_path(pre_step_path, "template"), "operation"
                        ),
                    )
                )
            return
        command = template.get("command")
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes, bytearray))
            or not command
            or any(_optional_string(item) is None for item in command)
        ):
            issues.append(
                SkillValidationIssue(
                    code="invalid_pre_step_command",
                    message=(
                        "Invoke tool pre-step templates must include a non-empty "
                        "command array."
                    ),
                    path=_child_path(_child_path(pre_step_path, "template"), "command"),
                )
            )
        if tool == "internal" and (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes, bytearray))
            or not command
            or command[0] != "powdrr-lift"
        ):
            issues.append(
                SkillValidationIssue(
                    code="invalid_internal_command",
                    message="Internal pre-step commands must invoke powdrr-lift.",
                    path=_child_path(_child_path(pre_step_path, "template"), "command"),
                )
            )
        return
    template = value.get("template")
    if not isinstance(template, Mapping):
        issues.append(
            SkillValidationIssue(
                code="invalid_pre_step_template",
                message="Skill step pre_step template must be an object.",
                path=_child_path(pre_step_path, "template"),
            )
        )
        return
    _validate_unknown_keys(
        template,
        {"feature_id", "types", "keywords", "filters"},
        issues,
        path=_child_path(pre_step_path, "template") or "",
        subject="skill step pre_step template",
    )
    if _optional_string(template.get("feature_id")) is None:
        issues.append(
            SkillValidationIssue(
                code="missing_pre_step_feature_id",
                message="Gather context templates must include feature_id.",
                path=_child_path(_child_path(pre_step_path, "template"), "feature_id"),
            )
        )
    types = template.get("types")
    if (
        not isinstance(types, Sequence)
        or isinstance(types, (str, bytes, bytearray))
        or not types
    ):
        issues.append(
            SkillValidationIssue(
                code="invalid_pre_step_types",
                message=(
                    "Gather context templates must include a non-empty types array."
                ),
                path=_child_path(_child_path(pre_step_path, "template"), "types"),
            )
        )


def _optional_tool_invocation_packages(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step tool_invocation_packages must be an array.")
    packages = tuple(_required_string({"package": item}, "package") for item in value)
    unsupported = sorted(set(packages) - SUPPORTED_TOOL_INVOCATION_PACKAGES)
    if unsupported:
        raise ValueError(
            "Unsupported tool invocation package(s): " + ", ".join(unsupported)
        )
    if len(packages) != len(set(packages)):
        raise ValueError(
            "Skill step tool_invocation_packages must not contain duplicates."
        )
    return packages


def _merge_tool_invocations(
    explicit: tuple[SkillToolInvocation, ...], packages: tuple[str, ...]
) -> tuple[SkillToolInvocation, ...]:
    expanded = [
        _parse_tool_invocation(item)
        for package in packages
        for item in TOOL_INVOCATION_PACKAGES[package]
    ]
    merged: list[SkillToolInvocation] = []
    for invocation in (*explicit, *expanded):
        if invocation not in merged:
            merged.append(invocation)
    return tuple(merged)


def _optional_tool_invocations(value: object) -> tuple[SkillToolInvocation, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step tool_invocations must be an array.")
    return tuple(_parse_tool_invocation(item) for item in value)


def _parse_tool_invocation(raw_tool_invocation: object) -> SkillToolInvocation:
    if not isinstance(raw_tool_invocation, Mapping):
        raise ValueError("Skill tool invocations must be objects.")
    raw_mapping = cast("Mapping[str, Any]", raw_tool_invocation)
    tool = _required_string(raw_mapping, "tool")
    label = _optional_string(raw_mapping.get("label"))
    if tool == "ref":
        if label is None:
            raise ValueError("Tool references must include a label.")
        if raw_mapping.get("command") is not None:
            raise ValueError("Tool references must not include a command.")
        return SkillToolInvocation(tool=tool, command=(), label=label)
    command_value = raw_mapping.get("command")
    operation = _optional_string(raw_mapping.get("operation"))
    if operation is not None:
        if command_value is not None:
            raise ValueError("Structured tool invocations must not include command.")
        return SkillToolInvocation(tool=tool, operation=operation)
    if not isinstance(command_value, Sequence) or isinstance(
        command_value,
        (str, bytes, bytearray),
    ):
        raise ValueError("Skill tool invocation command must be an array.")
    command = tuple(
        _required_string({"command": item}, "command") for item in command_value
    )
    cwd = _optional_string(raw_mapping.get("cwd"))
    env_value = raw_mapping.get("env")
    if env_value is None:
        env: tuple[tuple[str, str], ...] = ()
    else:
        if not isinstance(env_value, Mapping):
            raise ValueError("Skill tool invocation env must be an object.")
        env = tuple(
            (
                _required_string({"key": key}, "key"),
                _required_string({"value": value}, "value"),
            )
            for key, value in env_value.items()
        )
    return SkillToolInvocation(
        tool=tool,
        command=command,
        label=label,
        cwd=cwd,
        env=env,
    )


def _parse_enrich_invocation(raw_tool_invocation: object) -> SkillToolInvocation:
    """Validate the declarative, non-shell enrich intrinsic invocation."""
    if not isinstance(raw_tool_invocation, Mapping):
        raise ValueError("Skill tool invocations must be objects.")
    if raw_tool_invocation.get("tool") != "enrich":
        raise ValueError("Enrich invocations must use tool enrich.")
    if raw_tool_invocation.get("format") != "pytest":
        raise ValueError("Enrich invocations currently require format pytest.")
    tool_output = raw_tool_invocation.get("tool_output")
    valid_tool_output = tool_output == {"source": "previous_tool_output"}
    if isinstance(tool_output, Mapping):
        valid_tool_output = (
            tool_output.get("source") == "handoff"
            and isinstance(tool_output.get("name"), str)
            and bool(tool_output.get("name"))
        )
    if not valid_tool_output:
        raise ValueError("Enrich invocations must consume previous_tool_output.")
    return SkillToolInvocation(tool="enrich", command=())


def _parse_deferred_edit_invocation(raw_tool_invocation: object) -> SkillToolInvocation:
    if not isinstance(raw_tool_invocation, Mapping):
        raise ValueError("Deferred edit invocations must be objects.")
    tool = raw_tool_invocation.get("tool")
    if tool not in {"validate_edit", "apply_edit"}:
        raise ValueError("Deferred edit invocation has an unsupported tool.")
    if "edit" not in raw_tool_invocation:
        raise ValueError("Deferred edit invocations must include edit.")
    return SkillToolInvocation(tool=cast(str, tool), command=())


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    return normalized_value or None


def _optional_interaction_style(value: object) -> str | None:
    normalized_value = _optional_string(value)
    if normalized_value not in SUPPORTED_INTERACTION_STYLES:
        return None
    return normalized_value


def _validate_unknown_keys(
    data: Mapping[str, Any],
    allowed_keys: set[str],
    issues: list[SkillValidationIssue],
    *,
    path: str,
    subject: str,
) -> None:
    for key in data:
        if key not in allowed_keys:
            issues.append(
                SkillValidationIssue(
                    code="unknown_key",
                    message=f"Unknown {subject} field: {key}.",
                    path=_child_path(path or None, key),
                )
            )


def _path_prefix(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(path)


def _child_path(path: str | Path | None, child: str) -> str | None:
    prefix = _path_prefix(path)
    if prefix is None:
        return child
    return f"{prefix}.{child}"


def _sequence_path(
    path: str | Path | None,
    key: str,
    index: int,
    *rest: int | str,
) -> str:
    prefix = _path_prefix(path)
    path_str = f"{prefix}.{key}[{index}]" if prefix is not None else f"{key}[{index}]"
    for item in rest:
        if isinstance(item, int):
            path_str += f"[{item}]"
        else:
            path_str += f".{item}"
    return path_str.lstrip(".")


def _skill_paths_list(source_path: str | Path | None) -> list[str]:
    if source_path is None:
        return []
    return [str(source_path)]
