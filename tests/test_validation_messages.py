from powdrr_lift.core.validation_messages import (
    ValidationError,
    validation_error_to_data,
)


def test_validation_error_only_includes_yaml_edit_for_yaml_files() -> None:
    error = ValidationError("missing_value", "A value is required.", "id")

    yaml_data = validation_error_to_data(error, file_path="specification.yaml")
    json_data = validation_error_to_data(error, file_path="report.json")

    assert "yaml_edit" in yaml_data
    assert "yaml_edit_guidance" in yaml_data
    assert "multiple independent issues" in yaml_data["yaml_edit_guidance"]
    assert '"operations":[{"op":"set_value"' in yaml_data["yaml_edit_guidance"]
    assert '"path":["title"]' in yaml_data["yaml_edit_guidance"]
    assert "yaml_edit" not in json_data
    assert "yaml_edit_guidance" not in json_data


def test_boilerplate_issue_includes_indexed_yaml_removal() -> None:
    error = ValidationError(
        "boilerplate_not_removed",
        "Remove the requirements boilerplate placeholder entry.",
        "requirements[0]",
    )

    data = validation_error_to_data(error, file_path="system-specification.yaml")

    assert data["yaml_edit"]["operations"] == [
        {"op": "remove_item", "section": "requirements", "index": 0}
    ]


def test_unknown_id_correction_includes_gather_context_action() -> None:
    error = ValidationError(
        "unknown_supercedes_id",
        "The referenced requirement id is not available.",
        "requirements[0].supercedes[0]",
    )

    data = validation_error_to_data(error, file_path="system-specification.yaml")

    assert '"action":"gather_context"' in data["corrective_action"]
    assert '"types":["requirements"]' in data["corrective_action"]
    assert "exact returned id" in data["corrective_action"]


def test_missing_id_correction_includes_gather_context_action() -> None:
    error = ValidationError(
        "section_item_id_missing",
        "A section item must include an id.",
        "requirements[0].id",
    )

    data = validation_error_to_data(error, file_path="system-specification.yaml")

    assert '"action":"gather_context"' in data["corrective_action"]
    assert '"types":["requirements"]' in data["corrective_action"]
