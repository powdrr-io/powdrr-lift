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
    frozenset({"shell", "internal", "fuzzy-match", "ref"}) | BASEDPYRIGHT_TOOLS
)
SUPPORTED_PROMPT_CATALOGS = frozenset({"context_types", "skills"})


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
    label: str | None = None
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tool": self.tool,
        }
        if self.command:
            data["command"] = list(self.command)
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

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required_for_next_step": self.required_for_next_step,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class SkillStep:
    description: str
    details: str | None = None
    llm_type: str | None = None
    uses_skills: tuple[str, ...] = field(default_factory=tuple)
    tool_invocations: tuple[SkillToolInvocation, ...] = field(default_factory=tuple)
    prompt_catalogs: tuple[str, ...] | None = None
    id: str | None = None
    inputs: tuple[SkillStepInput, ...] = field(default_factory=tuple)
    outputs: tuple[SkillStepOutput, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"description": self.description}
        if self.id is not None:
            data["id"] = self.id
        if self.details is not None:
            data["details"] = self.details
        if self.llm_type is not None:
            data["llm_type"] = self.llm_type
        if self.uses_skills:
            data["uses_skills"] = list(self.uses_skills)
        if self.tool_invocations:
            data["tool_invocations"] = [
                tool_invocation.to_data() for tool_invocation in self.tool_invocations
            ]
        if self.prompt_catalogs is not None:
            data["prompt_catalogs"] = list(self.prompt_catalogs)
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
    adversarial: bool | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "when_to_use": list(self.when_to_use),
            "steps": [step.to_data() for step in self.steps],
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
    adversarial = data.get("adversarial")
    if adversarial is not None and not isinstance(adversarial, bool):
        raise ValueError("Skill adversarial must be a boolean.")
    return Skill(
        name=name,
        when_to_use=when_to_use,
        steps=steps,
        adversarial=adversarial,
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
        {"name", "when_to_use", "steps", "adversarial"},
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
        seen_step_ids: set[str] = set()
        for index, step in enumerate(steps):
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
                    "details",
                    "llm_type",
                    "uses_skills",
                    "tool_invocations",
                    "prompt_catalogs",
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

            uses_skills = step_mapping.get("uses_skills")
            if uses_skills is None:
                uses_skills = ()
            if not isinstance(uses_skills, Sequence) or isinstance(
                uses_skills,
                (str, bytes, bytearray),
            ):
                issues.append(
                    SkillValidationIssue(
                        code="invalid_uses_skills_type",
                        message="Skill step uses_skills must be an array.",
                        path=_child_path(step_path, "uses_skills"),
                    )
                )
            else:
                seen_refs: set[str] = set()
                for ref_index, ref_value in enumerate(uses_skills):
                    normalized_ref = _optional_string(ref_value)
                    if normalized_ref is None:
                        issues.append(
                            SkillValidationIssue(
                                code="invalid_uses_skills_item",
                                message=(
                                    "Skill step uses_skills must contain non-empty "
                                    "strings."
                                ),
                                path=_sequence_path(
                                    step_path,
                                    "uses_skills",
                                    ref_index,
                                ),
                            )
                        )
                        continue
                    if normalized_ref in seen_refs:
                        issues.append(
                            SkillValidationIssue(
                                code="duplicate_uses_skill",
                                message=(
                                    "Skill step uses_skills must not contain "
                                    "duplicates."
                                ),
                                path=_sequence_path(
                                    step_path,
                                    "uses_skills",
                                    ref_index,
                                ),
                            )
                        )
                        continue
                    seen_refs.add(normalized_ref)

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
                                        "support context_types and skills."
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

            _validate_step_contracts(step_mapping, step_path, issues)

            tool_invocations = step_mapping.get("tool_invocations")
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
                    {"tool", "command", "label", "cwd", "env"},
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
                if not isinstance(command, Sequence) or isinstance(
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
            for ref_index, referenced_skill in enumerate(step.uses_skills):
                reference_path = _sequence_path(
                    skill_path,
                    "steps",
                    step_index,
                    "uses_skills",
                    ref_index,
                )
                if referenced_skill == skill.name:
                    issues.append(
                        SkillValidationIssue(
                            code="self_dependency",
                            message=(
                                f"Skill {skill.name!r} cannot reference itself "
                                "from a step."
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
            for referenced_skill in step.uses_skills
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
        ("outputs", {"name", "type", "required_for_next_step", "scope"}),
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


def _parse_step(raw_step: object) -> SkillStep:
    if not isinstance(raw_step, Mapping):
        raise ValueError("Skill steps must be objects.")
    return skill_step_from_data(cast("Mapping[str, Any]", raw_step))


def skill_step_from_data(data: Mapping[str, Any]) -> SkillStep:
    """Parse the reusable executable-step shape used by skills and workflows."""
    step_id = _optional_string(data.get("id"))
    description = _required_string(data, "description")
    details = _optional_string(data.get("details"))
    llm_type = _optional_string(data.get("llm_type"))
    uses_skills = _optional_string_sequence(data.get("uses_skills"))
    tool_invocations = _optional_tool_invocations(
        data.get("tool_invocations"),
    )
    prompt_catalogs = _optional_prompt_catalogs(data.get("prompt_catalogs"))
    inputs = _parse_step_inputs(data.get("inputs"))
    outputs = _parse_step_outputs(data.get("outputs"))
    return SkillStep(
        id=step_id,
        description=description,
        details=details,
        llm_type=llm_type,
        uses_skills=uses_skills,
        tool_invocations=tool_invocations,
        prompt_catalogs=prompt_catalogs,
        inputs=inputs,
        outputs=outputs,
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
        result.append(SkillStepOutput(name, item_type, required_for_next_step, scope))
    return tuple(result)


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
        raise ValueError("Skill step uses_skills must be an array.")
    return tuple(_required_string({"value": item}, "value") for item in value)


def _optional_prompt_catalogs(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Skill step prompt_catalogs must be an array.")
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


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    return normalized_value or None


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
