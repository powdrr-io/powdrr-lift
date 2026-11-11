from __future__ import annotations

import jsonschema
import yaml
from powdrr_service.procedrr import parse_and_validate
from powdrr_service.resources import bundled_process_directory


def test_plan_repair_process_uses_typed_operation_key() -> None:
    process_path = bundled_process_directory() / "amend-plan-and-open-pr.yaml"
    source = process_path.read_text(encoding="utf-8")
    process = yaml.safe_load(source)
    parse_and_validate(source)
    operation_schema = process["steps"][0]["judge"]["output"]["schema"]["properties"][
        "operations"
    ]["items"]

    assert operation_schema["required"] == ["operation"]
    assert operation_schema["properties"]["operation"]["enum"] == [
        "add_item",
        "update_item",
        "retire_item",
        "set_dependency",
        "update_requirement",
        "assign_setup",
    ]

    jsonschema.validate(
        {"operation": "update_item", "item_id": "item-0004"}, operation_schema
    )
    try:
        jsonschema.validate(
            {"op": "update_item", "item_id": "item-0004"}, operation_schema
        )
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("typed repair schema must reject the `op` alias")
