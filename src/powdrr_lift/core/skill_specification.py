from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class SkillValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class SkillValidationReport:
    validation_successful: bool
    skill_names: list[str] = field(default_factory=list)
    skill_paths: list[str] = field(default_factory=list)
    issues: list[SkillValidationIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SkillStep:
    description: str
    details: str | None = None
    uses_skills: tuple[str, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"description": self.description}
        if self.details is not None:
            data["details"] = self.details
        if self.uses_skills:
            data["uses_skills"] = list(self.uses_skills)
        return data


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    when_to_use: tuple[str, ...]
    steps: tuple[SkillStep, ...]

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "when_to_use": list(self.when_to_use),
            "steps": [step.to_data() for step in self.steps],
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


def skill_from_json(json_content: str) -> Skill:
    loaded_content = json.loads(json_content)
    if not isinstance(loaded_content, Mapping):
        raise ValueError("Skill JSON must decode to an object.")
    return skill_from_data(cast("Mapping[str, Any]", loaded_content))


def skill_from_data(data: Mapping[str, Any]) -> Skill:
    name = _required_string(data, "name")
    when_to_use = _required_string_sequence(data, "when_to_use")
    steps = _parse_steps(data.get("steps"))
    return Skill(name=name, when_to_use=when_to_use, steps=steps)


def load_skill(path: str | Path) -> Skill:
    return skill_from_json(Path(path).read_text(encoding="utf-8"))


def save_skill(skill: Skill, path: str | Path) -> Path:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(skill_to_json(skill), encoding="utf-8")
    return resolved_path


def load_skills(directory: str | Path) -> tuple[Skill, ...]:
    directory_path = Path(directory)
    return tuple(
        load_skill(skill_path)
        for skill_path in sorted(directory_path.glob("*.json"))
        if skill_path.is_file()
    )


def build_skill_validation_report(
    json_content: str,
    *,
    source_path: str | Path | None = None,
) -> SkillValidationReport:
    try:
        loaded_content = json.loads(json_content)
    except Exception as exc:  # noqa: BLE001
        return SkillValidationReport(
            validation_successful=False,
            issues=[
                SkillValidationIssue(
                    code="invalid_json",
                    message=f"Could not parse skill JSON: {exc}",
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
                    message="Skill JSON must decode to an object.",
                    path=_path_prefix(source_path),
                )
            ],
        )

    raw_skill = cast("Mapping[str, Any]", loaded_content)
    issues: list[SkillValidationIssue] = []

    _validate_unknown_keys(
        raw_skill,
        {"name", "when_to_use", "steps"},
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
                {"description", "details", "uses_skills"},
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

            details = step_mapping.get("details")
            if details is not None and _optional_string(details) is None:
                issues.append(
                    SkillValidationIssue(
                        code="invalid_details",
                        message="Skill step details must be a non-empty string.",
                        path=_child_path(step_path, "details"),
                    )
                )

            uses_skills = step_mapping.get("uses_skills")
            if uses_skills is None:
                continue
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
                continue

            seen_refs: set[str] = set()
            for ref_index, ref_value in enumerate(uses_skills):
                normalized_ref = _optional_string(ref_value)
                if normalized_ref is None:
                    issues.append(
                        SkillValidationIssue(
                            code="invalid_uses_skills_item",
                            message=(
                                "Skill step uses_skills must contain non-empty strings."
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
                                "Skill step uses_skills must not contain duplicates."
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

    issues: list[SkillValidationIssue] = []
    skill_names: list[str] = []
    skill_paths: list[str] = []
    skills_by_name: dict[str, Skill] = {}
    skill_paths_by_name: dict[str, Path] = {}
    step_references: list[tuple[Path, Skill]] = []

    for skill_path in sorted(directory_path.glob("*.json")):
        if not skill_path.is_file():
            continue
        skill_paths.append(str(skill_path))
        raw_content = skill_path.read_text(encoding="utf-8")
        file_report = build_skill_validation_report(raw_content, source_path=skill_path)
        issues.extend(file_report.issues)
        if not file_report.validation_successful or not file_report.skill_names:
            continue

        skill = skill_from_json(raw_content)
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

    for skill_path, skill in step_references:
        for step_index, step in enumerate(skill.steps):
            for ref_index, referenced_skill in enumerate(step.uses_skills):
                if referenced_skill == skill.name:
                    issues.append(
                        SkillValidationIssue(
                            code="self_dependency",
                            message=(
                                f"Skill {skill.name!r} cannot reference itself "
                                "from a step."
                            ),
                            path=_sequence_path(
                                skill_path,
                                "steps",
                                step_index,
                            ),
                        )
                    )
                elif referenced_skill not in skills_by_name:
                    issues.append(
                        SkillValidationIssue(
                            code="missing_skill_reference",
                            message=(
                                f"Skill {skill.name!r} references unknown skill "
                                f"{referenced_skill!r}."
                            ),
                            path=_sequence_path(
                                skill_path,
                                "steps",
                                step_index,
                                "uses_skills",
                                ref_index,
                            ),
                        )
                    )

    return SkillValidationReport(
        validation_successful=not issues,
        skill_names=skill_names,
        skill_paths=skill_paths,
        issues=issues,
    )


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


def _parse_step(raw_step: object) -> SkillStep:
    if not isinstance(raw_step, Mapping):
        raise ValueError("Skill steps must be objects.")
    raw_step_mapping = cast("Mapping[str, Any]", raw_step)
    description = _required_string(raw_step_mapping, "description")
    details = _optional_string(raw_step_mapping.get("details"))
    uses_skills = _optional_string_sequence(raw_step_mapping.get("uses_skills"))
    return SkillStep(
        description=description,
        details=details,
        uses_skills=uses_skills,
    )


def _report_to_data(report: SkillValidationReport) -> dict[str, Any]:
    return {
        "validation_successful": report.validation_successful,
        "skill_names": report.skill_names,
        "skill_paths": report.skill_paths,
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
