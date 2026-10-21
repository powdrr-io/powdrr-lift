"""Generate and run instruction-derived post-run behavior probes."""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powdrr_lift.structrr.validation import DiscoveredValidationProfile
from powdrr_lift.workrr.llm import complete_json
from powdrr_lift.workrr.protocol import WorkflowLLMClient

POSTRUN_PROBE_SCHEMA_VERSION = "instruction-derived-probe-suite-v5"
_INTERACTION_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "interaction_id": {"type": "string"},
                    "scenario_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "condition": {"type": "string"},
                    "operation": {"type": "string"},
                    "expected": {"type": "string"},
                    "evidence_quote_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "interaction_id",
                    "scenario_ids",
                    "condition",
                    "operation",
                    "expected",
                ],
                # Missing evidence IDs must reach semantic validation so its
                # targeted repair can use the exact candidate list.
                "additionalProperties": False,
            },
        }
    },
    "required": ["interactions"],
    "additionalProperties": False,
}


def generate_probe_interaction_plan(
    client: WorkflowLLMClient,
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Plan source-grounded composition cases before probe code is authored."""
    scenario_fields = (
        "scenario_id",
        "given",
        "when",
        "then",
        "evidence",
        "related_requirements",
    )
    compact_scenarios = [
        {key: item[key] for key in scenario_fields if key in item} for item in scenarios
    ]
    evidence_candidates = _instruction_evidence_candidates(instruction)
    messages = [
        {
            "role": "system",
            "content": (
                "Analyze the instruction and behavior scenarios for interactions "
                "that a post-coding probe should exercise. Find combinations where "
                "one requirement defines values, states, or inputs and another "
                "defines an operation, precedence rule, resolution rule, or "
                "classification applied to them. Include only combinations that "
                "are entailed by the instruction; do not enumerate arbitrary pairs "
                "or invent behavior. Each interaction must cite at least two "
                "distinct instruction evidence candidates and refer to at least "
                "two scenario IDs. State a concrete condition, operation, and "
                "observable expected result. Choose at least two distinct "
                "evidence_quote_ids "
                "from the supplied exact instruction clauses; do not copy or "
                "rewrite quote text. For selection or classification behavior, make "
                "the result distinguish the included case from a relevant "
                "excluded case. Return an empty interactions list when no "
                "cross-requirement interaction is supported. Do not write code."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "instruction_evidence_candidates": evidence_candidates,
                    "behavior_scenarios": compact_scenarios,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    planned = _complete_valid_interaction_plan(
        client,
        messages,
        instruction=instruction,
        scenarios=scenarios,
        evidence_candidates=evidence_candidates,
        phase="initial interaction plan",
    )
    review_messages = [
        {
            "role": "system",
            "content": (
                "Audit the proposed interaction plan against the complete source "
                "instruction and behavior scenarios. Find any materially distinct "
                "cross-requirement combination that the plan omitted, especially "
                "where a special value is used with a grammar operation or where "
                "a composed result controls a later classification. Add only "
                "source-entailed interactions, using at least two distinct "
                "evidence_quote_ids from the supplied exact instruction clauses "
                "and at least two scenario IDs. Do not copy or rewrite quote text. "
                "Do not repeat an interaction already covered by the plan. Return "
                "an empty interactions list if the plan is complete. Do not write "
                "code or probes."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "instruction_evidence_candidates": evidence_candidates,
                    "behavior_scenarios": compact_scenarios,
                    "proposed_interactions": planned,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    additions = _complete_valid_interaction_plan(
        client,
        review_messages,
        instruction=instruction,
        scenarios=scenarios,
        evidence_candidates=evidence_candidates,
        phase="interaction-plan review",
    )
    by_id = {item["interaction_id"]: item for item in planned}
    for item in additions:
        by_id.setdefault(item["interaction_id"], item)
    return tuple(by_id.values())


def _complete_valid_interaction_plan(
    client: WorkflowLLMClient,
    messages: list[dict[str, str]],
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    evidence_candidates: Sequence[Mapping[str, str]],
    phase: str,
) -> tuple[dict[str, Any], ...]:
    """Validate each interaction and repair only invalid entries once."""
    response = complete_json(client, messages, response_schema=_INTERACTION_PLAN_SCHEMA)
    interactions = response.get("interactions")
    if not isinstance(interactions, list):
        raise ValueError(f"{phase} must contain an interactions array")

    valid, invalid = _validate_interaction_entries(
        interactions,
        instruction=instruction,
        scenarios=scenarios,
        evidence_candidates=evidence_candidates,
    )
    if not invalid:
        return tuple(item for _, item in valid)

    repair_messages = [
        {
            "role": "system",
            "content": (
                "Repair only the invalid interaction entries supplied below. "
                "Do not repeat or rewrite valid interactions. Preserve each "
                "existing interaction_id. Return only source-entailed entries. "
                "Each repaired entry needs two distinct evidence_quote_ids from "
                "instruction_evidence_candidates and two distinct scenario IDs. "
                "Do not invent IDs or copy quote text."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "instruction_evidence_candidates": list(evidence_candidates),
                    "behavior_scenarios": [dict(item) for item in scenarios],
                    "invalid_interactions": [
                        {
                            "index": index,
                            "interaction": raw,
                            "validation_error": error,
                        }
                        for index, raw, error in invalid
                    ],
                    "repair_phase": phase,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    repair_response = complete_json(
        client, repair_messages, response_schema=_INTERACTION_PLAN_SCHEMA
    )
    repaired = repair_response.get("interactions")
    if not isinstance(repaired, list) or len(repaired) != len(invalid):
        raise ValueError(
            f"{phase} repair must return exactly {len(invalid)} corrected "
            "interaction entries"
        )
    repaired_valid, repaired_invalid = _validate_interaction_entries(
        repaired,
        instruction=instruction,
        scenarios=scenarios,
        evidence_candidates=evidence_candidates,
    )
    if repaired_invalid:
        reasons = "; ".join(error for _, _, error in repaired_invalid)
        raise ValueError(f"{phase} repair remained invalid: {reasons}")

    by_position = dict(valid)
    valid_ids = {item["interaction_id"] for _, item in valid}
    for (position, original, _), (_, replacement) in zip(
        invalid, repaired_valid, strict=True
    ):
        original_id = (
            original.get("interaction_id") if isinstance(original, Mapping) else None
        )
        replacement_id = replacement["interaction_id"]
        if original_id and replacement_id != original_id:
            raise ValueError(
                f"{phase} repair changed interaction id {original_id!r} "
                f"to {replacement_id!r}"
            )
        if replacement_id in valid_ids:
            raise ValueError(
                f"{phase} repair duplicated interaction id {replacement_id!r}"
            )
        valid_ids.add(replacement_id)
        by_position[position] = replacement
    return tuple(by_position[index] for index in sorted(by_position))


def _validate_interaction_entries(
    interactions: Sequence[Any],
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    evidence_candidates: Sequence[Mapping[str, str]],
) -> tuple[
    list[tuple[int, dict[str, Any]]],
    list[tuple[int, Any, str]],
]:
    """Keep valid entries and report invalid entries with their original positions."""
    valid: list[tuple[int, dict[str, Any]]] = []
    invalid: list[tuple[int, Any, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(interactions):
        try:
            item = validate_probe_interaction_plan(
                [raw],
                instruction=instruction,
                scenarios=scenarios,
                evidence_candidates=evidence_candidates,
            )[0]
            interaction_id = item["interaction_id"]
            if interaction_id in seen:
                raise ValueError(f"duplicate interaction id {interaction_id!r}")
        except (ValueError, KeyError) as error:
            invalid.append((index, raw, str(error)))
        else:
            seen.add(interaction_id)
            valid.append((index, item))
    return valid, invalid


def validate_probe_interaction_plan(
    interactions: Sequence[Any],
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    evidence_candidates: Sequence[Mapping[str, str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Check references and materialize model-selected exact instruction quotes."""
    candidates = tuple(
        evidence_candidates or _instruction_evidence_candidates(instruction)
    )
    candidate_quotes = {str(item["id"]): str(item["quote"]) for item in candidates}
    known_scenarios = {str(item["scenario_id"]) for item in scenarios}
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in interactions:
        if not isinstance(raw, Mapping):
            raise ValueError("interaction plan contains a malformed entry")
        item = dict(raw)
        interaction_id = _required_text(item, "interaction_id")
        if interaction_id in seen:
            raise ValueError(f"duplicate interaction id {interaction_id!r}")
        scenario_ids = item.get("scenario_ids")
        if (
            not isinstance(scenario_ids, list)
            or any(not isinstance(value, str) for value in scenario_ids)
            or len(scenario_ids) < 2
            or len(set(scenario_ids)) != len(scenario_ids)
        ):
            raise ValueError(
                f"interaction {interaction_id!r} must reference at least two "
                "distinct scenarios"
            )
        unknown = set(scenario_ids) - known_scenarios
        if unknown:
            raise ValueError(
                f"interaction {interaction_id!r} refers to unknown scenarios: "
                + ", ".join(sorted(unknown))
            )
        for key in ("condition", "operation", "expected"):
            _required_text(item, key)
        quote_ids = item.get("evidence_quote_ids")
        if (
            not isinstance(quote_ids, list)
            or any(
                not isinstance(quote_id, str) or quote_id not in candidate_quotes
                for quote_id in quote_ids
            )
            or len(set(quote_ids)) < 2
        ):
            raise ValueError(
                f"interaction {interaction_id!r} must cite two distinct "
                "instruction evidence_quote_ids"
            )
        item.pop("evidence_quote_ids")
        item["evidence_quotes"] = [candidate_quotes[quote_id] for quote_id in quote_ids]
        seen.add(interaction_id)
        validated.append(item)
    return tuple(validated)


def _instruction_evidence_candidates(instruction: str) -> list[dict[str, str]]:
    """Split instruction text into exact, selectable evidence clauses."""
    clauses = re.split(r"(?<=[.!?;])(?:[ \t]+|\n+)|\n+", instruction)
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for clause in clauses:
        quote = clause.strip()
        if not quote or quote in seen:
            continue
        seen.add(quote)
        candidates.append({"id": f"quote-{len(candidates) + 1:03d}", "quote": quote})
    return candidates


@dataclass(frozen=True, slots=True)
class PostRunProbe:
    probe_id: str
    scenario_id: str
    oracle_basis: str
    test_source: str
    function_name: str
    obligation_id: str = ""
    obligation_category: str = "other"
    obligation_rule: str = "not_applicable"
    interaction_ids: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "oracle_basis": self.oracle_basis,
            "function_name": self.function_name,
            "test_source": self.test_source,
            "obligation_id": self.obligation_id,
            "obligation_category": self.obligation_category,
            "obligation_rule": self.obligation_rule,
            "interaction_ids": list(self.interaction_ids),
        }


@dataclass(frozen=True, slots=True)
class PostRunProbeResult:
    probe_id: str
    scenario_id: str
    status: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    output: str = ""
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "command": list(self.command),
            "returncode": self.returncode,
            "output": self.output,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ProbeInteractionPlan:
    """Result of planning cross-requirement cases, separate from suite status."""

    status: str
    interactions: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "interactions": list(self.interactions),
        }


def resolve_probe_interaction_plan(
    client: WorkflowLLMClient | None,
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
) -> ProbeInteractionPlan:
    """Return explicit empty, failed, or unconfigured interaction outcomes."""
    if client is None:
        return ProbeInteractionPlan("not_configured")
    try:
        interactions = generate_probe_interaction_plan(
            client, instruction=instruction, scenarios=scenarios
        )
    except Exception as error:
        return ProbeInteractionPlan("failed", error=f"{type(error).__name__}: {error}")
    return ProbeInteractionPlan(
        "generated" if interactions else "empty", interactions=interactions
    )


def parse_generated_postrun_probe_source(
    source: str,
    scenarios: Sequence[Mapping[str, Any]],
    *,
    instruction: str,
    repository_contract: Mapping[str, Any] | None = None,
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[PostRunProbe, ...],
]:
    """Validate MiniSWE's executable probes and Python-literal metadata."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("probe suite source is empty")
    try:
        tree = ast.parse(source)
        compile(tree, "<miniswe-postrun-probes>", "exec")
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"probe suite is not valid Python: {error}") from error

    functions = [
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name.startswith("test_probe_")
    ]
    module_metadata = _probe_module_metadata(tree)
    raw_obligations = module_metadata["OBLIGATIONS"]
    raw_interactions = module_metadata["INTERACTIONS"]
    raw_probe_metadata = module_metadata["PROBE_METADATA"]
    if not isinstance(raw_obligations, list):
        raise ValueError("OBLIGATIONS must be a Python list")
    if not isinstance(raw_interactions, list):
        raise ValueError("INTERACTIONS must be a Python list")
    if not isinstance(raw_probe_metadata, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, Mapping)
        for key, value in raw_probe_metadata.items()
    ):
        raise ValueError("PROBE_METADATA must map function names to Python dictionaries")
    interaction_obligations = validate_probe_interaction_plan(
        raw_interactions,
        instruction=instruction,
        scenarios=scenarios,
    )
    obligations = _validate_probe_obligations(
        raw_obligations,
        scenarios,
        instruction=instruction,
        repository_contract=repository_contract or {},
    )
    if not functions and any(item["supported"] is True for item in obligations):
        raise ValueError("probe suite must define probes for supported obligations")
    function_names = {item.name for item in functions}
    if set(raw_probe_metadata) != function_names:
        raise ValueError(
            "PROBE_METADATA function names must match the test_probe_* functions"
        )
    obligation_by_id = {item["obligation_id"]: item for item in obligations}

    known_scenarios = {str(item["scenario_id"]) for item in scenarios}
    seen_probe_ids: set[str] = set()
    covered_obligations: set[str] = set()
    known_interactions = {
        str(item["interaction_id"]) for item in interaction_obligations
    }
    covered_interactions: set[str] = set()
    probes: list[PostRunProbe] = []
    for function in functions:
        if isinstance(function, ast.AsyncFunctionDef):
            raise ValueError("probe functions must be synchronous")
        if (
            function.args.args
            or function.args.posonlyargs
            or function.args.kwonlyargs
            or function.args.vararg
            or function.args.kwarg
        ):
            raise ValueError(f"{function.name} must take no arguments")
        metadata = raw_probe_metadata[function.name]
        probe_id = _required_text(metadata, "probe_id")
        scenario_id = _required_text(metadata, "scenario_id")
        oracle_basis = _required_text(metadata, "oracle_basis")
        obligation_id = _required_text(metadata, "obligation_id")
        raw_interaction_ids = metadata.get("interaction_ids", [])
        if not isinstance(raw_interaction_ids, list) or any(
            not isinstance(value, str) for value in raw_interaction_ids
        ):
            raise ValueError(f"{function.name} has malformed interaction_ids")
        interaction_ids = tuple(dict.fromkeys(raw_interaction_ids))
        if probe_id in seen_probe_ids:
            raise ValueError(f"probe suite has duplicate probe id {probe_id!r}")
        if scenario_id not in known_scenarios:
            raise ValueError(f"probe refers to unknown scenario {scenario_id!r}")
        obligation = obligation_by_id.get(obligation_id)
        if obligation is None or obligation.get("supported") is not True:
            raise ValueError(
                f"probe refers to unsupported obligation {obligation_id!r}"
            )
        if obligation.get("scenario_id") != scenario_id:
            raise ValueError(
                f"probe {probe_id!r} does not match its obligation scenario"
            )
        unknown_interactions = set(interaction_ids) - known_interactions
        if unknown_interactions:
            raise ValueError(
                f"probe {probe_id!r} refers to unknown interactions: "
                + ", ".join(sorted(unknown_interactions))
            )
        for interaction_id in interaction_ids:
            interaction = next(
                item
                for item in interaction_obligations
                if item["interaction_id"] == interaction_id
            )
            if scenario_id not in interaction["scenario_ids"]:
                raise ValueError(
                    f"probe {probe_id!r} scenario is unrelated to interaction "
                    f"{interaction_id!r}"
                )
        seen_probe_ids.add(probe_id)
        covered_obligations.add(obligation_id)
        covered_interactions.update(interaction_ids)
        _validate_probe_function(function, tree)
        probes.append(
            PostRunProbe(
                probe_id,
                scenario_id,
                oracle_basis,
                source,
                function.name,
                obligation_id,
                str(obligation["category"]),
                str(obligation["rule"]),
                interaction_ids,
            )
        )
    missing_obligations = {
        str(item["obligation_id"]) for item in obligations if item["supported"] is True
    } - covered_obligations
    if missing_obligations:
        raise ValueError(
            "probe suite omitted supported obligations: "
            + ", ".join(sorted(missing_obligations))
        )
    missing_interactions = known_interactions - covered_interactions
    if missing_interactions:
        raise ValueError(
            "probe suite omitted required interactions: "
            + ", ".join(sorted(missing_interactions))
        )
    return obligations, interaction_obligations, tuple(probes)


def _probe_module_metadata(tree: ast.Module) -> dict[str, Any]:
    """Read only literal metadata assignments; never execute model-authored code."""
    metadata: dict[str, Any] = {}
    expected_names = {"OBLIGATIONS", "INTERACTIONS", "PROBE_METADATA"}
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(
            node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if isinstance(node, ast.Assign):
            if (
                len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)
                or node.targets[0].id not in expected_names
            ):
                raise ValueError(
                    "probe suite may assign only OBLIGATIONS, INTERACTIONS, "
                    "and PROBE_METADATA at module scope"
                )
            name = node.targets[0].id
            if name in metadata:
                raise ValueError(f"probe suite assigns {name} more than once")
            try:
                metadata[name] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as error:
                raise ValueError(
                    f"{name} must be a literal Python value (lists, dictionaries, "
                    "strings, numbers, and booleans only)"
                ) from error
            continue
        raise ValueError("probe suite has executable module-level statements")
    missing = expected_names - set(metadata)
    if missing:
        raise ValueError(
            "probe suite must define Python literals for: " + ", ".join(sorted(missing))
        )
    return metadata


def _validate_probe_obligations(
    raw_obligations: list[Any],
    scenarios: Sequence[Mapping[str, Any]],
    *,
    instruction: str,
    repository_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    category_values = {
        "transformation",
        "validation",
        "preservation",
        "lifecycle",
        "compatibility",
        "presence",
        "other",
    }
    rule_values = {
        "reference_integrity",
        "uniqueness",
        "collision",
        "mutual_exclusion",
        "type_constraint",
        "range_constraint",
        "presence",
        "format",
        "other",
        "not_applicable",
        "unresolved",
    }
    operand_roles = {
        "mapping_key",
        "mapping_value",
        "input",
        "result",
        "constraint",
        "trigger",
        "state",
        "other",
    }
    known_scenarios = {str(item["scenario_id"]) for item in scenarios}
    instruction_strings = (instruction,)
    contract_strings = tuple(_string_leaves(repository_contract))
    seen: set[str] = set()
    covered: set[str] = set()
    obligations: list[dict[str, Any]] = []
    for raw in raw_obligations:
        if not isinstance(raw, Mapping):
            raise ValueError("probe suite contains a malformed obligation")
        item = dict(raw)
        obligation_id = _required_text(item, "obligation_id")
        scenario_id = _required_text(item, "scenario_id")
        if obligation_id in seen:
            raise ValueError(f"duplicate obligation id {obligation_id!r}")
        if scenario_id not in known_scenarios:
            raise ValueError(f"obligation refers to unknown scenario {scenario_id!r}")
        if (
            item.get("category") not in category_values
            or item.get("rule") not in rule_values
        ):
            raise ValueError(f"obligation {obligation_id!r} has an invalid type")
        if not isinstance(item.get("supported"), bool):
            raise ValueError(f"obligation {obligation_id!r} has invalid support status")
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(f"obligation {obligation_id!r} has malformed evidence")
        for citation in evidence:
            if not isinstance(citation, Mapping):
                raise ValueError(f"obligation {obligation_id!r} has malformed evidence")
            source, quote = citation.get("source"), citation.get("quote")
            allowed_strings = (
                instruction_strings if source == "instruction" else contract_strings
            )
            if (
                source not in {"instruction", "repository_contract"}
                or not isinstance(quote, str)
                or not quote.strip()
                or not any(quote in value for value in allowed_strings)
            ):
                raise ValueError(f"obligation {obligation_id!r} cites absent evidence")
        if item["supported"]:
            if item["rule"] == "unresolved" or not evidence:
                raise ValueError(
                    f"supported obligation {obligation_id!r} is not grounded"
                )
            for key in ("summary", "condition", "operation", "expected"):
                _required_text(item, key)
            operands = item.get("operands")
            if not isinstance(operands, list):
                raise ValueError(f"obligation {obligation_id!r} has malformed operands")
            for operand in operands:
                if (
                    not isinstance(operand, Mapping)
                    or operand.get("role") not in operand_roles
                ):
                    raise ValueError(
                        f"obligation {obligation_id!r} has invalid operand roles"
                    )
                _required_text(operand, "name")
                _required_text(operand, "meaning")
        elif item["rule"] != "unresolved":
            raise ValueError(
                f"unsupported obligation {obligation_id!r} must be unresolved"
            )
        else:
            _required_text(item, "unresolved_reason")
        seen.add(obligation_id)
        covered.add(scenario_id)
        obligations.append(item)
    missing = known_scenarios - covered
    if missing:
        raise ValueError("probe suite omitted scenarios: " + ", ".join(sorted(missing)))
    return tuple(obligations)


def _probe_metadata(function: ast.FunctionDef) -> Mapping[str, Any]:
    docstring = ast.get_docstring(function, clean=False)
    if docstring is None:
        raise ValueError(f"{function.name} must have a JSON metadata docstring")
    try:
        metadata = json.loads(docstring)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{function.name} metadata docstring must be a JSON object: {error}"
        ) from error
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{function.name} metadata must be a JSON object")
    return metadata


def _validate_probe_function(function: ast.FunctionDef, tree: ast.Module) -> None:
    builtin_names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"open", "exec", "eval", "compile", "__import__"}:
                raise ValueError(f"probe suite uses disallowed function {node.func.id}")
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            raise ValueError("probe suite references __builtins__")
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
            _validate_import_roots(roots)
        if isinstance(node, ast.ImportFrom):
            _validate_import_roots({(node.module or "").split(".", 1)[0]})

    if not any(isinstance(node, ast.Assert) for node in ast.walk(function)):
        raise ValueError(f"{function.name} must contain an observable assertion")
    function_calls = (node for node in ast.walk(function) if isinstance(node, ast.Call))
    if not any(
        not isinstance(call.func, ast.Name) or call.func.id not in builtin_names
        for call in function_calls
    ):
        raise ValueError(f"{function.name} must exercise a non-built-in software call")


def _validate_import_roots(roots: set[str]) -> None:
    if "pytest" in roots:
        raise ValueError("probe suite must not depend on pytest")
    if roots & {
        "os",
        "socket",
        "subprocess",
        "shutil",
        "urllib",
        "requests",
        "tempfile",
        "importlib",
        "ctypes",
    }:
        raise ValueError("probe suite imports a disallowed system module")


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"probe metadata has an empty {key}")
    return item.strip()


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def run_postrun_probes(
    probes: Sequence[PostRunProbe],
    *,
    root: Path,
    profiles: Sequence[DiscoveredValidationProfile],
    timeout_seconds: float = 180.0,
) -> tuple[PostRunProbeResult, ...]:
    """Run frozen probes independently of the repository's test framework."""
    del profiles
    results: list[PostRunProbeResult] = []
    with tempfile.TemporaryDirectory(prefix="powdrr-probes-") as temporary:
        probe_root = Path(temporary)
        runner_path = probe_root / "run_probe.py"
        runner_path.write_text(_PROBE_RUNNER, encoding="utf-8")
        suites: dict[str, Path] = {}
        for probe in probes:
            suite_path = suites.get(probe.test_source)
            if suite_path is None:
                suite_path = probe_root / f"suite_{len(suites):03d}.py"
                suite_path.write_text(
                    probe.test_source.rstrip() + "\n", encoding="utf-8"
                )
                suites[probe.test_source] = suite_path
            command = (
                sys.executable,
                str(runner_path),
                str(suite_path),
                probe.function_name,
            )
            environment = os.environ.copy()
            environment.pop("VIRTUAL_ENV", None)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            existing_pythonpath = environment.get("PYTHONPATH", "")
            import_paths = [str(root), str(root / "src")]
            if existing_pythonpath:
                import_paths.append(existing_pythonpath)
            environment["PYTHONPATH"] = os.pathsep.join(import_paths)
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                results.append(
                    PostRunProbeResult(
                        probe.probe_id,
                        probe.scenario_id,
                        "timed_out",
                        command,
                        124,
                        _output(error.stdout, error.stderr),
                        "probe timed out",
                    )
                )
                continue
            except OSError as error:
                results.append(
                    PostRunProbeResult(
                        probe.probe_id,
                        probe.scenario_id,
                        "unavailable",
                        command,
                        error=str(error),
                    )
                )
                continue
            results.append(
                PostRunProbeResult(
                    probe.probe_id,
                    probe.scenario_id,
                    "passed" if completed.returncode == 0 else "failed",
                    command,
                    completed.returncode,
                    _output(completed.stdout, completed.stderr),
                )
            )
    return tuple(results)


def baseline_probe_failures(
    results: Sequence[PostRunProbeResult],
) -> list[str]:
    """Return probes that did not execute and fail through an assertion."""
    problems = []
    for result in results:
        if result.status != "failed" or "AssertionError" not in result.output:
            problems.append(
                f"{result.probe_id} ({result.scenario_id}) returned "
                f"{result.status}; expected an AssertionError. "
                f"{_truncate(result.output or result.error or '', 1600)}"
            )
    return problems


def probe_failures(results: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Convert failed probe results into focused coding-agent repair findings."""
    return [
        {
            "scenario_id": str(item.get("scenario_id", "unknown")),
            "reason": (
                f"probe {item.get('probe_id', 'unknown')} {item.get('status')}: "
                f"{_truncate(str(item.get('output') or item.get('error') or ''), 2400)}"
            ),
        }
        for item in results
        if item.get("status") in {"failed", "timed_out", "unavailable"}
    ]


_PROBE_RUNNER = """\
import importlib.util
import sys

path, function_name = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("powdrr_generated_probe", path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load generated probe: {path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
function = getattr(module, function_name, None)
if not callable(function):
    raise RuntimeError(f"generated probe function not found: {function_name}")
function()
"""


def _output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    chunks = [
        part.decode(errors="replace") if isinstance(part, bytes) else part
        for part in (stdout, stderr)
        if part
    ]
    return _truncate("\n".join(chunks), 8000)


def _truncate(value: str, limit: int) -> str:
    normalized = re.sub(r"\n{3,}", "\n\n", value).strip()
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


__all__ = [
    "POSTRUN_PROBE_SCHEMA_VERSION",
    "PostRunProbe",
    "PostRunProbeResult",
    "ProbeInteractionPlan",
    "baseline_probe_failures",
    "generate_probe_interaction_plan",
    "parse_generated_postrun_probe_source",
    "probe_failures",
    "resolve_probe_interaction_plan",
    "run_postrun_probes",
    "validate_probe_interaction_plan",
]
