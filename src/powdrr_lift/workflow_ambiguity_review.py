"""Opt-in advisory ambiguity reviews for individual workflow definition steps."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from powdrr_lift.workflow_llm import WorkflowLLMClient


class WorkflowAmbiguityReviewError(ValueError):
    """Raised for malformed definition steps or reviewer responses."""


@dataclass(frozen=True, slots=True)
class WorkflowAmbiguityReview:
    definition: str
    definition_kind: str
    step_id: str | None
    step_index: int
    first_action: Mapping[str, Any]
    completion_condition: str
    allowed_actions: tuple[str, ...]
    missing_information: tuple[str, ...]
    conflicts: tuple[str, ...]
    ambiguous_phrases: tuple[str, ...]
    source_sentences: tuple[str, ...]
    suggested_wording: tuple[str, ...]
    confidence: float

    def to_data(self) -> dict[str, Any]:
        return {
            "definition": self.definition,
            "definition_kind": self.definition_kind,
            "step_id": self.step_id,
            "step_index": self.step_index,
            "first_action": dict(self.first_action),
            "completion_condition": self.completion_condition,
            "allowed_actions": list(self.allowed_actions),
            "missing_information": list(self.missing_information),
            "conflicts": list(self.conflicts),
            "ambiguous_phrases": list(self.ambiguous_phrases),
            "source_sentences": list(self.source_sentences),
            "suggested_wording": list(self.suggested_wording),
            "confidence": self.confidence,
        }


def build_ambiguity_review_messages(
    definition_path: Path,
    *,
    step_id: str | None = None,
    step_index: int | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build a compact, deterministic review prompt for exactly one step."""
    definition, kind = _load_definition(definition_path)
    steps = definition["steps" if kind == "skill" else "task_templates"]
    selected_index = _select_step(steps, step_id=step_id, step_index=step_index)
    step = steps[selected_index]
    assert isinstance(step, Mapping)
    selected_id = step.get("id") if isinstance(step.get("id"), str) else None
    example = {
        "first_action": {"action": "invoke_tool", "parameters": {}},
        "completion_condition": "The declared output is recorded.",
        "allowed_actions": ["invoke_tool", "next_step"],
        "missing_information": [],
        "conflicts": [],
        "ambiguous_phrases": [],
        "source_sentences": [],
        "suggested_wording": [],
        "confidence": 0.95,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are an observational, read-only workflow-definition reviewer. "
                "Inspect one step and its compact parent contract. Do not propose "
                "code changes, invoke tools, or claim execution results. Identify "
                "only ambiguity that could cause an LLM to choose the wrong first "
                "action, completion condition, parameters, or human handoff. Every "
                "finding must quote its exact source sentence in source_sentences and "
                "offer a concrete replacement in suggested_wording. "
                "Return exactly one JSON object matching this complete example:\n"
                + json.dumps(example, ensure_ascii=False)
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "definition": definition_path.as_posix(),
                    "definition_kind": kind,
                    "when_to_use": definition.get("when_to_use", []),
                    "guidance": definition.get("how_to_fill_this_out", []),
                    "step_index": selected_index,
                    "step": dict(step),
                },
                ensure_ascii=False,
            ),
        },
    ]
    return messages, {
        "definition": str(definition_path),
        "definition_kind": kind,
        "step_id": selected_id,
        "step_index": selected_index,
    }


def review_workflow_definition_step(
    client: WorkflowLLMClient,
    definition_path: Path,
    *,
    step_id: str | None = None,
    step_index: int | None = None,
) -> WorkflowAmbiguityReview:
    """Request and validate an advisory review; no workflow state is mutated."""
    messages, identity = build_ambiguity_review_messages(
        definition_path, step_id=step_id, step_index=step_index
    )
    try:
        payload = client.complete_json(messages)
    except RuntimeError as exc:
        raise WorkflowAmbiguityReviewError(
            f"Ambiguity reviewer request failed: {exc}"
        ) from exc
    return _parse_review(payload, identity)


def _parse_review(
    payload: Mapping[str, Any], identity: Mapping[str, Any]
) -> WorkflowAmbiguityReview:
    first_action = payload.get("first_action")
    if not isinstance(first_action, Mapping) or not isinstance(
        first_action.get("action"), str
    ):
        raise WorkflowAmbiguityReviewError(
            "Reviewer first_action must be an object with a non-empty action."
        )
    completion_condition = _required_text(
        payload.get("completion_condition"), "completion_condition"
    )
    allowed_actions = _string_list(payload.get("allowed_actions"), "allowed_actions")
    missing_information = _string_list(
        payload.get("missing_information"), "missing_information"
    )
    conflicts = _string_list(payload.get("conflicts"), "conflicts")
    ambiguous_phrases = _string_list(
        payload.get("ambiguous_phrases"), "ambiguous_phrases"
    )
    source_sentences = _string_list(payload.get("source_sentences"), "source_sentences")
    suggested_wording = _string_list(
        payload.get("suggested_wording"), "suggested_wording"
    )
    if bool(ambiguous_phrases) != bool(source_sentences) or bool(
        ambiguous_phrases
    ) != bool(suggested_wording):
        raise WorkflowAmbiguityReviewError(
            "Reviewer ambiguity findings require source_sentences and "
            "suggested_wording."
        )
    confidence = payload.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise WorkflowAmbiguityReviewError(
            "Reviewer confidence must be a number from 0 to 1."
        )
    return WorkflowAmbiguityReview(
        definition=_required_text(identity.get("definition"), "definition"),
        definition_kind=_required_text(
            identity.get("definition_kind"), "definition_kind"
        ),
        step_id=identity.get("step_id")
        if isinstance(identity.get("step_id"), str)
        else None,
        step_index=_required_index(identity.get("step_index")),
        first_action=dict(first_action),
        completion_condition=completion_condition,
        allowed_actions=tuple(allowed_actions),
        missing_information=tuple(missing_information),
        conflicts=tuple(conflicts),
        ambiguous_phrases=tuple(ambiguous_phrases),
        source_sentences=tuple(source_sentences),
        suggested_wording=tuple(suggested_wording),
        confidence=float(confidence),
    )


def _load_definition(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise WorkflowAmbiguityReviewError(
            f"Could not read definition {path}: {exc}"
        ) from exc
    if not isinstance(data, Mapping):
        raise WorkflowAmbiguityReviewError("Definition must decode to an object.")
    if isinstance(data.get("steps"), list):
        return data, "skill"
    if isinstance(data.get("task_templates"), list):
        return data, "workflow_template"
    raise WorkflowAmbiguityReviewError(
        "Definition must contain steps or task_templates."
    )


def _select_step(
    steps: Sequence[Any], *, step_id: str | None, step_index: int | None
) -> int:
    if step_id is not None and step_index is not None:
        raise WorkflowAmbiguityReviewError(
            "Specify either step_id or step_index, not both."
        )
    if step_index is not None:
        if not 0 <= step_index < len(steps) or not isinstance(
            steps[step_index], Mapping
        ):
            raise WorkflowAmbiguityReviewError(
                f"step_index {step_index} is outside the definition."
            )
        return step_index
    if step_id is not None:
        matches = [
            index
            for index, step in enumerate(steps)
            if isinstance(step, Mapping) and step.get("id") == step_id
        ]
        if len(matches) != 1:
            raise WorkflowAmbiguityReviewError(
                f"No unique step with id {step_id!r} exists."
            )
        return matches[0]
    raise WorkflowAmbiguityReviewError(
        "Specify step_id or step_index for ambiguity review."
    )


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowAmbiguityReviewError(
            f"Reviewer {label} must be a non-empty string."
        )
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise WorkflowAmbiguityReviewError(
            f"Reviewer {label} must be a list of non-empty strings."
        )
    return list(value)


def _required_index(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkflowAmbiguityReviewError(
            "Reviewer step_index must be a non-negative integer."
        )
    return value
