from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class CorrectiveAction(ABC):
    """Typed repair behavior owned by a validation error category."""

    @abstractmethod
    def applies_to(self, code: str) -> bool:
        """Return whether this action handles the validation code."""

    @abstractmethod
    def instructions(self, error: ValidationError) -> str:
        """Return precise instructions for repairing the validation error."""


class RationaleReferenceAction(CorrectiveAction):
    _CODES = {
        "missing_entity_rationale_reference": "entity",
        "unknown_entity_rationale_reference": "entity",
        "missing_relationship_rationale_reference": "entity relationship",
        "unknown_relationship_rationale_reference": "entity relationship",
    }

    def applies_to(self, code: str) -> bool:
        return code.casefold() in self._CODES

    def instructions(self, error: ValidationError) -> str:
        subject = self._CODES[error.code.casefold()]
        return (
            "First use the workflow gather_context action exactly like "
            '`{"kind":"gather_context","types":["requirements"],'
            '"keywords":["<work-item-name>"],"filters":{}}` to retrieve '
            "the current requirement ids. Then reason about which returned "
            f"requirement drives this {subject}, edit its rationale to cite "
            "an exact returned requirement id in quotes, and rerun the same "
            "evaluate command. Replace any unknown or outdated id; do not "
            "invent one."
        )


class UnknownReferenceAction(CorrectiveAction):
    def applies_to(self, code: str) -> bool:
        normalized = code.casefold()
        return "unknown" in normalized or "unavailable" in normalized

    def instructions(self, error: ValidationError) -> str:
        location = f" at `{error.path}`" if error.path else ""
        return (
            f"The unknown id is identified by the validator{location}: "
            f"{error.message} Use gather_context to discover the current ids "
            "in the referenced section, then replace that id with an exact "
            "current id and rerun the same evaluate command."
        )


class DuplicateIdAction(CorrectiveAction):
    def applies_to(self, code: str) -> bool:
        return "duplicate" in code.casefold()

    def instructions(self, error: ValidationError) -> str:
        location = f" at `{error.path}`" if error.path else ""
        return (
            f"Remove or rename the duplicate identified by the validator{location}: "
            f"{error.message} Ensure every id is unique, then rerun the same "
            "evaluate command."
        )


class MissingValueAction(CorrectiveAction):
    def applies_to(self, code: str) -> bool:
        normalized = code.casefold()
        return "missing" in normalized or "required" in normalized

    def instructions(self, error: ValidationError) -> str:
        location = f" at `{error.path}`" if error.path else ""
        return (
            f"Add the missing field identified by the validator{location}: "
            f"{error.message} Use the expected type and a valid value, then "
            "rerun the same evaluate command."
        )


class ParseAction(CorrectiveAction):
    def applies_to(self, code: str) -> bool:
        normalized = code.casefold()
        return "parse" in normalized or "yaml" in normalized

    def instructions(self, error: ValidationError) -> str:
        line_match = re.search(r"line (\d+)", error.message, flags=re.IGNORECASE)
        line = f" on line {line_match.group(1)}" if line_match else ""
        return (
            f"Correct the YAML syntax{line} identified by the parser: "
            f"{error.message} Preserve the required mapping and sequence "
            "structure, then rerun the same evaluate command."
        )


class WorkflowToolAction(CorrectiveAction):
    def applies_to(self, code: str) -> bool:
        return code.casefold() == "workflow_tool_action_invalid"

    def instructions(self, error: ValidationError) -> str:
        return (
            "Return a corrected invoke_tool action using exactly one of the "
            "tool invocations declared by the current workflow step. Preserve "
            "the declared command structure and replace only invalid arguments; "
            "do not retry the rejected action unchanged."
        )


class GenericValidationAction(CorrectiveAction):
    def applies_to(self, code: str) -> bool:
        return True

    def instructions(self, error: ValidationError) -> str:
        location = f" at `{error.path}`" if error.path else ""
        return (
            f"Correct the value identified by the validator{location}: "
            f"{error.message} Then rerun the same evaluate command."
        )


_ACTIONS: tuple[CorrectiveAction, ...] = (
    RationaleReferenceAction(),
    UnknownReferenceAction(),
    DuplicateIdAction(),
    MissingValueAction(),
    ParseAction(),
    WorkflowToolAction(),
    GenericValidationAction(),
)


def _action_for(code: str) -> CorrectiveAction:
    return next(action for action in _ACTIONS if action.applies_to(code))


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A validator-produced failure that owns its repair instructions."""

    code: str
    message: str
    path: str | None = None
    _action: CorrectiveAction = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_action", _action_for(self.code))

    @property
    def corrective_action(self) -> str:
        return self._action.instructions(self)

    def instructional_message(self) -> str:
        location = f" at `{self.path}`" if self.path else ""
        return (
            f"{self.message} Corrective action: edit the specification{location}; "
            f"{self.corrective_action} Then rerun the same evaluate command to "
            "verify the correction."
        )


def validation_error_to_data(
    error: ValidationError,
    *,
    file_path: str = "<validated-file>",
) -> dict[str, Any]:
    """Serialize one validator-generated error without losing its repair action."""
    data: dict[str, Any] = {
        "code": error.code,
        "message": error.instructional_message(),
        "corrective_action": error.corrective_action,
        "yaml_edit_guidance": (
            "Use yaml_edit with structural operations after determining the exact "
            f"replacement for `{error.path or '<invalid-value>'}`; do not use a "
            "line-based edit on this YAML file."
        ),
    }
    if error.path is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", error.path):
        data["yaml_edit"] = {
            "kind": "yaml_edit",
            "file_path": file_path,
            "operations": [
                {
                    "op": "set_value",
                    "path": [error.path],
                    "value": "<correct-value>",
                }
            ],
        }
    if error.path is not None:
        data["path"] = error.path
    return data
