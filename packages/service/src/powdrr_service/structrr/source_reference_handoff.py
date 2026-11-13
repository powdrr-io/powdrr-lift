"""Compile accepted placement intent into one prompt and a private validator input.

The caller establishes design acceptance and catalog authority. This compiler
never inspects a candidate diff or infers mandatory references from source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from powdrr_service.behavior_contract import (
    BehaviorContractError,
    BehaviorScenario,
    compile_behavior_scenarios,
)
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.design import DesiredState
from powdrr_service.structrr.obligation_evidence import (
    assert_obligation_evidence_complete,
    compile_obligation_evidence_contract,
)
from powdrr_service.structrr.reference_catalog import (
    ReferenceCatalog,
    resolve_reference,
)
from powdrr_service.structrr.reference_definitions import (
    ReferenceDefinition,
    required_text,
    text_tuple,
    validate_reference_id,
)

COMPILER_REVISION = "structrr-source-reference-handoff-v1"
PROMPT_SCHEMA = "minisweagent-source-reference-prompt-v1"
MANIFEST_SCHEMA = "source-reference-validation-manifest-v1"
SECTIONS = (
    "Objective",
    "Repository starting point",
    "Required behavior",
    "Required verification",
    "Preserve and avoid",
    "Allowed scope",
    "Focused commands",
    "Completion protocol",
)
WORKER_INSTRUCTION = (
    "Add the supplied Structrr IDs to the smallest declarations or blocks that own\n"
    "their requirements, using the supplied annotation convention. Preserve and\n"
    "relocate existing applicable references when moving code. Read referenced\n"
    "context before changing the owner. Do not invent IDs, alter canonical intent,\n"
    "or satisfy coverage by listing IDs in unrelated code. If an essential decision\n"
    "or assumption is missing, report the gap with its location and consequence.\n"
    "Run the local reference check before submitting. Report each implemented ID\n"
    "and its owning path and qualified symbol. These claims are independently checked."
)
_RELATIONSHIPS = {
    "implements": "feature",
    "realizes": "decision",
    "preserves": "invariant",
    "relies_on": "assumption",
}
_OWNER_KINDS = {"module", "class", "function", "async_function", "block"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _path(value: Any, *, source: bool = False) -> str:
    required_text(value, "path")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or ".." in parsed.parts
        or "\\" in value
        or PureWindowsPath(value).drive
        or any(c in value for c in "*?[]\n\r\x00")
    ):
        raise ValueError("selectors require safe literal repository-relative paths")
    if source and parsed.suffix != ".py":
        raise ValueError("placement owners require Python source paths")
    if value != parsed.as_posix():
        raise ValueError("selector paths must be normalized")
    return value


def _within(path: str, scope: tuple[str, ...]) -> bool:
    return any(
        root == "." or path == root or path.startswith(root + "/") for root in scope
    )


def _binding_key(owner: Mapping[str, Any]) -> str:
    # Role describes responsibility, not a second physical annotation owner.
    return _json(
        {key: owner.get(key) for key in ("binding", "path", "kind", "qualified_name")}
    )


def _owner(raw: Mapping[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("owner selector must be an object")
    binding = raw.get("binding", "exact")
    role = raw.get("role", "implementation")
    kind = raw.get("kind")
    if kind not in _OWNER_KINDS or role not in {"test", "implementation"}:
        raise ValueError("unsupported owner kind or role")
    if role == "test" and kind not in {"function", "async_function"}:
        raise ValueError("test ownership requires a collected function owner")
    common = {"binding", "role", "kind"}
    if binding == "exact":
        if set(raw) - common != {"path", "qualified_name"}:
            raise ValueError("exact selectors require path and qualified_name only")
        path = _path(raw["path"], source=True)
        if not _within(path, allowed):
            raise ValueError("owner lies outside the allowed scope")
        name = required_text(raw["qualified_name"], "qualified_name")
        return {
            "binding": binding,
            "role": role,
            "kind": kind,
            "path": path,
            "qualified_name": name,
        }
    if binding != "requires_semantic_review" or set(raw) - common != {
        "allowed_paths",
        "responsibility",
    }:
        raise ValueError("semantic selectors require paths and responsibility")
    paths = tuple(
        sorted(set(_path(p) for p in text_tuple(raw["allowed_paths"], "allowed_paths")))
    )
    if not paths or any(not _within(p, allowed) for p in paths):
        raise ValueError("semantic owner paths must stay inside allowed scope")
    return {
        "binding": binding,
        "role": role,
        "kind": kind,
        "allowed_paths": list(paths),
        "responsibility": required_text(raw["responsibility"], "responsibility"),
    }


def _placement(
    raw: Mapping[str, Any],
    definitions: Mapping[str, ReferenceDefinition],
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    if set(raw) != {
        "obligation_id",
        "ref",
        "relationship",
        "requirement",
        "owner",
        "cardinality",
        "verification_refs",
    }:
        raise ValueError("invalid placement obligation fields")
    obligation_id = validate_reference_id(raw["obligation_id"])
    ref = validate_reference_id(raw["ref"])
    definition = definitions.get(ref)
    if definition is None or definition.lifecycle != "active":
        raise ValueError("placement requires a resolved active canonical reference")
    relationship = raw["relationship"]
    owner = _owner(raw["owner"], allowed)
    if relationship == "checks":
        if owner["role"] != "test":
            raise ValueError("checks requires explicit test ownership")
    elif (
        _RELATIONSHIPS.get(relationship) != definition.kind
        or owner["role"] != "implementation"
    ):
        raise ValueError(
            "placement relationship disagrees with reference kind or owner role"
        )
    if raw["requirement"] != definition.statement:
        raise ValueError(
            "placement requirement must retain the exact accepted statement"
        )
    cardinality = raw["cardinality"]
    if cardinality not in {"exactly_one", "one_or_more"}:
        raise ValueError("unsupported placement cardinality")
    if owner["binding"] == "requires_semantic_review" and cardinality != "one_or_more":
        raise ValueError("semantic binding requires one_or_more and independent review")
    verification = sorted(
        set(text_tuple(raw["verification_refs"], "verification_refs"))
    )
    if not verification:
        raise ValueError("placement requires verification references")
    return {
        "obligation_id": obligation_id,
        "ref": ref,
        "relationship": relationship,
        "requirement": definition.statement,
        "owner": owner,
        "cardinality": cardinality,
        "verification_refs": verification,
    }


def _preservation(
    raw: Mapping[str, Any],
    definitions: Mapping[str, ReferenceDefinition],
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    if set(raw) != {
        "id",
        "ref",
        "owner",
        "disposition",
        "reason",
        "successor",
        "destination",
    }:
        raise ValueError("invalid preservation record fields")
    ref = validate_reference_id(raw["ref"])
    if ref not in definitions:
        raise ValueError("preservation reference is absent from the frozen catalog")
    # Baseline owners can be outside edit scope; they still must be preserved.
    owner = _owner(raw["owner"], (".",))
    if owner["binding"] != "exact":
        raise ValueError("baseline preservation needs an exact owner")
    disposition = raw["disposition"]
    if disposition not in {"preserve", "remove", "supersede", "relocate"}:
        raise ValueError("unsupported reference preservation disposition")
    reason = required_text(raw["reason"], "reason")
    destination = (
        _owner(raw["destination"], allowed) if raw["destination"] is not None else None
    )
    successor = raw["successor"]
    if (destination is not None) != (disposition == "relocate"):
        raise ValueError("only relocation requires a destination")
    if (successor is not None) != (disposition == "supersede"):
        raise ValueError("only supersession requires a successor")
    if disposition != "preserve" and not _within(owner["path"], allowed):
        raise ValueError("reference changes require the baseline owner in edit scope")
    if successor is not None:
        validate_reference_id(successor)
        if (
            successor == ref
            or successor not in definitions
            or definitions[successor].lifecycle != "active"
        ):
            raise ValueError("supersession needs a distinct active canonical successor")
        if definitions[ref].successor != successor:
            raise ValueError("supersession must agree with canonical lifecycle history")
    return {
        "id": validate_reference_id(raw["id"]),
        "ref": ref,
        "owner": owner,
        "disposition": disposition,
        "reason": reason,
        "successor": successor,
        "destination": destination,
    }


def compile_accepted_behavior_scenarios(
    accepted_design: Mapping[str, Any],
) -> tuple[BehaviorScenario, ...]:
    """Compile reviewed required cases and reject incomplete semantic closure."""
    return _compile_accepted_behavior_design(accepted_design)[0]


def _compile_accepted_behavior_design(
    accepted_design: Mapping[str, Any],
) -> tuple[tuple[BehaviorScenario, ...], dict[str, list[str]]]:
    raw_cases = accepted_design.get("required_test_cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("accepted source design requires observable behavior cases")
    scenarios: list[Mapping[str, Any]] = []
    case_ids: set[str] = set()
    for case in raw_cases:
        if not isinstance(case, Mapping):
            raise ValueError("accepted behavior case is malformed")
        case_id = required_text(case.get("id"), "behavior case id")
        if case_id in case_ids:
            raise ValueError(f"duplicate behavior case ID: {case_id}")
        case_ids.add(case_id)
        scenario = case.get("behavior_scenario")
        if not isinstance(scenario, Mapping):
            raise ValueError(f"behavior case {case_id} has no typed scenario")
        scenarios.append(scenario)
    compiled = _compile_behavior_scenario_data(scenarios)
    aliases = case_ids | {scenario.scenario_id for scenario in compiled}
    case_ids_by_scenario: dict[str, list[str]] = {}
    for case, scenario in zip(raw_cases, compiled, strict=True):
        case_ids_by_scenario.setdefault(scenario.scenario_id, []).append(
            required_text(case.get("id"), "behavior case id")
        )
    placements = accepted_design.get("source_reference_placements", [])
    if isinstance(placements, list):
        for placement in placements:
            if not isinstance(placement, Mapping):
                continue
            missing = sorted(set(placement.get("verification_refs", ())) - aliases)
            if missing:
                raise ValueError(
                    f"placement {placement.get('obligation_id')} has no accepted "
                    "behavior case for verification reference(s): " + ", ".join(missing)
                )
    return compiled, {
        scenario_id: sorted(ids) for scenario_id, ids in case_ids_by_scenario.items()
    }


def _compile_behavior_scenario_data(
    scenarios: list[Mapping[str, Any]],
) -> tuple[BehaviorScenario, ...]:
    try:
        compiled = compile_behavior_scenarios(scenarios)
    except BehaviorContractError as error:
        raise ValueError(
            f"accepted behavior scenarios are incomplete: {error}"
        ) from error
    for scenario in compiled:
        if scenario.routing not in {"include", "include_prohibition"}:
            raise ValueError(
                f"behavior scenario {scenario.scenario_id} is not an actionable "
                "obligation"
            )
        if scenario.unresolved_dimensions:
            raise ValueError(
                f"behavior scenario {scenario.scenario_id} has unresolved dimensions"
            )
        if not scenario.faithfulness_ref.get("fingerprint"):
            raise ValueError(
                f"behavior scenario {scenario.scenario_id} lacks source-"
                "faithfulness provenance"
            )
    return compiled


def _compile_obligation_evidence_contracts(
    placements: list[dict[str, Any]],
    preservation: list[dict[str, Any]],
    definitions: Mapping[str, ReferenceDefinition],
) -> list[dict[str, Any]]:
    contracts = []
    for placement in placements:
        definition = definitions[placement["ref"]]
        kind = (
            "invariant"
            if placement["relationship"] == "preserves"
            or definition.kind == "invariant"
            else "guidance"
            if definition.kind in {"assumption", "procedure", "guidance"}
            else "feature"
        )
        contracts.append(
            compile_obligation_evidence_contract(
                obligation_id=placement["obligation_id"],
                clause_id=placement["ref"],
                requirement_strength="must",
                kind=kind,
                polarity="required",
            )
        )
    for rule in preservation:
        contracts.append(
            compile_obligation_evidence_contract(
                obligation_id=rule["id"],
                clause_id=rule["ref"],
                requirement_strength="must",
                kind="invariant",
                polarity="required",
            )
        )
    expected_ids = [
        *(item["obligation_id"] for item in placements),
        *(item["id"] for item in preservation),
    ]
    assert_obligation_evidence_complete(contracts, expected_ids)
    return [item.to_data() for item in contracts]


def _normalize(contract: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "base_commit",
        "design_fingerprint",
        "accepted_document_digest",
        "catalog_digest",
        "objective",
        "allowed_paths",
        "verification_commands",
        "reference_check_command",
        "definitions",
        "obligations",
        "preservation",
        "behavior_scenarios",
        "behavior_case_ids",
    }
    if set(contract) - {"workspace_root"} != fields:
        raise ValueError("invalid source reference contract fields")
    if "workspace_root" in contract:
        workspace = required_text(contract["workspace_root"], "workspace_root")
        parsed = PurePosixPath(workspace)
        if not parsed.is_absolute() or ".." in parsed.parts or str(parsed) != workspace:
            raise ValueError("rollout requires a normalized absolute workspace root")
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", contract["base_commit"]):
        raise ValueError("source reference handoff requires a full base commit")
    for key in ("design_fingerprint", "accepted_document_digest", "catalog_digest"):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", contract[key]):
            raise ValueError("invalid source reference input fingerprint")
    allowed = tuple(
        sorted(
            set(
                _path(p) for p in text_tuple(contract["allowed_paths"], "allowed_paths")
            )
        )
    )
    if not allowed:
        raise ValueError("source reference handoff requires allowed paths")
    definitions = [ReferenceDefinition.from_data(d) for d in contract["definitions"]]
    by_id = {d.id: d for d in definitions}
    if len(by_id) != len(definitions):
        raise ValueError("duplicate canonical reference definitions")
    obligations = [_placement(raw, by_id, allowed) for raw in contract["obligations"]]
    preservation = [
        _preservation(raw, by_id, allowed) for raw in contract["preservation"]
    ]
    raw_scenarios = contract["behavior_scenarios"]
    if not isinstance(raw_scenarios, list) or any(
        not isinstance(scenario, Mapping) for scenario in raw_scenarios
    ):
        raise ValueError("source reference contract requires behavior scenarios")
    behavior_scenarios = [
        scenario.to_data()
        for scenario in _compile_behavior_scenario_data(raw_scenarios)
    ]
    raw_case_ids = contract["behavior_case_ids"]
    scenario_ids = {scenario["scenario_id"] for scenario in behavior_scenarios}
    if not isinstance(raw_case_ids, Mapping) or set(raw_case_ids) != scenario_ids:
        raise ValueError("behavior case IDs must map every accepted scenario")
    behavior_case_ids: dict[str, list[str]] = {}
    for scenario_id, case_ids in raw_case_ids.items():
        if not isinstance(case_ids, list):
            raise ValueError(f"behavior case IDs for {scenario_id} are malformed")
        normalized_case_ids = sorted(
            {required_text(case_id, "behavior case id") for case_id in case_ids}
        )
        if not normalized_case_ids or len(normalized_case_ids) != len(case_ids):
            raise ValueError(f"behavior case IDs for {scenario_id} are incomplete")
        behavior_case_ids[scenario_id] = normalized_case_ids
    for items, field in ((obligations, "obligation_id"), (preservation, "id")):
        if len({item[field] for item in items}) != len(items):
            raise ValueError("duplicate source reference obligation identity")
    if not obligations and not preservation:
        raise ValueError(
            "source reference handoff requires accepted placement obligations"
        )
    baseline_keys = [
        (rule["ref"], _binding_key(rule["owner"])) for rule in preservation
    ]
    if len(set(baseline_keys)) != len(baseline_keys):
        raise ValueError("conflicting baseline reference preservation rules")
    required_keys = {(o["ref"], _binding_key(o["owner"])) for o in obligations}
    if any(
        rule["disposition"] != "preserve"
        and (rule["ref"], _binding_key(rule["owner"])) in required_keys
        for rule in preservation
    ):
        raise ValueError("cannot remove or move a required reference from its owner")
    commands = [
        list(text_tuple(c, "verification command"))
        for c in contract["verification_commands"]
    ]
    if any(not command for command in commands):
        raise ValueError("verification commands cannot be empty")
    check = contract["reference_check_command"]
    if check is not None:
        check = list(text_tuple(check, "reference_check_command"))
        if not check:
            raise ValueError("reference check command cannot be empty")
    return {
        **contract,
        "objective": required_text(contract["objective"], "objective"),
        "allowed_paths": list(allowed),
        "reference_check_command": check,
        "verification_commands": commands,
        "definitions": [by_id[key].to_data() for key in sorted(by_id)],
        "obligations": sorted(obligations, key=lambda o: o["obligation_id"]),
        "preservation": sorted(preservation, key=lambda o: o["id"]),
        "behavior_scenarios": sorted(
            behavior_scenarios, key=lambda scenario: scenario["scenario_id"]
        ),
        "behavior_case_ids": {
            scenario_id: behavior_case_ids[scenario_id]
            for scenario_id in sorted(behavior_case_ids)
        },
    }


def _render(contract: Mapping[str, Any]) -> str:
    workspace = contract.get("workspace_root")
    sections = (
        _json(contract["objective"]),
        _json(
            {
                k: contract[k]
                for k in (
                    "base_commit",
                    "design_fingerprint",
                    "catalog_digest",
                    *(("workspace_root",) if workspace else ()),
                )
            }
        )
        + (
            "\nBefore inspection run pwd and git rev-parse --show-toplevel; both "
            "must identify this workspace. Do not read or edit another checkout."
            if workspace
            else ""
        ),
        _json(
            {
                "placements": contract["obligations"],
                "reference_metadata": [
                    {
                        key: value
                        for key, value in definition.items()
                        if key != "statement"
                    }
                    for definition in contract["definitions"]
                ],
                "behavior_scenarios": contract["behavior_scenarios"],
                "behavior_case_ids": contract["behavior_case_ids"],
            }
        ),
        _json(
            {
                "verification_refs": sorted(
                    {
                        ref
                        for o in contract["obligations"]
                        for ref in o["verification_refs"]
                    }
                ),
                "commands": contract["verification_commands"],
            }
        ),
        WORKER_INSTRUCTION + "\n\nAnnotation grammar: # structrr: ID; "
        "# structrr[module]: ID; # structrr[block]: ID. Place declaration markers "
        "immediately above the owning declaration, before decorators. Module markers "
        "belong in the module header. Block markers precede their owning statement.\n"
        + _json({"baseline_reference_rules": contract["preservation"]}),
        _json(contract["allowed_paths"]),
        _json(
            {
                "reader": [
                    "python",
                    "-m",
                    "powdrr_agent.execution.source_reader",
                    "read",
                    "PATH",
                    "--start",
                    "N",
                    "--end",
                    "N",
                ],
                "search": [
                    "python",
                    "-m",
                    "powdrr_agent.execution.source_reader",
                    "search",
                    "LITERAL",
                    "PATH",
                ],
                "local_reference_check": contract["reference_check_command"],
            }
        )
        + (
            "\nLocal reference check is unavailable in this build. Report it as an "
            "unresolved gap; do not claim reference validation passed."
            if contract["reference_check_command"] is None
            else ""
        ),
        "Return control using the existing completion protocol. In the existing "
        "submission report each reference ID, owning path, kind and qualified symbol; "
        "include unresolved design gaps, their locations and consequences. Do not "
        "write an additional repository artifact. Semantic owner selectors require "
        "independent review; worker claims never establish a passing binding. "
        "A design gap leaves the attempt incomplete and requires the normal design "
        "workflow; the worker cannot approve its own amendment."
        + (
            "\nSubmit a JSON object with "
            "schema_version=structrr-reference-completion-v1, "
            "reference_owners (a list of ref/path/kind/qualified_name objects), and "
            "design_gaps (a list of location/consequence objects). Report every "
            "unresolved gap; a nonempty list blocks acceptance. Do not commit or push."
            if workspace
            else ""
        ),
    )
    return (
        "\n\n".join(
            f"## {heading}\n{body}"
            for heading, body in zip(SECTIONS, sections, strict=True)
        )
        + "\n"
    )


def _prompt(contract: Mapping[str, Any], private_digest: str) -> dict[str, Any]:
    if not isinstance(private_digest, str) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", private_digest
    ):
        raise ValueError("invalid private validation input digest")
    rendered = _render(contract)
    if len(rendered) > 64000:
        raise ValueError("source reference prompt exceeds its 64000 character bound")
    data = {
        "schema_version": PROMPT_SCHEMA,
        "compiler_revision": COMPILER_REVISION,
        "source_reference_mode": "enforce",
        "private_validation_digest": private_digest,
        "contract": dict(contract),
        "contract_fingerprint": content_fingerprint(contract),
        "prompt": rendered,
    }
    return {**data, "fingerprint": content_fingerprint(data)}


@dataclass(frozen=True, slots=True)
class SourceReferencePrompt:
    _data_json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._data_json)

    @property
    def prompt(self) -> str:
        return str(self.to_data()["prompt"])

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> SourceReferencePrompt:
        if raw.get("schema_version") != PROMPT_SCHEMA or not isinstance(
            raw.get("contract"), Mapping
        ):
            raise ValueError("invalid source reference prompt schema")
        expected = _prompt(
            _normalize(raw["contract"]), raw.get("private_validation_digest")
        )
        if dict(raw) != expected:
            raise ValueError("source reference prompt or fingerprint mismatch")
        return cls(_json(expected))


@dataclass(frozen=True, slots=True)
class CompiledSourceReferenceHandoff:
    """Private bundle; only worker_prompt belongs in a worker request."""

    _data_json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._data_json)

    @property
    def worker_prompt(self) -> SourceReferencePrompt:
        return SourceReferencePrompt.from_data(self.to_data()["worker_prompt"])

    @property
    def validation_manifest(self) -> dict[str, Any]:
        return self.to_data()["validation_manifest"]

    def require_matches(self, prompt: SourceReferencePrompt) -> None:
        if prompt.to_data() != self.worker_prompt.to_data():
            raise ValueError(
                "worker prompt differs from the private validation manifest"
            )

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> CompiledSourceReferenceHandoff:
        if raw.get("schema_version") != COMPILER_REVISION:
            raise ValueError("invalid source reference handoff schema")
        prompt = SourceReferencePrompt.from_data(raw["worker_prompt"]).to_data()
        expected = _bundle(
            prompt, raw["validation_manifest"]["private_validation_data"]
        )
        if dict(raw) != expected:
            raise ValueError("source reference handoff or manifest mismatch")
        return cls(_json(expected))


def _bundle(prompt: dict[str, Any], private: Mapping[str, Any]) -> dict[str, Any]:
    if prompt["private_validation_digest"] != content_fingerprint(dict(private)):
        raise ValueError("private validation inputs differ from the compiled prompt")
    contract = prompt["contract"]
    definitions = {
        item["id"]: ReferenceDefinition.from_data(item)
        for item in contract["definitions"]
    }
    expected_evidence_contracts = _compile_obligation_evidence_contracts(
        contract["obligations"], contract["preservation"], definitions
    )
    if private.get("obligation_evidence_contracts") != expected_evidence_contracts:
        raise ValueError("private obligation evidence contracts do not match handoff")
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "compiler_revision": COMPILER_REVISION,
        "contract": prompt["contract"],
        "contract_fingerprint": prompt["contract_fingerprint"],
        "prompt_fingerprint": prompt["fingerprint"],
        "private_validation_data": dict(private),
    }
    manifest["fingerprint"] = content_fingerprint(manifest)
    data = {
        "schema_version": COMPILER_REVISION,
        "worker_prompt": prompt,
        "validation_manifest": manifest,
    }
    return {**data, "fingerprint": content_fingerprint(data)}


def compile_source_reference_handoff(
    accepted_design: Mapping[str, Any],
    catalog: ReferenceCatalog,
    *,
    objective: str,
    allowed_paths: tuple[str, ...],
    verification_commands: tuple[tuple[str, ...], ...] = (),
    reference_check_command: tuple[str, ...] | None = None,
    private_validation_data: Mapping[str, Any] | None = None,
    workspace_root: str | None = None,
) -> CompiledSourceReferenceHandoff:
    if catalog.accepted_document_digest != content_fingerprint(accepted_design):
        raise ValueError("accepted design and frozen catalog do not match")
    desired = DesiredState.from_snapshot(accepted_design)
    behavior_scenarios, behavior_case_ids = _compile_accepted_behavior_design(
        accepted_design
    )
    placements = desired.document.get("source_reference_placements", [])
    preservation = desired.document.get("source_reference_preservations", [])
    definitions_by_id: dict[str, ReferenceDefinition] = {}
    refs = {p["ref"] for p in placements} | {p["ref"] for p in preservation}
    refs.update(p["successor"] for p in preservation if p.get("successor") is not None)
    definitions = []
    for ref in sorted(refs):
        resolved = resolve_reference(catalog, ref)
        if resolved.entry is None:
            raise ValueError(f"unresolved placement/preservation reference: {ref}")
        definitions.append(resolved.entry.reference.to_data())
        definitions_by_id[ref] = resolved.entry.reference
    contract = _normalize(
        {
            **(
                {"workspace_root": workspace_root} if workspace_root is not None else {}
            ),
            "base_commit": catalog.base_commit,
            "design_fingerprint": desired.fingerprint,
            "accepted_document_digest": catalog.accepted_document_digest,
            "catalog_digest": catalog.digest,
            "objective": objective,
            "allowed_paths": list(allowed_paths),
            "verification_commands": [list(c) for c in verification_commands],
            "reference_check_command": list(reference_check_command)
            if reference_check_command is not None
            else None,
            "definitions": definitions,
            "obligations": placements,
            "preservation": preservation,
            "behavior_scenarios": [
                scenario.to_data() for scenario in behavior_scenarios
            ],
            "behavior_case_ids": behavior_case_ids,
        }
    )
    private = dict(private_validation_data or {})
    if "obligation_evidence_contracts" in private:
        raise ValueError("private validation data uses a reserved contract key")
    private["obligation_evidence_contracts"] = _compile_obligation_evidence_contracts(
        placements, preservation, definitions_by_id
    )
    return CompiledSourceReferenceHandoff(
        _json(_bundle(_prompt(contract, content_fingerprint(private)), private))
    )
