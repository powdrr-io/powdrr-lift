from __future__ import annotations


def instructional_validation_message(
    message: str,
    *,
    code: str,
    path: str | None,
) -> str:
    """Make a validation issue directly usable as an LLM repair instruction."""
    location = f" at `{path}`" if path else ""
    normalized_code = code.casefold()
    if normalized_code in {
        "missing_entity_rationale_reference",
        "missing_relationship_rationale_reference",
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
            "evaluate command. Do not invent or reuse an outdated id."
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
    return (
        f"{message} Corrective action: edit the specification{location}; {action} "
        "Then rerun the same evaluate command to verify the correction."
    )
