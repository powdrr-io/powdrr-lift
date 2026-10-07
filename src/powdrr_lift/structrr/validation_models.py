"""Typed, versioned Structrr records for discovered validation checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

VALIDATION_INVENTORY_SCHEMA_VERSION = "verification-provider-inventory-v2"
VALIDATION_CONTEXT_SCHEMA_VERSION = "validation-context-v1"
LEGACY_VALIDATION_INVENTORY_SCHEMA_VERSION = "verification-provider-inventory-v1"

_CONFIRMATION_LEVELS = {
    "static",
    "availability",
    "syntax",
    "configuration",
    "collection",
    "execution",
}
_CHECK_OUTCOMES = {
    "not_run",
    "passed",
    "failed",
    "blocked",
    "timed_out",
    "not_applicable",
}
_REQUIREDNESS = {"required", "optional", "unknown"}
_APPLICABILITY = {"applicable", "not_applicable", "unknown"}
_DISCOVERY_STATUSES = {"complete", "partial", "unknown"}
_EXECUTION_KINDS = {"argv", "shell", "unresolved"}
_DECLARATION_KINDS = {"declared", "inferred", "observed", "unknown"}
_SETTINGS_STATUSES = {"declared_only", "effective", "mixed", "unknown"}
_REPRODUCIBILITY = {"yes", "no", "partial", "unknown"}


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One validation obligation, including enough context to explain it."""

    id: str
    provider: str
    profile: str
    command: tuple[str, ...]
    source: str
    environment: str | None = None
    execution: Mapping[str, Any] = field(
        default_factory=lambda: {
            "kind": "argv",
            "cwd": ".",
            "shell": None,
            "script": None,
        }
    )
    component: str | None = None
    purpose: str = ""
    roles: tuple[str, ...] = ()
    selectors: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)
    settings_status: str = "declared_only"
    applicability: Mapping[str, Any] = field(
        default_factory=lambda: {"local": True, "evaluation": "unknown"}
    )
    ci_origins: tuple[Mapping[str, Any], ...] = ()
    requiredness: Mapping[str, Any] = field(
        default_factory=lambda: {"status": "unknown", "evidence": []}
    )
    provenance: Mapping[str, Any] = field(
        default_factory=lambda: {"declaration": "inferred", "evidence": []}
    )
    confirmation: Mapping[str, Any] = field(
        default_factory=lambda: {"level": "static", "observations": []}
    )
    depends_on: tuple[str, ...] = ()
    local_reproducibility: str = "unknown"
    baseline: Mapping[str, Any] = field(
        default_factory=lambda: {"status": "not_run", "observation": None}
    )
    unresolved: tuple[str, ...] = ()
    schema_version: str = VALIDATION_INVENTORY_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        """Serialize with plain YAML/JSON-compatible values."""
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "provider": self.provider,
            "profile": self.profile,
            "component": self.component,
            "purpose": self.purpose,
            "roles": list(self.roles),
            "command": list(self.command),
            "execution": dict(self.execution),
            "selectors": list(self.selectors),
            "config_files": list(self.config_files),
            "settings": dict(self.settings),
            "settings_status": self.settings_status,
            "applicability": dict(self.applicability),
            "ci_origins": [dict(origin) for origin in self.ci_origins],
            "requiredness": dict(self.requiredness),
            "provenance": dict(self.provenance),
            "confirmation": dict(self.confirmation),
            "depends_on": list(self.depends_on),
            "local_reproducibility": self.local_reproducibility,
            "baseline": dict(self.baseline),
            "unresolved": list(self.unresolved),
            "source": self.source,
            "environment": self.environment,
        }

    @classmethod
    def from_data(cls, value: Mapping[str, Any]) -> ValidationCheck:
        """Parse a v1 or v2 inventory item, preserving legacy uncertainty."""
        version = value.get("schema_version")
        if version == LEGACY_VALIDATION_INVENTORY_SCHEMA_VERSION:
            profile = _required_text(value, "profile")
            provider = _required_text(value, "provider")
            command = _string_tuple(value.get("command"), "command")
            return cls(
                id=str(value.get("id") or f"validation:{profile}"),
                provider=provider,
                profile=profile,
                command=command,
                source=str(value.get("source") or "legacy v1 inventory"),
                environment=None,
                selectors=_string_tuple(value.get("selectors", []), "selectors"),
                schema_version=VALIDATION_INVENTORY_SCHEMA_VERSION,
                purpose="Imported from a legacy validation inventory.",
                provenance={
                    "declaration": "unknown",
                    "evidence": [],
                    "legacy_schema_version": version,
                },
                confirmation={"level": "static", "observations": []},
                unresolved=(
                    "Legacy v1 inventory did not record execution context or "
                    "whether the command was confirmed.",
                ),
            )
        if version != VALIDATION_INVENTORY_SCHEMA_VERSION:
            raise ValueError(f"unsupported validation schema version: {version!r}")
        return cls(
            id=_required_text(value, "id"),
            provider=_required_text(value, "provider"),
            profile=_required_text(value, "profile"),
            command=_string_tuple(value.get("command"), "command"),
            source=_required_text(value, "source"),
            environment=_optional_text(value.get("environment"), "environment"),
            execution=_mapping(value.get("execution"), "execution"),
            component=_optional_text(value.get("component"), "component"),
            purpose=str(value.get("purpose", "")),
            roles=_string_tuple(value.get("roles", []), "roles"),
            selectors=_string_tuple(value.get("selectors", []), "selectors"),
            config_files=_string_tuple(value.get("config_files", []), "config_files"),
            settings=_mapping(value.get("settings", {}), "settings"),
            settings_status=str(value.get("settings_status", "declared_only")),
            applicability=_mapping(value.get("applicability"), "applicability"),
            ci_origins=_mapping_tuple(value.get("ci_origins", []), "ci_origins"),
            requiredness=_mapping(value.get("requiredness"), "requiredness"),
            provenance=_mapping(value.get("provenance"), "provenance"),
            confirmation=_mapping(value.get("confirmation"), "confirmation"),
            depends_on=_string_tuple(value.get("depends_on", []), "depends_on"),
            local_reproducibility=str(value.get("local_reproducibility", "unknown")),
            baseline=_mapping(value.get("baseline"), "baseline"),
            unresolved=_string_tuple(value.get("unresolved", []), "unresolved"),
        )


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Repository-level validation discovery state referenced by check rows."""

    discovery_status: str = "unknown"
    coverage: tuple[str, ...] = ()
    components: tuple[Mapping[str, Any], ...] = ()
    environments: tuple[Mapping[str, Any], ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    input_fingerprints: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = VALIDATION_CONTEXT_SCHEMA_VERSION

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "discovery_status": self.discovery_status,
            "coverage": list(self.coverage),
            "components": [dict(value) for value in self.components],
            "environments": [dict(value) for value in self.environments],
            "evidence": [dict(value) for value in self.evidence],
            "diagnostics": [dict(value) for value in self.diagnostics],
            "input_fingerprints": [dict(value) for value in self.input_fingerprints],
        }


@dataclass(frozen=True, slots=True)
class ValidationModelIssue:
    code: str
    message: str
    path: str


def validate_validation_records(
    inventory: object, context: object
) -> tuple[ValidationModelIssue, ...]:
    """Validate v1/v2 check entries and the v1 repository context section."""
    issues: list[ValidationModelIssue] = []
    if not _is_sequence(inventory):
        return (
            ValidationModelIssue(
                "validation_inventory_invalid",
                "validation_inventory must be a list.",
                "validation_inventory",
            ),
        )
    if not isinstance(context, Mapping):
        issues.append(
            ValidationModelIssue(
                "validation_context_invalid",
                "validation_context must be a mapping.",
                "validation_context",
            )
        )
        context_mapping: Mapping[str, Any] = {}
    else:
        context_mapping = context
    if context_mapping.get("schema_version") != VALIDATION_CONTEXT_SCHEMA_VERSION:
        issues.append(
            ValidationModelIssue(
                "validation_context_schema_unsupported",
                "validation_context has an unsupported schema version.",
                "validation_context.schema_version",
            )
        )
    if context_mapping.get("discovery_status") not in _DISCOVERY_STATUSES:
        issues.append(
            ValidationModelIssue(
                "validation_context_status_invalid",
                "discovery_status must be complete, partial, or unknown.",
                "validation_context.discovery_status",
            )
        )
    for field_name in (
        "coverage",
        "components",
        "environments",
        "evidence",
        "diagnostics",
        "input_fingerprints",
    ):
        field_value = context_mapping.get(field_name)
        if not _is_sequence(field_value):
            issues.append(
                ValidationModelIssue(
                    "validation_context_field_invalid",
                    f"validation_context.{field_name} must be a list.",
                    f"validation_context.{field_name}",
                )
            )

    context_ids: dict[str, set[str]] = {}
    for field_name in ("components", "environments", "evidence"):
        ids: set[str] = set()
        records = context_mapping.get(field_name)
        if _is_sequence(records):
            for index, record in enumerate(cast(Sequence[object], records)):
                record_path = f"validation_context.{field_name}[{index}]"
                if not isinstance(record, Mapping):
                    issues.append(
                        ValidationModelIssue(
                            "validation_context_record_invalid",
                            f"{field_name} entries must be mappings.",
                            record_path,
                        )
                    )
                    continue
                record_id = record.get("id")
                if not isinstance(record_id, str) or not record_id.strip():
                    issues.append(
                        ValidationModelIssue(
                            "validation_context_record_id_missing",
                            f"{field_name} entries need a non-empty id.",
                            f"{record_path}.id",
                        )
                    )
                    continue
                if record_id in ids:
                    issues.append(
                        ValidationModelIssue(
                            "validation_context_record_id_duplicate",
                            f"Duplicate {field_name} id {record_id!r}.",
                            f"{record_path}.id",
                        )
                    )
                ids.add(record_id)
        context_ids[field_name] = ids

    check_ids: set[str] = set()
    checks: list[ValidationCheck] = []
    for index, item in enumerate(cast(Sequence[object], inventory)):
        path = f"validation_inventory[{index}]"
        if not isinstance(item, Mapping):
            issues.append(
                ValidationModelIssue(
                    "validation_inventory_entry_invalid",
                    "Validation inventory entries must be mappings.",
                    path,
                )
            )
            continue
        try:
            check = ValidationCheck.from_data(item)
        except (TypeError, ValueError) as error:
            issues.append(
                ValidationModelIssue(
                    "validation_inventory_entry_invalid", str(error), path
                )
            )
            continue
        checks.append(check)
        if check.id in check_ids:
            issues.append(
                ValidationModelIssue(
                    "validation_inventory_id_duplicate",
                    f"Duplicate validation id {check.id!r}.",
                    f"{path}.id",
                )
            )
        check_ids.add(check.id)
        if check.schema_version == VALIDATION_INVENTORY_SCHEMA_VERSION:
            issues.extend(_validate_v2_check(check, path))
            if check.component and check.component not in context_ids["components"]:
                issues.append(
                    ValidationModelIssue(
                        "validation_component_reference_invalid",
                        f"Unknown component {check.component!r}.",
                        f"{path}.component",
                    )
                )
            if (
                check.environment
                and check.environment not in context_ids["environments"]
            ):
                issues.append(
                    ValidationModelIssue(
                        "validation_environment_reference_invalid",
                        f"Unknown environment {check.environment!r}.",
                        f"{path}.environment",
                    )
                )
            for evidence_id in _evidence_references(check):
                if evidence_id not in context_ids["evidence"]:
                    issues.append(
                        ValidationModelIssue(
                            "validation_evidence_reference_invalid",
                            f"Unknown evidence id {evidence_id!r}.",
                            f"{path}.provenance.evidence",
                        )
                    )
    issues.extend(_validate_dependency_graph(checks))
    return tuple(issues)


def _validate_v2_check(check: ValidationCheck, path: str) -> list[ValidationModelIssue]:
    issues: list[ValidationModelIssue] = []
    execution = check.execution
    kind = execution.get("kind")
    if kind not in _EXECUTION_KINDS:
        issues.append(
            _issue(
                "execution_kind_invalid",
                "Unknown execution kind.",
                path + ".execution.kind",
            )
        )
    cwd = execution.get("cwd")
    if (
        not isinstance(cwd, str)
        or not cwd
        or cwd.startswith("/")
        or ".." in cwd.split("/")
    ):
        issues.append(
            _issue(
                "execution_cwd_invalid",
                "cwd must be a non-empty repository-relative path.",
                path + ".execution.cwd",
            )
        )
    shell = execution.get("shell")
    script = execution.get("script")
    if kind == "argv" and (
        not check.command or shell is not None or script is not None
    ):
        issues.append(
            _issue(
                "execution_argv_shape_invalid",
                "argv execution needs a command and no shell or script.",
                path + ".execution",
            )
        )
    elif kind == "shell" and (
        check.command
        or not isinstance(shell, str)
        or not shell
        or not isinstance(script, str)
        or not script
    ):
        issues.append(
            _issue(
                "execution_shell_shape_invalid",
                "shell execution needs a shell and script and an empty command.",
                path + ".execution",
            )
        )
    elif kind == "unresolved" and check.command:
        issues.append(
            _issue(
                "execution_unresolved_shape_invalid",
                "unresolved execution must not expose an executable command.",
                path + ".command",
            )
        )
    if check.requiredness.get("status") not in _REQUIREDNESS:
        issues.append(
            _issue(
                "requiredness_invalid",
                "requiredness.status is invalid.",
                path + ".requiredness.status",
            )
        )
    if check.applicability.get("evaluation") not in _APPLICABILITY:
        issues.append(
            _issue(
                "applicability_invalid",
                "applicability.evaluation is invalid.",
                path + ".applicability.evaluation",
            )
        )
    level = check.confirmation.get("level")
    if level not in _CONFIRMATION_LEVELS:
        issues.append(
            _issue(
                "confirmation_level_invalid",
                "confirmation.level is invalid.",
                path + ".confirmation.level",
            )
        )
    observations = check.confirmation.get("observations")
    if not _is_sequence(observations):
        issues.append(
            _issue(
                "confirmation_observations_invalid",
                "confirmation.observations must be a list.",
                path + ".confirmation.observations",
            )
        )
    else:
        for index, observation in enumerate(cast(Sequence[object], observations)):
            observation_path = f"{path}.confirmation.observations[{index}]"
            if not isinstance(observation, Mapping):
                issues.append(
                    _issue(
                        "confirmation_observation_invalid",
                        "Confirmation observations must be mappings.",
                        observation_path,
                    )
                )
            elif (
                "status" in observation
                and observation.get("status") not in _CHECK_OUTCOMES
            ):
                issues.append(
                    _issue(
                        "confirmation_outcome_invalid",
                        "Observation status is invalid.",
                        f"{observation_path}.status",
                    )
                )
    declaration = check.provenance.get("declaration")
    if declaration not in _DECLARATION_KINDS:
        issues.append(
            _issue(
                "provenance_declaration_invalid",
                "provenance.declaration is invalid.",
                path + ".provenance.declaration",
            )
        )
    provenance_evidence = check.provenance.get("evidence")
    if not _is_sequence(provenance_evidence):
        issues.append(
            _issue(
                "provenance_evidence_invalid",
                "provenance.evidence must be a list.",
                path + ".provenance.evidence",
            )
        )
    elif any(
        not isinstance(reference, str) or not reference.strip()
        for reference in cast(Sequence[object], provenance_evidence)
    ):
        issues.append(
            _issue(
                "provenance_evidence_invalid",
                "provenance.evidence entries must be non-empty ids.",
                path + ".provenance.evidence",
            )
        )
    if (
        not isinstance(check.baseline.get("status"), str)
        or check.baseline.get("status") not in _CHECK_OUTCOMES
    ):
        issues.append(
            _issue(
                "baseline_status_invalid",
                "baseline.status is invalid.",
                path + ".baseline.status",
            )
        )
    for index, origin in enumerate(check.ci_origins):
        if not origin:
            issues.append(
                _issue(
                    "ci_origin_empty",
                    "CI origins must be non-empty mappings.",
                    f"{path}.ci_origins[{index}]",
                )
            )
    for field_name, values in (
        ("roles", check.roles),
        ("config_files", check.config_files),
        ("depends_on", check.depends_on),
        ("unresolved", check.unresolved),
    ):
        if any(not value.strip() for value in values):
            issues.append(
                _issue(
                    f"{field_name}_empty_value",
                    f"{field_name} entries must be non-empty strings.",
                    f"{path}.{field_name}",
                )
            )
    if check.environment is not None and not check.environment.strip():
        issues.append(
            _issue(
                "environment_id_empty",
                "environment must be null or a non-empty id.",
                path + ".environment",
            )
        )
    if check.settings_status not in _SETTINGS_STATUSES:
        issues.append(
            _issue(
                "settings_status_invalid",
                "settings_status is invalid.",
                path + ".settings_status",
            )
        )
    if check.local_reproducibility not in _REPRODUCIBILITY:
        issues.append(
            _issue(
                "local_reproducibility_invalid",
                "local_reproducibility is invalid.",
                path + ".local_reproducibility",
            )
        )
    return issues


def _evidence_references(check: ValidationCheck) -> tuple[str, ...]:
    references = check.provenance.get("evidence", [])
    result = (
        [value for value in references if isinstance(value, str)]
        if _is_sequence(references)
        else []
    )
    requiredness_evidence = check.requiredness.get("evidence", [])
    if _is_sequence(requiredness_evidence):
        result.extend(
            value for value in requiredness_evidence if isinstance(value, str)
        )
    return tuple(result)


def _validate_dependency_graph(
    checks: Sequence[ValidationCheck],
) -> list[ValidationModelIssue]:
    by_id = {check.id: check for check in checks}
    graph = {
        check.id: tuple(dep for dep in check.depends_on if dep in by_id)
        for check in checks
    }
    issues: list[ValidationModelIssue] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(check_id: str) -> None:
        if check_id in visited:
            return
        if check_id in visiting:
            issues.append(
                _issue(
                    "validation_dependency_cycle",
                    "Validation dependency graph contains a cycle.",
                    f"validation_inventory[{check_id}].depends_on",
                )
            )
            return
        visiting.add(check_id)
        for dependency in graph[check_id]:
            visit(dependency)
        visiting.remove(check_id)
        visited.add(check_id)

    for check_id in graph:
        visit(check_id)
    return issues


def _issue(code: str, message: str, path: str) -> ValidationModelIssue:
    return ValidationModelIssue(code, message, path)


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _optional_text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _string_tuple(value: object, key: str) -> tuple[str, ...]:
    if not _is_sequence(value) or not all(
        isinstance(item, str) and bool(item.strip())
        for item in cast(Sequence[object], value)
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(cast(Sequence[str], value))


def _mapping(value: object, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be a mapping")
    return value


def _mapping_tuple(value: object, key: str) -> tuple[Mapping[str, Any], ...]:
    if not _is_sequence(value) or not all(
        isinstance(item, Mapping) for item in cast(Sequence[object], value)
    ):
        raise ValueError(f"{key} must be a list of mappings")
    return tuple(cast(Sequence[Mapping[str, Any]], value))


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


__all__ = [
    "LEGACY_VALIDATION_INVENTORY_SCHEMA_VERSION",
    "VALIDATION_CONTEXT_SCHEMA_VERSION",
    "VALIDATION_INVENTORY_SCHEMA_VERSION",
    "ValidationCheck",
    "ValidationContext",
    "ValidationModelIssue",
    "validate_validation_records",
]
