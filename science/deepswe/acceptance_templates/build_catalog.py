"""Freeze the reviewed proposal's cards into a standalone experiment catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROPOSAL = ROOT / "docs/plans/acceptance-criteria-template-catalog-proposal.md"
DEFAULT_CATALOG = Path(__file__).with_name("catalog.json")

PREREQUISITES = {
    "T17": [
        {
            "id": "value_selection",
            "claim": "The source specifies which configuration value takes effect "
            "when multiple applicable sources supply it.",
            "reject_inference": "Traversal order or presentation order alone does "
            "not establish overriding or value selection.",
        }
    ],
    "T18": [
        {
            "id": "collision_winner",
            "claim": "The source specifies a winner for conflicting values "
            "at the same key in the named operation and scope.",
            "reject_inference": "Old-before-new concatenation, insertion order, "
            "or generic merging alone does not specify a collision winner.",
        }
    ],
    "T31": [
        {
            "id": "object_identity",
            "claim": "The source requires the same object reference to be "
            "returned, shared, or preserved by the named operation.",
            "reject_inference": "Read-only access, tuple representation, equal "
            "contents, insertion order, or immutability alone does not require "
            "object identity. Identity can be specified without the word identity.",
        }
    ],
}

RELATIONS = {
    "T17": {
        "question": "What does this operation do with applicable "
        "configuration sources?",
        "relation_classes": {
            "select_effective_value": "Select an effective setting using "
            "specified source priority.",
            "list_in_order": "Traverse or list entries without selecting "
            "an effective value.",
            "combine_values": "Combine values without specifying source priority.",
            "unspecified": "No relation is defined.",
            "uncertain": "The relation is ambiguous.",
        },
        "required_relation": "select_effective_value",
    },
    "T18": {
        "question": "In this operation and scope, what happens to competing "
        "values from the two inputs?",
        "relation_classes": {
            "select_one_source_value": "For a keyed conflict, one specified "
            "source's value is selected for the same key/field lookup; the "
            "competing value is not selected.",
            "retain_both_in_order": "Both input values/elements survive in a "
            "specified relative order, as in concatenation. Neither is "
            "discarded as a losing value.",
            "combine_values": "A result is composed from both values without "
            "selecting a source's conflicting value.",
            "unspecified": "No conflict outcome is defined for this operation/scope.",
            "uncertain": "The relation is ambiguous.",
        },
        "required_relation": "select_one_source_value",
    },
    "T31": {
        "question": "What object-reference relationship does this operation require?",
        "relation_classes": {
            "same_object": "The original reference is returned, shared, or "
            "preserved rather than copied or replaced.",
            "equal_contents": "Contents are equal without requiring the "
            "same reference.",
            "read_only": "The value is immutable/read-only without requiring "
            "a particular reference.",
            "fresh_object": "A different object is created or copied.",
            "unspecified": "No reference relationship is specified.",
            "uncertain": "The relation is ambiguous.",
        },
        "required_relation": "same_object",
    },
}

# Optional clauses are omitted when their slots lack source support. These
# adaptations remove editorial directions and slash alternatives from prose.
RENDERERS: dict[str, tuple[str, list[str]]] = {
    "T01": (
        "{symbol} exposes {specified_interface}.",
        ["It is available at {public_path}.", "{valid_invocation} is accepted."],
    ),
    "T05": ("Given {invalid_input_condition}, {operation} {error_contract}.", []),
    "T13": (
        "{parser} accepts {grammar_class} and rejects {excluded_class}.",
        ["Rejection produces {specified_failure}."],
    ),
    "T14": (
        "{sequence} is ordered by {ordering_rule}.",
        ["For ties or groups, {tie_or_group_rule}."],
    ),
    "T15": (
        "For items equivalent under {duplicate_relation}, retain {retention_rule}.",
        ["Expose {specified_alias_or_count_behavior}."],
    ),
    "T19": (
        "For distinct {owners}, {action_on_first} leaves {observation_on_second} "
        "unchanged or inaccessible according to {isolation_rule}.",
        [],
    ),
    "T27": (
        "After saving and recalling {history_kind}, "
        "restore data for {included_states}.",
        ["{excluded_descendants} follow {specified_nonrestored_behavior}."],
    ),
    "T28": (
        "At {phase}, invoke {callback} with {context}.",
        [
            "Invocation count: {cardinality_if_specified}.",
            "Event ordering: {other_events_if_specified}.",
        ],
    ),
    "T29": (
        "During {window}, {query} returns {included_records}; at {reset_boundary}, "
        "prior records no longer appear.",
        ["Records follow {order_if_specified}."],
    ),
    "T39": (
        "Apply {patch} at {resolved_path} using {operation}.",
        ["Missing paths: {missing_path_rule}.", "Null values: {null_rule}."],
    ),
    "T47": (
        "When {cancellation_trigger} occurs, pending {operations} finish with "
        "{cancellation_outcome}.",
        ["{excluded_completed_operations} retain {specified_behavior}."],
    ),
    "T48": (
        "Before {time_boundary}, {restricted_behavior} holds; "
        "{specified_relation_to_boundary}, {eligible_behavior} applies.",
        ["{restart_event} resets the window."],
    ),
    "T49": (
        "For {existing_input_class}, {operation} "
        "preserves {named_existing_observations}.",
        ["It adds {permitted_new_information}."],
    ),
    "T50": (
        "For each {listed_surface}, {same_contract} holds.",
        ["The permitted differences are {specified_surface_differences}."],
    ),
}


def placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\{([a-z_]+)\}", text)))


def _slot_kind(name: str) -> str:
    if any(word in name for word in ("scope", "owner", "window", "depth")):
        return "Scope"
    if any(word in name for word in ("condition", "predicate", "guard", "relation")):
        return "Condition"
    if any(word in name for word in ("count", "cardinality", "limit")):
        return "Quantifier"
    if any(word in name for word in ("observation", "result", "field", "effect")):
        return "Observation"
    if any(word in name for word in ("sequence", "ordering", "order", "stages")):
        return "Sequence"
    if any(word in name for word in ("value", "expression", "rule", "alternative")):
        return "Expression"
    return "Symbol"


def build_catalog(proposal: Path) -> dict[str, Any]:
    source = proposal.read_text(encoding="utf-8")
    cards = []
    for match in re.finditer(
        r"^#### (T\d+) — ([^\n]+)\n(.*?)(?=^#### |^### |^## |\Z)",
        source,
        re.M | re.S,
    ):
        template_id, name, body = match.groups()
        fields = {
            key: re.search(rf"\*\*{key}:\*\*\s*(.*?)(?=\n\n|\Z)", body, re.S)
            for key in ("Select when", "Prose", "Slots", "Example", "Rejects")
        }
        if any(value is None for value in fields.values()):
            raise ValueError(f"incomplete proposal card: {template_id}")
        text = {key: value.group(1).strip() for key, value in fields.items() if value}
        quoted = re.search("“(.+?)”", text["Prose"])
        if quoted is None:
            raise ValueError(f"no renderer: {template_id}")
        required, optional = RENDERERS.get(template_id, (quoted.group(1), []))
        required_slots = placeholders(required)
        all_slots = placeholders(" ".join([required, *optional]))
        cards.append(
            {
                "id": template_id,
                "version": 2 if template_id in PREREQUISITES else 1,
                "name": name,
                "applies_when": text["Select when"],
                "prerequisites": [
                    {**item, **RELATIONS.get(template_id, {})}
                    for item in PREREQUISITES.get(template_id, [])
                ],
                "required_sentence": required,
                "optional_sentences": optional,
                "slots": [
                    {
                        "name": slot,
                        "kind": _slot_kind(slot),
                        "required": slot in required_slots,
                    }
                    for slot in all_slots
                ],
                "slot_guidance": text["Slots"],
                "example": text["Example"],
                "near_miss": text["Rejects"],
            }
        )
    if [card["id"] for card in cards] != [f"T{i:02}" for i in range(1, 53)]:
        raise ValueError("expected exactly the proposal's 52 sequential cards")
    return {
        "version": 2,
        "proposal_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "slot_representation": "Typed semantic slots use nonempty prose strings; "
        "type/evidence validation is structural, not proof of entailment.",
        "templates": cards,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, default=DEFAULT_PROPOSAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()
    args.output.write_text(json.dumps(build_catalog(args.proposal), indent=2) + "\n")
    print(f"Wrote 52 template cards to {args.output}")


if __name__ == "__main__":
    main()
