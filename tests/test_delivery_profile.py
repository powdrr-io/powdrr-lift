from __future__ import annotations

import json
from pathlib import Path

from powdrr_lift.core.delivery_profile import (
    DELIVERY_PROFILE_SCHEMA_VERSION,
    DeliveryProfile,
    PersonaType,
    PhaseType,
    build_delivery_profile_validation_report,
    load_delivery_profile,
    validate_delivery_profile_yaml,
)
from powdrr_lift.core.workflow_task_specification import (
    TaskComplexity,
    TaskStatus,
    WorkflowTask,
    workflow_task_from_data,
)
from powdrr_lift.core.workflow_template_specification import (
    WorkflowTaskTemplate,
    WorkflowTemplate,
    workflow_template_from_data,
)

_REPO_ROOT = Path(__file__).parents[1]
_DEFAULT_PROFILE = _REPO_ROOT / "delivery-profiles" / "default-software-delivery.yaml"


def test_default_delivery_profile_is_valid_and_loadable() -> None:
    profile = load_delivery_profile(_DEFAULT_PROFILE)

    assert profile.schema_version == DELIVERY_PROFILE_SCHEMA_VERSION
    assert profile.profile_id == "default-software-delivery"
    assert {persona.persona_type for persona in profile.personas} == {
        PersonaType.ARCHITECT,
        PersonaType.ENGINEERING_MANAGER,
        PersonaType.ENGINEER,
        PersonaType.SPECIFICATION_REVIEWER,
        PersonaType.CODE_REVIEWER,
    }
    assert {phase.phase_type for phase in profile.phases} == set(PhaseType)


def test_delivery_profile_round_trips_yaml() -> None:
    profile = load_delivery_profile(_DEFAULT_PROFILE)
    reloaded = DeliveryProfile.from_yaml(profile.to_yaml())

    assert reloaded == profile


def test_delivery_profile_validation_rejects_unknown_fields() -> None:
    report = build_delivery_profile_validation_report(
        """
schema_version: delivery-profile-v1
profile_id: example
unexpected: true
personas: []
phases: []
"""
    )

    assert not report.validation_successful
    assert any(issue.code == "unknown_field" for issue in report.issues)


def test_delivery_profile_validation_rejects_unsafe_reviewer_assignment() -> None:
    content = _DEFAULT_PROFILE.read_text(encoding="utf-8").replace(
        "persona_id: code-reviewer\n"
        "    input_artifacts: [implementation, validation_evidence]",
        "persona_id: engineer\n"
        "    input_artifacts: [implementation, validation_evidence]",
        1,
    )

    report = build_delivery_profile_validation_report(content)

    assert not report.validation_successful
    assert any(issue.code == "invalid_phase_persona" for issue in report.issues)


def test_delivery_profile_validation_is_json() -> None:
    report = json.loads(validate_delivery_profile_yaml("not: [valid"))

    assert report["validation_successful"] is False
    assert report["issues"][0]["code"] == "invalid_yaml"


def test_workflow_task_phase_and_persona_are_optional_compatibility_fields() -> None:
    task = workflow_task_from_data(
        {
            "task_id": "task-001",
            "status": "open",
            "description": "Implement the change.",
            "complexity": "low",
            "input_state": {"request": "request"},
            "upstream_task_ids": [],
            "dependent_state": [],
            "output_state_type": "implementation",
            "assignee_type": "agent",
            "assignee_role": "coder",
            "phase_type": "build",
            "persona_id": "engineer",
        }
    )

    assert isinstance(task, WorkflowTask)
    assert task.phase_type is PhaseType.BUILD
    assert task.persona_id == "engineer"
    assert task.to_data()["phase_type"] == "build"


def test_old_workflow_task_data_still_has_no_profile_requirement() -> None:
    task = workflow_task_from_data(
        {
            "task_id": "task-001",
            "status": TaskStatus.OPEN.value,
            "description": "Existing task.",
            "complexity": TaskComplexity.LOW.value,
            "input_state": {"request": "request"},
            "upstream_task_ids": [],
            "dependent_state": [],
            "output_state_type": "state",
            "assignee_type": "agent",
            "assignee_role": "coder",
        }
    )

    assert task.phase_type is None
    assert task.persona_id is None


def test_workflow_template_round_trips_profile_references() -> None:
    template = WorkflowTemplate(
        when_to_use=("implement a change",),
        how_to_fill_this_out=("use the current proposed PR",),
        task_templates=(
            WorkflowTaskTemplate(
                description="Build the change.",
                complexity=TaskComplexity.LOW,
                input_state={"request": "request"},
                phase_type=PhaseType.BUILD,
                persona_id="engineer",
            ),
        ),
    )

    reloaded = workflow_template_from_data(template.to_data())

    assert reloaded.task_templates[0].phase_type is PhaseType.BUILD
    assert reloaded.task_templates[0].persona_id == "engineer"
