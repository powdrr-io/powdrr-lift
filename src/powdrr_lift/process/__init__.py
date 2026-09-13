"""The LLM-led process language.

This package owns skills, workflows, templates, tasks, steps, actions, effects,
outcomes, handoffs, and their static safety/liveness contracts. It compiles
source definitions into immutable contracts consumed by the agent and
execution kernel; it does not call providers or execute tools.

The current workflow-definition modules are being migrated here incrementally.
New process-language APIs belong in this package; this marker is intentionally
small until each implementation has a clean ownership boundary.
"""

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    name: "powdrr_lift.process.model"
    for name in (
        "SUPPORTED_INTERACTION_STYLES",
        "SUPPORTED_PROMPT_CATALOGS",
        "SUPPORTED_SKILL_TOOL_TYPES",
        "SUPPORTED_STEP_ACTIONS",
        "SUPPORTED_STEP_TYPES",
        "SUPPORTED_TOOL_INVOCATION_PACKAGES",
        "UNIVERSAL_STEP_ACTIONS",
        "CodingLoopSpec",
        "CodingLoopVerification",
        "Skill",
        "SkillDocument",
        "SkillStep",
        "SkillStepBranch",
        "SkillStepBranchCase",
        "SkillStepCompletion",
        "SkillStepGate",
        "SkillStepInput",
        "SkillStepOutput",
        "SkillStepPreStep",
        "SkillStepRequiredAction",
        "SkillToolInvocation",
        "SkillUsesSkill",
        "SkillUsesSkillBinding",
        "SkillValidationIssue",
        "SkillValidationReport",
        "build_skill_directory_validation_report",
        "build_skill_validation_report",
        "load_skill",
        "load_skills",
        "save_skill",
        "skill_from_data",
        "skill_from_json",
        "skill_from_yaml",
        "skill_step_from_data",
        "skill_to_json",
        "skill_to_yaml",
        "validate_skill_directory",
        "validate_skill_json",
        "validate_skill_json_file",
    )
}
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.tasks"
        for name in (
            "AgentRole",
            "AssigneeRole",
            "AssigneeType",
            "HumanRole",
            "ReadyWorkflowTask",
            "TaskComplexity",
            "TaskStatus",
            "WorkflowInstance",
            "WorkflowTask",
            "WorkflowTaskDocument",
            "WorkflowTaskValidationIssue",
            "WorkflowTaskValidationReport",
            "build_workflow_task_directory_validation_report",
            "build_workflow_task_validation_report",
            "load_ready_workflow_tasks",
            "load_workflow_task",
            "load_workflow_tasks",
            "save_workflow_task",
            "select_ready_workflow_tasks",
            "validate_assignee",
            "validate_workflow_task_directory",
            "validate_workflow_task_json",
            "validate_workflow_task_json_file",
            "validate_workflow_task_yaml",
            "validate_workflow_task_yaml_file",
            "workflow_task_from_data",
            "workflow_task_from_json",
            "workflow_task_from_yaml",
            "workflow_task_to_json",
            "workflow_task_to_yaml",
            "load_workflow_task_document",
            "load_workflow_task_documents",
            "save_workflow_task_document",
            "validate_workflow_task_directory_json",
            "workflow_task_document_from_data",
            "workflow_task_document_from_json",
            "workflow_task_document_from_yaml",
            "workflow_task_document_to_json",
            "workflow_task_document_to_yaml",
        )
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.templates"
        for name in (
            "WorkflowTaskTemplate",
            "WorkflowTaskTemplateGeneration",
            "WorkflowTemplate",
            "WorkflowTemplateDocument",
            "WorkflowTemplateValidationIssue",
            "WorkflowTemplateValidationReport",
            "build_workflow_template_validation_report",
            "instantiate_workflow_template",
            "instantiated_workflow_relationships",
            "load_workflow_template",
            "save_workflow_template",
            "validate_workflow_template_json",
            "validate_workflow_template_json_file",
            "workflow_template_from_data",
            "workflow_template_from_json",
            "workflow_template_from_yaml",
            "workflow_template_to_json",
            "workflow_template_to_yaml",
        )
    }
)
_EXPORT_MODULES.update(
    {
        "SkillCatalogEntry": "powdrr_lift.process.catalog",
        "WorkflowTemplateCatalogEntry": "powdrr_lift.process.catalog",
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.branching"
        for name in ("resolve_handoff_path", "select_branch_target")
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.step_behavior"
        for name in ("StepBehavior", "behavior_for_step")
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.action_catalog"
        for name in (
            "DEFAULT_ACTION_INSTRUCTIONS",
            "declared_action_names",
            "predicated_context_complete",
            "recovery_tool_invocations",
            "step_actions",
        )
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.liveness"
        for name in (
            "AbstractTransition",
            "AbstractWorkflowState",
            "CapabilityEffect",
            "SkillEffectSummary",
            "StepControlContract",
            "build_abstract_execution_graph",
            "capability_effect",
            "effect_for_pre_step",
            "effect_registry",
            "expand_abstract_transitions",
            "is_fixed_deterministic",
            "is_idempotent",
            "runtime_static_conformance",
            "step_control_contracts",
            "summarize_skill",
        )
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.discovery"
        for name in (
            "find_skill_by_name",
            "load_skill_catalog",
            "load_workflow_template_catalog",
        )
    }
)
_EXPORT_MODULES.update(
    {
        name: "powdrr_lift.process.compiler"
        for name in (
            "WorkflowDefinitionIssue",
            "WorkflowDefinitionReport",
            "WorkflowDefinitionsReport",
            "WorkflowIR",
            "WorkflowStepIR",
            "analyze_workflow_definition",
            "analyze_workflow_definitions",
            "apply_liveness_baseline",
            "apply_warning_budget",
            "compare_prompt_snapshot_contract",
            "discover_workflow_definitions",
            "instantiate_template_contract",
            "warning_counts",
            "warning_report_data",
        )
    }
)


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "SkillCatalogEntry",
    "WorkflowTemplateCatalogEntry",
    "SUPPORTED_INTERACTION_STYLES",
    "SUPPORTED_PROMPT_CATALOGS",
    "SUPPORTED_SKILL_TOOL_TYPES",
    "SUPPORTED_STEP_ACTIONS",
    "SUPPORTED_STEP_TYPES",
    "SUPPORTED_TOOL_INVOCATION_PACKAGES",
    "UNIVERSAL_STEP_ACTIONS",
    "CodingLoopSpec",
    "CodingLoopVerification",
    "Skill",
    "SkillDocument",
    "SkillStep",
    "SkillStepBranch",
    "SkillStepBranchCase",
    "SkillStepCompletion",
    "SkillStepGate",
    "SkillStepInput",
    "SkillStepOutput",
    "SkillStepPreStep",
    "SkillStepRequiredAction",
    "SkillToolInvocation",
    "SkillUsesSkill",
    "SkillUsesSkillBinding",
    "build_skill_directory_validation_report",
    "build_skill_validation_report",
    "load_skill",
    "load_skills",
    "save_skill",
    "skill_from_data",
    "skill_from_json",
    "skill_from_yaml",
    "skill_step_from_data",
    "skill_to_json",
    "skill_to_yaml",
    "validate_skill_directory",
    "validate_skill_json",
    "validate_skill_json_file",
    "AgentRole",
    "AssigneeRole",
    "AssigneeType",
    "HumanRole",
    "ReadyWorkflowTask",
    "TaskComplexity",
    "TaskStatus",
    "WorkflowInstance",
    "WorkflowTask",
    "WorkflowTaskValidationIssue",
    "WorkflowTaskValidationReport",
    "WorkflowTaskDocument",
    "build_workflow_task_directory_validation_report",
    "build_workflow_task_validation_report",
    "load_ready_workflow_tasks",
    "load_workflow_task",
    "load_workflow_tasks",
    "save_workflow_task",
    "select_ready_workflow_tasks",
    "validate_assignee",
    "validate_workflow_task_directory",
    "validate_workflow_task_json",
    "validate_workflow_task_json_file",
    "validate_workflow_task_yaml",
    "validate_workflow_task_yaml_file",
    "workflow_task_from_data",
    "workflow_task_from_json",
    "workflow_task_from_yaml",
    "workflow_task_to_json",
    "workflow_task_to_yaml",
    "load_workflow_task_document",
    "load_workflow_task_documents",
    "save_workflow_task_document",
    "validate_workflow_task_directory_json",
    "workflow_task_document_from_data",
    "workflow_task_document_from_json",
    "workflow_task_document_from_yaml",
    "workflow_task_document_to_json",
    "workflow_task_document_to_yaml",
    "WorkflowTaskTemplate",
    "WorkflowTaskTemplateGeneration",
    "WorkflowTemplate",
    "WorkflowTemplateDocument",
    "WorkflowTemplateValidationIssue",
    "WorkflowTemplateValidationReport",
    "build_workflow_template_validation_report",
    "instantiate_workflow_template",
    "instantiated_workflow_relationships",
    "load_workflow_template",
    "save_workflow_template",
    "validate_workflow_template_json",
    "validate_workflow_template_json_file",
    "workflow_template_from_data",
    "workflow_template_from_json",
    "workflow_template_from_yaml",
    "workflow_template_to_json",
    "workflow_template_to_yaml",
    "resolve_handoff_path",
    "select_branch_target",
    "StepBehavior",
    "behavior_for_step",
    "DEFAULT_ACTION_INSTRUCTIONS",
    "declared_action_names",
    "predicated_context_complete",
    "recovery_tool_invocations",
    "step_actions",
    "AbstractTransition",
    "AbstractWorkflowState",
    "CapabilityEffect",
    "SkillEffectSummary",
    "StepControlContract",
    "build_abstract_execution_graph",
    "capability_effect",
    "effect_for_pre_step",
    "effect_registry",
    "expand_abstract_transitions",
    "is_fixed_deterministic",
    "is_idempotent",
    "runtime_static_conformance",
    "step_control_contracts",
    "summarize_skill",
    "find_skill_by_name",
    "load_skill_catalog",
    "load_workflow_template_catalog",
    "WorkflowDefinitionIssue",
    "WorkflowDefinitionReport",
    "WorkflowDefinitionsReport",
    "WorkflowIR",
    "WorkflowStepIR",
    "analyze_workflow_definition",
    "analyze_workflow_definitions",
    "apply_liveness_baseline",
    "apply_warning_budget",
    "compare_prompt_snapshot_contract",
    "discover_workflow_definitions",
    "instantiate_template_contract",
    "warning_counts",
    "warning_report_data",
]
