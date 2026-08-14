from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A validation failure with the repair instructions for that failure."""

    code: str
    message: str
    path: str | None = None
    corrective_action: str = ""

    def __post_init__(self) -> None:
        if not self.corrective_action:
            object.__setattr__(
                self,
                "corrective_action",
                _corrective_action(self.code),
            )

    def instructional_message(self) -> str:
        location = f" at `{self.path}`" if self.path else ""
        return (
            f"{self.message} Corrective action: edit the specification{location}; "
            f"{self.corrective_action} Then rerun the same evaluate command to "
            "verify the correction."
        )


def _corrective_action(code: str) -> str:
    """Return repair guidance at error construction time, not serialization time."""
    normalized_code = code.casefold()
    if normalized_code in {
        "missing_entity_rationale_reference",
        "missing_relationship_rationale_reference",
        "unknown_entity_rationale_reference",
        "unknown_relationship_rationale_reference",
    }:
        section = (
            "entity relationship" if "relationship" in normalized_code else "entity"
        )
        action = (
            "First use the workflow gather_context action exactly like "
            '`{"kind":"gather_context","types":["requirements"],'
            '"keywords":["<work-item-name>"],"filters":{}}` to retrieve '
            "the current requirement ids. Then reason about which returned "
            f"requirement drives this {section}, edit its rationale to cite "
            "an exact returned requirement id in quotes, and rerun the same "
            "evaluate command. Replace any unknown or outdated id; do not "
            "invent one."
        )
    elif "unknown" in normalized_code or "unavailable" in normalized_code:
        action = (
            "Replace the unknown reference with an id that is defined in the "
            "referenced section, or add that id to the referenced section."
        )
    elif "duplicate" in normalized_code:
        action = "Remove or rename the duplicate id so every id is unique."
    elif "missing" in normalized_code or "required" in normalized_code:
        action = "Add the missing required field or item with a valid value."
    elif "parse" in normalized_code or "yaml" in normalized_code:
        action = (
            "Correct the YAML syntax near this error and preserve the required "
            "mapping and sequence structure."
        )
    else:
        action = "Change the value to the type and format required by this error."
    return action


def validation_error_to_data(error: ValidationError) -> dict[str, str]:
    """Serialize one validator-generated error without losing its repair action."""
    data = {
        "code": error.code,
        "message": error.instructional_message(),
        "corrective_action": error.corrective_action,
    }
    if error.path is not None:
        data["path"] = error.path
    return data
