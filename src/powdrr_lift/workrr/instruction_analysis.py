"""Read-only conversion of an instruction file into a Powdrr proposal packet."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.core.decision_obligation import content_fingerprint
from powdrr_lift.structrr.active_intent import resolve_active_intent
from powdrr_lift.structrr.bootstrap import bootstrap_structrr
from powdrr_lift.structrr.gate_compiler import (
    evaluate_structural_proposal_gate,
)
from powdrr_lift.structrr.proposal import compile_proposal_revision
from powdrr_lift.structrr.validation import discover_validation_profiles
from powdrr_lift.structrr.verification_obligations import (
    compile_verification_obligations,
)
from powdrr_lift.workrr.feature_endpoint import (
    _expand_provider_inventory,
    _mapping_values,
    _merge_contract_mappings,
    _plan_acceptance_references,
    _verification_contracts,
)
from powdrr_lift.workrr.llm import complete_json
from powdrr_lift.workrr.protocol import WorkflowLLMClient
from powdrr_lift.workrr.verification_provider import (
    default_verification_provider_registry,
)

ANALYSIS_SCHEMA_VERSION = "instruction-analysis-v1"


class InstructionAnalysisError(ValueError):
    """The instruction analysis could not produce a structurally valid packet."""


def analyze_instruction_file(
    instruction_file: str | Path,
    *,
    repo_root: str | Path,
    work_item_name: str,
    planning_client: WorkflowLLMClient,
    allowed_paths: Sequence[str] = (".",),
) -> dict[str, Any]:
    """Generate a proposal packet without changing the repository.

    The model supplies only a candidate Structrr plan. All intent resolution,
    proposal operations, verification selection, worklist generation, and
    fingerprints are compiled locally from that candidate.
    """
    root = Path(repo_root).resolve()
    source = Path(instruction_file).resolve()
    try:
        instruction = source.read_text(encoding="utf-8")
    except OSError as error:
        raise InstructionAnalysisError(f"could not read {source}: {error}") from error
    if not instruction.strip():
        raise InstructionAnalysisError("instruction file must not be empty")

    baseline, baseline_ref = _load_baseline(root)
    active_before = resolve_active_intent(root, baseline_document=baseline)
    response = complete_json(
        planning_client,
        _planning_messages(
            instruction=instruction,
            work_item_name=work_item_name,
            baseline=baseline,
            active_intent=tuple(item.to_data() for item in active_before),
        ),
        response_schema=_candidate_schema(),
    )
    plan = _candidate_plan(response, work_item_name, instruction)
    active_after = resolve_active_intent(
        root, baseline_document=baseline, feature_document=plan
    )
    proposal = compile_proposal_revision(
        _slug(work_item_name),
        baseline,
        plan,
        acceptance_criteria=_plan_texts(plan.get("acceptance_criteria")),
        must_preserve=_plan_texts(plan.get("must_preserve"))
        or tuple(item.statement for item in active_before),
        non_goals=_plan_texts(plan.get("non_goals")),
        allowed_paths=tuple(dict.fromkeys(str(item) for item in allowed_paths if item)),
        source_refs=(baseline_ref, f"instruction:{source}"),
    )
    profiles = discover_validation_profiles(root)
    provider_inventory = tuple(
        entry
        for item in default_verification_provider_registry().inventory(root, profiles)
        for entry in _expand_provider_inventory(item.to_data())
    )
    contracts = _verification_contracts(
        _merge_contract_mappings(
            _mapping_values(baseline.get("required_test_cases")),
            _mapping_values(plan.get("required_test_cases")),
        )
    )
    verification = compile_verification_obligations(
        proposal,
        active_intents=tuple(item.to_data() for item in active_after),
        contracts=contracts,
        provider_inventory=provider_inventory,
        anticipated_paths=_planned_paths(plan),
        relationships=tuple(
            _mapping_values(baseline.get("entity_relationships"))
            + _mapping_values(plan.get("entity_relationships"))
        ),
        previous_contracts=_verification_contracts(
            _mapping_values(baseline.get("required_test_cases"))
        ),
    )
    evidence = {
        "instruction": _sha256(instruction.encode("utf-8")),
        "baseline": content_fingerprint(baseline),
        "plan": content_fingerprint(plan),
        "active_intent_before": content_fingerprint(
            [item.to_data() for item in active_before]
        ),
        "active_intent_after": content_fingerprint(
            [item.to_data() for item in active_after]
        ),
    }
    worklist, structural_failures = evaluate_structural_proposal_gate(
        proposal,
        active_intent_clause_ids=tuple(item.clause_id for item in active_after),
        evidence_fingerprints=evidence,
        verification_compilation=verification,
    )
    obligations = _feature_obligations(plan, instruction)
    report: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "status": "proposal_only",
        "review_required": True,
        "implementation_authorized": False,
        "instruction": {
            "path": str(source),
            "fingerprint": evidence["instruction"],
            "text": instruction,
        },
        "baseline": {"source": baseline_ref, "fingerprint": evidence["baseline"]},
        "candidate_plan": plan,
        "active_intent": {
            "before": [item.to_data() for item in active_before],
            "after": [item.to_data() for item in active_after],
            "added_or_changed": _intent_delta(active_before, active_after),
        },
        "feature_obligations": obligations,
        "proposal_revision": proposal.to_data(),
        "verification_obligations": verification.to_data(),
        "validation_profiles": [
            {
                "name": profile.name,
                "command": list(profile.command),
                "source": profile.source,
            }
            for profile in profiles
        ],
        "evidence_fingerprints": evidence,
        "proposal_review": {
            "worklist": worklist.to_data(),
            "structural_failures": list(structural_failures),
            "passed": not structural_failures,
        },
    }
    report["fingerprint"] = content_fingerprint(report)
    return report


def prepare_instruction_context(
    instruction_file: str | Path,
    *,
    repo_root: str | Path,
    work_item_name: str,
) -> dict[str, Any]:
    """Load the immutable context supplied to the Procedrr planning step."""
    root = Path(repo_root).resolve()
    source = Path(instruction_file).resolve()
    try:
        instruction = source.read_text(encoding="utf-8")
    except OSError as error:
        raise InstructionAnalysisError(f"could not read {source}: {error}") from error
    if not instruction.strip():
        raise InstructionAnalysisError("instruction file must not be empty")
    baseline, baseline_ref = _load_baseline(root)
    active_intent = resolve_active_intent(root, baseline_document=baseline)
    return {
        "repo_root": str(root),
        "instruction_file": str(source),
        "instruction": instruction,
        "work_item_name": work_item_name,
        "baseline": baseline,
        "baseline_ref": baseline_ref,
        "active_intent": [item.to_data() for item in active_intent],
    }


def compile_instruction_analysis(
    context: Mapping[str, Any],
    planning_response: Mapping[str, Any],
    *,
    allowed_paths: Sequence[str] = (".",),
) -> dict[str, Any]:
    """Compile a Procedrr planning response without another model call."""
    required = ("instruction_file", "repo_root", "work_item_name")
    if not all(isinstance(context.get(key), str) for key in required):
        raise InstructionAnalysisError("analysis context is incomplete")
    return analyze_instruction_file(
        context["instruction_file"],
        repo_root=context["repo_root"],
        work_item_name=context["work_item_name"],
        planning_client=_StaticPlanningClient(planning_response),
        allowed_paths=allowed_paths,
    )


def run_instruction_analysis_flow(
    instruction_file: str | Path,
    *,
    repo_root: str | Path,
    work_item_name: str,
    planning_client: WorkflowLLMClient,
    allowed_paths: Sequence[str] = (".",),
) -> dict[str, Any]:
    """Run the read-only analysis as a bounded Procedrr process."""
    from powdrr_lift.workrr.procedrr import WorkrrProcedrrClient
    from procedrr import parse_and_validate
    from procedrr_evaluator import Evaluator

    root = Path(repo_root).resolve()
    skills_dir = root / "docs" / "procedrr" / "skill-definitions"
    flow_path = skills_dir / "analyze-instruction.yaml"
    if not flow_path.is_file():
        flow_path = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "procedrr"
            / "skill-definitions"
            / "analyze-instruction.yaml"
        )
    flow = parse_and_validate(flow_path.read_text(encoding="utf-8"))

    def execute(tool: str, parameters: Mapping[str, Any]) -> Any:
        if tool != "internal":
            raise InstructionAnalysisError(
                f"analyze-instruction requested unsupported tool {tool!r}"
            )
        command = parameters.get("command")
        if command == ["load_instruction_analysis_context"]:
            return prepare_instruction_context(
                str(parameters["instruction_file"]),
                repo_root=str(parameters["repo_root"]),
                work_item_name=str(parameters["work_item_name"]),
            )
        if command == ["compile_instruction_analysis"]:
            context = parameters.get("context")
            response = parameters.get("planning_response")
            paths = parameters.get("allowed_paths")
            if not isinstance(context, Mapping) or not isinstance(response, Mapping):
                raise InstructionAnalysisError(
                    "compile_instruction_analysis inputs are malformed"
                )
            if not isinstance(paths, list) or not all(
                isinstance(item, str) for item in paths
            ):
                raise InstructionAnalysisError(
                    "allowed_paths must be a list of strings"
                )
            return compile_instruction_analysis(
                context, response, allowed_paths=tuple(paths)
            )
        raise InstructionAnalysisError(
            f"unknown analyze-instruction operation: {command}"
        )

    client = WorkrrProcedrrClient(planning_client, skills_dir=skills_dir)
    result = Evaluator(
        client,
        execute,
        process_directory=skills_dir,
        judge_clients={"planning": client},
    ).evaluate(
        flow,
        {
            "instruction_file": str(Path(instruction_file).resolve()),
            "repo_root": str(root),
            "work_item_name": work_item_name,
            "allowed_paths": list(allowed_paths),
        },
    )
    analysis = result.bindings.get("analysis")
    if not isinstance(analysis, Mapping):
        raise InstructionAnalysisError("analyze-instruction flow produced no analysis")
    return dict(analysis)


class _StaticPlanningClient:
    """Adapter that lets the Procedrr result enter the deterministic compiler."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)

    def complete_json(self, messages: list[dict[str, str]], **_: Any) -> dict[str, Any]:
        del messages
        return self.response


def _planning_messages(
    *,
    instruction: str,
    work_item_name: str,
    baseline: Mapping[str, Any],
    active_intent: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the planning stage of Powdrr. Return JSON only. "
                "Create a candidate Structrr diff, not code and not prose. "
                "Every changed item must use action added, deleted, or removed. "
                "Every operation that affects intent must include a concise "
                "intent_effect string. Preserve existing intent unless the user "
                "explicitly changes it. Include acceptance criteria and required "
                "test cases when the instruction makes them inferable."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "work_item_name": work_item_name,
                    "instruction": instruction,
                    "current_active_intent": list(active_intent),
                    "current_structrr": baseline,
                    "required_output": "candidate_plan",
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
        },
    ]


def _candidate_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["plan"],
        "properties": {"plan": {"type": "object"}},
        "additionalProperties": True,
    }


def _candidate_plan(
    response: Mapping[str, Any], work_item_name: str, instruction: str
) -> dict[str, Any]:
    raw = response.get("plan")
    if not isinstance(raw, Mapping):
        raise InstructionAnalysisError(
            "planning response must contain an object named plan"
        )
    plan = dict(raw)
    slug = _slug(work_item_name)
    plan.setdefault("schema", "https://powdrr.io/schema/changelog-v2")
    plan.setdefault("change_id", slug)
    plan.setdefault("title", work_item_name)
    plan.setdefault(
        "intent",
        {
            "problem": "The requested behavior is not yet available.",
            "goal": instruction,
        },
    )
    plan.setdefault(
        "features", [{"id": slug, "description": instruction, "action": "added"}]
    )
    plan.setdefault(
        "acceptance_criteria",
        [{"id": "instruction", "description": instruction}],
    )
    for key in (
        "invariants",
        "guidance",
        "entities",
        "entity_relationships",
        "required_test_cases",
        "files",
        "non_goals",
        "must_preserve",
    ):
        value = plan.get(key)
        if value is None:
            plan[key] = []
        elif not isinstance(value, list):
            raise InstructionAnalysisError(
                f"candidate plan field {key!r} must be a list"
            )
    return plan


def _load_baseline(root: Path) -> tuple[dict[str, Any], str]:
    candidates = sorted(
        (root / "docs" / "structrr" / "current").glob("baseline-*.yaml")
    )
    if candidates:
        path = candidates[-1]
        return _load_yaml(path), f"structrr:{path.relative_to(root)}"
    # Bootstrap writes only to the temporary output path; source repositories
    # remain untouched.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="powdrr-analysis-") as directory:
        result = bootstrap_structrr(root, output_path=Path(directory) / "baseline.yaml")
        if not result.validation.successful:
            messages = "; ".join(issue.message for issue in result.validation.issues)
            raise InstructionAnalysisError(
                f"could not bootstrap Structrr baseline: {messages}"
            )
        return dict(result.document), "structrr:bootstrap"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise InstructionAnalysisError(
            f"could not read baseline {path}: {error}"
        ) from error
    if not isinstance(value, Mapping):
        raise InstructionAnalysisError(f"baseline {path} must contain a mapping")
    return dict(value)


def _feature_obligations(
    plan: Mapping[str, Any], instruction: str
) -> list[dict[str, Any]]:
    refs = _plan_acceptance_references(plan)
    if not refs:
        refs = ["acceptance_criteria.instruction"]
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?])\s+|\n+", instruction)
        if item.strip()
    ]
    return [
        {"id": f"sentence-{index}", "description": sentence, "plan_refs": refs}
        for index, sentence in enumerate(sentences, start=1)
    ]


def _planned_paths(plan: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item["path"])
        for item in _mapping_values(plan.get("files"))
        if isinstance(item.get("path"), str) and item["path"].strip()
    )


def _intent_delta(before: Sequence[Any], after: Sequence[Any]) -> list[dict[str, Any]]:
    old = {item.clause_id: item for item in before}
    new = {item.clause_id: item for item in after}
    return (
        [
            {"clause_id": key, "action": "added", "clause": new[key].to_data()}
            for key in sorted(new.keys() - old.keys())
        ]
        + [
            {"clause_id": key, "action": "removed", "clause": old[key].to_data()}
            for key in sorted(old.keys() - new.keys())
        ]
        + [
            {
                "clause_id": key,
                "action": "changed",
                "before": old[key].to_data(),
                "after": new[key].to_data(),
            }
            for key in sorted(old.keys() & new.keys())
            if old[key].to_data() != new[key].to_data()
        ]
    )


def _plan_texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        text = (
            item
            if isinstance(item, str)
            else item.get("description", item.get("text"))
            if isinstance(item, Mapping)
            else None
        )
        if isinstance(text, str) and text.strip():
            result.append(text.strip())
    return tuple(dict.fromkeys(result))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "instruction"


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "InstructionAnalysisError",
    "analyze_instruction_file",
    "compile_instruction_analysis",
    "prepare_instruction_context",
    "run_instruction_analysis_flow",
]
