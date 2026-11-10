"""Canonical desired source design, independent of observations and execution.

Snapshots and proposals remain readable through their existing adapters. This
module owns their shared state transition semantics and the portable handoff.
It never derives intended meaning from candidate source code.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.actions import normalize_action
from powdrr_service.structrr.proposal import ProposalRevision
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.snapshot import snapshot_digest

DESIGN_SCHEMA = "structrr-desired-state-v1"
HANDOFF_SCHEMA = "structrr-design-handoff-v1"
DESIGN_SECTIONS = frozenset(
    {
        "reference_definitions",
        "assumptions",
        "entities",
        "entity_relationships",
        "source_subjects",
        "source_bindings",
        "requirements",
        "decisions",
        "human-decisions",
        "invariants",
        "guidance",
        "features",
        "approach",
        "modules",
        "tools",
        "intents",
        "active_intent",
        "acceptance_criteria",
        "required_test_cases",
        "expected_tests",
        "expected_outcomes",
        "non_goals",
        "risks",
        "proposed_prs",
        "files",
        "statements",
    }
)


def subject_identity(section: str, record: Mapping[str, Any]) -> str:
    keys = (
        ("stable_key", "id", "path")
        if section in {"files", "source_subjects"}
        else ("id", "clause_id", "intent_id")
    )
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError(f"{section} design record requires a stable identity")


def project_sections(
    baseline: Mapping[str, Any], sections: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    """Apply add/change/remove without mutating inputs or discarding metadata.

    Changes merge explicit fields; removals retire the complete subject. A field
    can be explicitly cleared with null. Supersession is an explicit removal and
    addition, with a supersedes reference on the replacement.
    """
    result: dict[str, Any] = json.loads(json.dumps(dict(baseline)))
    for section, operations in sections.items():
        if section not in DESIGN_SECTIONS:
            raise ValueError(f"cannot project unsupported Structrr section {section!r}")
        collection = "source_subjects" if section == "files" else section
        records = result.get(collection, [])
        if not isinstance(records, list) or any(
            not isinstance(record, Mapping) for record in records
        ):
            raise ValueError(f"Structrr {collection} must contain identified records")
        by_id: dict[str, dict[str, Any]] = {}
        for record in records:
            identity = subject_identity(collection, record)
            if identity in by_id:
                raise ValueError(
                    f"duplicate Structrr {collection} identity: {identity}"
                )
            by_id[identity] = dict(record)
        touched: set[str] = set()
        for operation in operations:
            identity = subject_identity(section, operation)
            if identity in touched:
                raise ValueError(
                    f"duplicate operation for {section} subject {identity}"
                )
            touched.add(identity)
            action = normalize_action(operation.get("action"))
            if action == "add" and identity in by_id:
                raise ValueError(
                    f"cannot project added {section} subject already present: "
                    f"{identity}"
                )
            if action != "add" and identity not in by_id:
                path = identity.removeprefix("path::")
                known_file = (
                    action == "change"
                    and section == "files"
                    and identity.startswith("path::")
                    and operation.get("path") == path
                    and any(
                        record.get("path") == path for record in result.get("files", [])
                    )
                )
                if not known_file:
                    raise ValueError(
                        f"cannot project {action} {section} subject absent from "
                        "baseline: "
                        f"{identity}"
                    )
            if action == "remove":
                del by_id[identity]
            else:
                content = {
                    key: value for key, value in operation.items() if key != "action"
                }
                by_id[identity] = {**by_id.get(identity, {}), **content}
        result[collection] = [by_id[key] for key in sorted(by_id)]
    return result


@dataclass(frozen=True, slots=True)
class DesiredState:
    """Immutable JSON backing prevents callers mutating a revision's identity."""

    _document_json: str

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> DesiredState:
        if snapshot.get("schema_version") == DESIGN_SCHEMA:
            return cls.from_data(snapshot)
        metadata = {
            "schema",
            "structrr_schema",
            "schema_version",
            "section_versions",
            "change_id",
            "title",
            "structured_files",
            "source_manifest",
            "validation_inventory",
            "validation_context",
            "code_symbol_artifact",
            "structrr_snapshot_id",
        }
        if unknown := set(snapshot) - DESIGN_SECTIONS - metadata - {"intent"}:
            raise ValueError(f"unsupported desired-state fields: {sorted(unknown)}")
        document: dict[str, Any] = {}
        for section in sorted(DESIGN_SECTIONS):
            if section not in snapshot:
                continue
            value = snapshot[section]
            if not isinstance(value, list):
                raise ValueError(f"desired-state {section} must be a list")
            records = []
            identities: set[str] = set()
            for raw in value:
                if not isinstance(raw, Mapping):
                    raise ValueError(
                        f"desired-state {section} requires identified records"
                    )
                record = dict(raw)
                if record.get("action") in {"remove", "removed", "deleted"}:
                    raise ValueError(
                        "desired state cannot adopt a removal delta as an active record"
                    )
                identity = subject_identity(section, record)
                if identity in identities:
                    raise ValueError(
                        f"duplicate desired-state {section} identity: {identity}"
                    )
                identities.add(identity)
                # A materialized state describes what holds, not past operations.
                record.pop("action", None)
                if section == "reference_definitions":
                    record = ReferenceDefinition.from_data(record).to_data()
                records.append(record)
            document[section] = sorted(
                records, key=lambda item: subject_identity(section, item)
            )
        # Top-level intent is design meaning; source manifests and tool results
        # deliberately do not enter the desired-state fingerprint.
        if "intent" in snapshot:
            document["intent"] = snapshot["intent"]
        entities = {item["id"] for item in document.get("entities", [])}
        for relationship in document.get("entity_relationships", []):
            if any(
                not isinstance(relationship.get(endpoint), str)
                or relationship[endpoint] not in entities
                for endpoint in ("source", "target")
            ):
                raise ValueError(
                    "desired relationship endpoints must identify active entities"
                )
        return cls(
            json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False)
        )

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self._document_json)

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.to_data(include_fingerprint=False))

    def to_data(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        data = {"schema_version": DESIGN_SCHEMA, "document": self.document}
        if include_fingerprint:
            data["fingerprint"] = self.fingerprint
        return data

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> DesiredState:
        if raw.get("schema_version") != DESIGN_SCHEMA or not isinstance(
            raw.get("document"), Mapping
        ):
            raise ValueError("invalid Structrr desired-state schema")
        state = cls.from_snapshot(raw["document"])
        if dict(raw) != state.to_data():
            raise ValueError("desired-state fingerprint mismatch")
        return state


@dataclass(frozen=True, slots=True)
class DesignHandoff:
    """Everything a fresh coding session needs, with no transcript dependency."""

    _data_json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._data_json)

    @classmethod
    def compile(
        cls,
        baseline: Mapping[str, Any],
        proposal: ProposalRevision,
        *,
        base_commit: str,
    ) -> DesignHandoff:
        if not base_commit.strip():
            raise ValueError("design handoff requires a repository base commit")
        if snapshot_digest(baseline) != proposal.structrr_baseline_fingerprint:
            raise ValueError("design handoff proposal baseline mismatch")
        before = DesiredState.from_snapshot(baseline)
        sections: dict[str, list[dict[str, Any]]] = {}
        for operation in proposal.operations:
            content = dict(operation.content)
            if not any(
                key in content for key in ("id", "stable_key", "clause_id", "path")
            ):
                content["id"] = operation.subject_id
            if subject_identity(operation.section, content) != operation.subject_id:
                raise ValueError("proposal subject identity differs from content")
            content["action"] = operation.action
            sections.setdefault(operation.section, []).append(content)
        after = DesiredState.from_snapshot(project_sections(before.document, sections))
        data = {
            "schema_version": HANDOFF_SCHEMA,
            "base_commit": base_commit,
            "baseline": dict(baseline),
            "before": before.to_data(),
            "desired": after.to_data(),
            "proposal": proposal.to_data(),
        }
        data["fingerprint"] = content_fingerprint(data)
        return cls(
            json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
        )

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> DesignHandoff:
        if raw.get("schema_version") != HANDOFF_SCHEMA:
            raise ValueError("unsupported design handoff schema")
        proposal = ProposalRevision.from_data(raw["proposal"])
        expected = cls.compile(
            raw["baseline"], proposal, base_commit=raw["base_commit"]
        )
        if dict(raw) != expected.to_data():
            raise ValueError("design handoff content or fingerprint mismatch")
        return expected

    def render(self) -> str:
        data = self.to_data()
        proposal = ProposalRevision.from_data(data["proposal"])
        return "\n".join(
            (
                "Structrr desired source design",
                f"Repository starting commit: {data['base_commit']}",
                "Bring the source into agreement with the desired design below.",
                "Rationale explains the design; it is not a source-code observation.",
                "If a required design cannot be implemented, report a discrepancy",
                "and propose a Structrr amendment. Do not silently weaken it.",
                json.dumps(data["desired"]["document"], indent=2, sort_keys=True),
                "Design changes (including reasons and provenance):",
                "Change intent: " + json.dumps(proposal.intent, sort_keys=True),
                "Verification requirements: "
                + json.dumps(proposal.verification, sort_keys=True),
                "Source evidence: "
                + json.dumps(proposal.code_evidence, sort_keys=True),
                json.dumps(
                    [item.to_data() for item in proposal.operations],
                    indent=2,
                    sort_keys=True,
                ),
                "Acceptance criteria: " + json.dumps(proposal.acceptance_criteria),
                "Preserve: " + json.dumps(proposal.must_preserve),
                "Non-goals: " + json.dumps(proposal.non_goals),
                "Allowed paths: " + json.dumps(proposal.allowed_paths),
                "Source references: " + json.dumps(proposal.source_refs),
            )
        )
