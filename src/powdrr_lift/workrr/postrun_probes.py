"""Generate and run instruction-derived post-run behavior probes."""

from __future__ import annotations

import ast
import builtins
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
from powdrr_lift.workrr.test_obligations import missing_obligation_ids

POSTRUN_PROBE_SCHEMA_VERSION = "instruction-derived-probe-suite-v7"


def validate_probe_interaction_plan(
    interactions: Sequence[Any],
    *,
    instruction: str,
    scenarios: Sequence[Mapping[str, Any]],
    repository_contract: Mapping[str, Any] | None = None,
    instruction_ledger_clauses: Sequence[Mapping[str, Any]] = (),
    rejected_interactions: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Check references and bind evidence from each scenario deterministically."""
    scenarios_by_id = {str(item["scenario_id"]): item for item in scenarios}
    known_scenarios = set(scenarios_by_id)
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in interactions:
        try:
            validated_item = _validate_probe_interaction(
                raw,
                scenarios_by_id=scenarios_by_id,
                known_scenarios=known_scenarios,
                instruction=instruction,
                repository_contract=repository_contract or {},
                instruction_ledger_clauses=instruction_ledger_clauses,
                seen=seen,
            )
        except ValueError as error:
            if rejected_interactions is None:
                raise
            interaction_id = (
                raw.get("interaction_id") if isinstance(raw, Mapping) else None
            )
            rejected_interactions.append(
                {
                    "interaction_id": (
                        interaction_id if isinstance(interaction_id, str) else ""
                    ),
                    "error": str(error),
                }
            )
            continue
        seen.add(validated_item["interaction_id"])
        validated.append(validated_item)
    return tuple(validated)


def _validate_probe_interaction(
    raw: Any,
    *,
    scenarios_by_id: Mapping[str, Mapping[str, Any]],
    known_scenarios: set[str],
    instruction: str,
    repository_contract: Mapping[str, Any],
    instruction_ledger_clauses: Sequence[Mapping[str, Any]],
    seen: set[str],
) -> dict[str, Any]:
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
    item.pop("evidence_quote_ids", None)
    item.pop("evidence_quotes", None)
    evidence_quotes: list[str] = []
    for scenario_id in scenario_ids:
        evidence = _resolve_scenario_evidence(
            scenarios_by_id[scenario_id].get("evidence", ()),
            scenario_id=scenario_id,
            instruction=instruction,
            repository_contract=repository_contract,
            instruction_ledger_clauses=instruction_ledger_clauses,
        )
        quote = next(
            (
                citation["quote"]
                for citation in evidence
                if citation["quote"] not in evidence_quotes
            ),
            None,
        )
        if quote is not None:
            evidence_quotes.append(quote)
    if len(evidence_quotes) < 2:
        raise ValueError(
            f"interaction {interaction_id!r} cannot resolve two distinct "
            "source quotes from its scenarios"
        )
    item["evidence_quotes"] = evidence_quotes
    return item


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
    execution_path: str = "sync"

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
            "execution_path": self.execution_path,
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


def parse_generated_postrun_probe_source(
    source: str,
    scenarios: Sequence[Mapping[str, Any]],
    *,
    instruction: str,
    repository_contract: Mapping[str, Any] | None = None,
    instruction_ledger_clauses: Sequence[Mapping[str, Any]] = (),
    rejected_interactions: list[dict[str, str]] | None = None,
    rejected_probes: list[dict[str, str]] | None = None,
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
        raise ValueError(
            "PROBE_METADATA must map function names to Python dictionaries"
        )
    interaction_obligations = validate_probe_interaction_plan(
        raw_interactions,
        instruction=instruction,
        scenarios=scenarios,
        repository_contract=repository_contract or {},
        instruction_ledger_clauses=instruction_ledger_clauses,
        rejected_interactions=rejected_interactions,
    )
    obligations = _validate_probe_obligations(
        raw_obligations,
        scenarios,
        instruction=instruction,
        repository_contract=repository_contract or {},
        instruction_ledger_clauses=instruction_ledger_clauses,
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
    covered_execution_paths: dict[str, set[str]] = {}
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
        execution_path = metadata.get("execution_path", "sync")
        if not isinstance(execution_path, str) or execution_path not in {
            "sync",
            "async",
        }:
            raise ValueError(
                f"{function.name} execution_path must be 'sync' or 'async'"
            )
        raw_interaction_ids = metadata.get("interaction_ids", [])
        if not isinstance(raw_interaction_ids, list) or any(
            not isinstance(value, str) for value in raw_interaction_ids
        ):
            raise ValueError(f"{function.name} has malformed interaction_ids")
        rejected_ids = {
            item["interaction_id"]
            for item in rejected_interactions or ()
            if item.get("interaction_id")
        }
        interaction_ids = tuple(
            value
            for value in dict.fromkeys(raw_interaction_ids)
            if value not in rejected_ids
        )
        if probe_id in seen_probe_ids:
            raise ValueError(f"probe suite has duplicate probe id {probe_id!r}")
        seen_probe_ids.add(probe_id)
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
        invalid_interactions: set[str] = set()
        for interaction_id in interaction_ids:
            if interaction_id not in known_interactions:
                error_message = (
                    f"probe {probe_id!r} refers to unknown interaction "
                    f"{interaction_id!r}"
                )
                if rejected_interactions is None:
                    raise ValueError(error_message)
                rejected_interactions.append(
                    {
                        "interaction_id": interaction_id,
                        "probe_id": probe_id,
                        "error": error_message,
                    }
                )
                invalid_interactions.add(interaction_id)
                continue
            interaction = next(
                item
                for item in interaction_obligations
                if item["interaction_id"] == interaction_id
            )
            if scenario_id not in interaction["scenario_ids"]:
                error_message = (
                    f"probe {probe_id!r} scenario is unrelated to interaction "
                    f"{interaction_id!r}"
                )
                if rejected_interactions is None:
                    raise ValueError(error_message)
                rejected_interactions.append(
                    {
                        "interaction_id": interaction_id,
                        "probe_id": probe_id,
                        "error": error_message,
                    }
                )
                invalid_interactions.add(interaction_id)
        interaction_ids = tuple(
            interaction_id
            for interaction_id in interaction_ids
            if interaction_id not in invalid_interactions
        )
        try:
            _validate_probe_function(function, tree, execution_path=execution_path)
        except ValueError as error:
            if rejected_probes is None:
                raise
            rejected_probes.append(
                {
                    "probe_id": probe_id,
                    "scenario_id": scenario_id,
                    "obligation_id": obligation_id,
                    "error": str(error),
                }
            )
            continue
        covered_obligations.add(obligation_id)
        covered_execution_paths.setdefault(obligation_id, set()).add(execution_path)
        covered_interactions.update(interaction_ids)
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
                execution_path,
            )
        )
    missing_obligations = missing_obligation_ids(
        [
            str(item["obligation_id"])
            for item in obligations
            if item["supported"] is True
        ],
        covered_obligations,
    )
    if missing_obligations:
        if rejected_probes is None:
            raise ValueError(
                "probe suite omitted supported obligations: "
                + ", ".join(sorted(missing_obligations))
            )
        scenarios_by_obligation = {
            str(item["obligation_id"]): str(item["scenario_id"]) for item in obligations
        }
        rejected_obligation_ids = {
            item.get("obligation_id") for item in rejected_probes
        }
        rejected_probes.extend(
            {
                "probe_id": "",
                "scenario_id": scenarios_by_obligation[obligation_id],
                "obligation_id": obligation_id,
                "error": (f"supported obligation {obligation_id!r} has no valid probe"),
            }
            for obligation_id in sorted(
                set(missing_obligations) - rejected_obligation_ids
            )
        )
    if _repository_has_paired_sync_async_engines(repository_contract):
        for obligation in obligations:
            if (
                obligation.get("supported") is not True
                or obligation.get("category") == "validation"
            ):
                continue
            obligation_id = str(obligation["obligation_id"])
            covered_paths = covered_execution_paths.get(obligation_id, set())
            for missing_path in sorted({"sync", "async"} - covered_paths):
                if missing_path == "async":
                    message = (
                        f"supported obligation {obligation_id!r} needs an async-path "
                        "probe because the repository exposes paired sync and async "
                        "engines; author a synchronous wrapper that exercises the "
                        "async engine with asyncio.run"
                    )
                else:
                    message = (
                        f"supported obligation {obligation_id!r} needs a sync-path "
                        "probe because the repository exposes paired sync and async "
                        "engines"
                    )
                if rejected_probes is None:
                    raise ValueError(message)
                if not any(
                    item.get("obligation_id") == obligation_id
                    and message in item.get("error", "")
                    for item in rejected_probes
                ):
                    rejected_probes.append(
                        {
                            "probe_id": "",
                            "scenario_id": str(obligation["scenario_id"]),
                            "obligation_id": obligation_id,
                            "error": message,
                        }
                    )
    if not probes and any(item["supported"] is True for item in obligations):
        rejected_details = "; ".join(
            f"{item.get('probe_id') or item.get('obligation_id')}: {item['error']}"
            for item in rejected_probes or ()
            if item.get("error")
        )
        detail_suffix = f": {rejected_details}" if rejected_details else ""
        raise ValueError(
            f"probe suite contains no valid probes after isolation{detail_suffix}"
        )
    missing_interactions = known_interactions - covered_interactions
    if missing_interactions:
        if rejected_interactions is None:
            raise ValueError(
                "probe suite omitted required interactions: "
                + ", ".join(sorted(missing_interactions))
            )
        already_reported = {
            item.get("interaction_id") for item in rejected_interactions
        }
        rejected_interactions.extend(
            {
                "interaction_id": interaction_id,
                "error": "no valid probe covers this interaction",
            }
            for interaction_id in sorted(missing_interactions - already_reported)
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


def _resolve_scenario_evidence(
    raw_evidence: Any,
    *,
    scenario_id: str,
    instruction: str,
    repository_contract: Mapping[str, Any],
    instruction_ledger_clauses: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Bind scenario evidence to exact source text without model-authored citations."""
    if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes)):
        return []
    contract_strings = _string_leaves(repository_contract)
    citations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    clause_id = scenario_id.removeprefix("scenario:")
    ledger_quote = next(
        (
            str(clause["text"]).strip()
            for clause in instruction_ledger_clauses
            if clause.get("clause_id") == clause_id
            and isinstance(clause.get("text"), str)
            and str(clause["text"]).strip() in instruction
        ),
        None,
    )
    if ledger_quote is None:
        ledger_clause = next(
            (
                clause
                for clause in instruction_ledger_clauses
                if clause.get("clause_id") == clause_id
            ),
            None,
        )
        source_span = (
            ledger_clause.get("source_span")
            if isinstance(ledger_clause, Mapping)
            else None
        )
        if isinstance(source_span, Mapping):
            start = source_span.get("start")
            end = source_span.get("end")
            if (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and 0 <= start < end <= len(instruction)
            ):
                ledger_quote = instruction[start:end].strip() or None
    if ledger_quote is not None:
        citations.append({"source": "instruction", "quote": ledger_quote})
        seen.add(("instruction", ledger_quote))
    for raw_quote in raw_evidence:
        if not isinstance(raw_quote, str) or not raw_quote.strip():
            continue
        quote = raw_quote.strip()
        if quote in instruction:
            citation = ("instruction", quote)
        elif any(quote in value for value in contract_strings):
            citation = ("repository_contract", quote)
        else:
            continue
        if citation in seen:
            continue
        seen.add(citation)
        citations.append({"source": citation[0], "quote": citation[1]})
    return citations


def _validate_probe_obligations(
    raw_obligations: list[Any],
    scenarios: Sequence[Mapping[str, Any]],
    *,
    instruction: str,
    repository_contract: Mapping[str, Any],
    instruction_ledger_clauses: Sequence[Mapping[str, Any]],
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
        item.pop("evidence", None)
        item.pop("evidence_ids", None)
        scenario = next(
            scenario
            for scenario in scenarios
            if str(scenario["scenario_id"]) == scenario_id
        )
        evidence = _resolve_scenario_evidence(
            scenario.get("evidence", ()),
            scenario_id=scenario_id,
            instruction=instruction,
            repository_contract=repository_contract,
            instruction_ledger_clauses=instruction_ledger_clauses,
        )
        item["evidence"] = evidence
        if item["supported"]:
            if item["rule"] == "unresolved" or not evidence:
                raise ValueError(
                    f"supported obligation {obligation_id!r} has no scenario evidence "
                    "that resolves to exact instruction or repository-contract text"
                )
            for key in ("summary", "condition", "operation", "expected"):
                _required_text(item, key)
        elif item["rule"] != "unresolved":
            raise ValueError(
                f"unsupported obligation {obligation_id!r} must be unresolved"
            )
        else:
            _required_text(item, "unresolved_reason")
        item["operands"] = _normalize_obligation_operands(
            item.get("operands"), operand_roles
        )
        seen.add(obligation_id)
        covered.add(scenario_id)
        obligations.append(item)
    missing = missing_obligation_ids(sorted(known_scenarios), covered)
    if missing:
        raise ValueError("probe suite omitted scenarios: " + ", ".join(sorted(missing)))
    return tuple(obligations)


def _normalize_obligation_operands(
    value: Any, valid_roles: set[str]
) -> list[dict[str, str]]:
    """Keep usable descriptive operand fields and drop malformed model output."""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    for operand in value:
        if not isinstance(operand, Mapping):
            continue
        role = operand.get("role")
        name = operand.get("name")
        if (
            not isinstance(role, str)
            or role not in valid_roles
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        item = {"role": role, "name": name.strip()}
        meaning = operand.get("meaning")
        if isinstance(meaning, str) and meaning.strip():
            item["meaning"] = meaning.strip()
        normalized.append(item)
    return normalized


def _validate_probe_function(
    function: ast.FunctionDef,
    tree: ast.Module,
    *,
    execution_path: str = "sync",
) -> None:
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
    async_helpers = {
        node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }
    async_runner_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "run"
    ]
    if execution_path == "async" and not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in async_helpers
        and _async_helper_awaits_public_call(async_helpers[node.func.id])
        for runner_call in async_runner_calls
        for node in runner_call.args[:1]
    ):
        raise ValueError(
            f"{function.name} is marked async but must use asyncio.run in its "
            "synchronous wrapper on a local async helper that awaits a public "
            "operation"
        )


def _async_helper_awaits_public_call(function: ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        called = node.value.func
        if (
            isinstance(called, ast.Attribute)
            and isinstance(called.value, ast.Name)
            and called.value.id == "asyncio"
        ):
            continue
        return True
    return False


def _repository_has_paired_sync_async_engines(
    repository_contract: Mapping[str, Any] | None,
) -> bool:
    """Detect explicit AsyncX/SyncX engine pairs in repository evidence."""
    if not isinstance(repository_contract, Mapping):
        return False
    records = repository_contract.get("records")
    if not isinstance(records, list):
        return False
    async_families: set[str] = set()
    sync_families: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or record.get("kind") != "class":
            continue
        path = record.get("path")
        if isinstance(path, str) and any(
            part.casefold() in {"test", "tests"}
            for part in path.replace("\\", "/").split("/")[:-1]
        ):
            continue
        name = record.get("name")
        if not isinstance(name, str):
            continue
        folded = name.casefold()
        if folded.startswith("async") and len(folded) > len("async"):
            async_families.add(folded[len("async") :])
        elif folded.startswith("sync") and len(folded) > len("sync"):
            sync_families.add(folded[len("sync") :])
    return bool(async_families & sync_families)


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
    """Return probes that passed or failed for an unexpected baseline reason."""
    problems = []
    for result in results:
        if not baseline_probe_is_expected_failure(result):
            problems.append(
                f"{result.probe_id} ({result.scenario_id}) returned "
                f"{result.status}; expected a baseline failure through "
                "AssertionError, TypeError, or ImportError. "
                f"{_truncate(result.output or result.error or '', 1600)}"
            )
    return problems


def baseline_probe_is_expected_failure(result: PostRunProbeResult) -> bool:
    """Recognize baseline exceptions that mean the requested API is absent."""
    if result.status != "failed":
        return False
    terminal_line = next(
        (line.strip() for line in reversed(result.output.splitlines()) if line.strip()),
        "",
    )
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)(?::|$)", terminal_line)
    if match is None:
        return False
    exception_name = match.group(1).rsplit(".", 1)[-1]
    return exception_name in {
        "AssertionError",
        "ImportError",
        "ModuleNotFoundError",
        "TypeError",
    }


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
    "baseline_probe_failures",
    "baseline_probe_is_expected_failure",
    "parse_generated_postrun_probe_source",
    "probe_failures",
    "run_postrun_probes",
    "validate_probe_interaction_plan",
]
