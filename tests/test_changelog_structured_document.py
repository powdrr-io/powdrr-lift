from __future__ import annotations

from types import SimpleNamespace

import pytest
from powdrr_service.changelog import (
    parse_structured_document,
    qualify_structured_document_issues,
)


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("schema: [", "structured_file_invalid_yaml"),
        ("- not-a-mapping", "structured_file_invalid_yaml"),
        ("schema: https://example.test/schema", "structured_file_invalid_schema"),
    ],
)
def test_parse_structured_document_rejects_invalid_envelopes(
    content: str,
    expected_code: str,
) -> None:
    result = parse_structured_document(
        content,
        path="docs/current/example/system-specification.yaml",
        expected_schema="https://powdrr.io/schemas/specification-v1",
    )

    assert result.mapping is None
    assert [issue.code for issue in result.issues] == [expected_code]
    assert result.issues[0].path == "docs/current/example/system-specification.yaml"


def test_parse_structured_document_returns_parsed_mapping() -> None:
    result = parse_structured_document(
        "schema: https://powdrr.io/schemas/specification-v1\nid: system-1\n",
        path="docs/current/example/system-specification.yaml",
        expected_schema="https://powdrr.io/schemas/specification-v1",
    )

    assert result.issues == ()
    assert result.mapping == {
        "schema": "https://powdrr.io/schemas/specification-v1",
        "id": "system-1",
    }


def test_qualify_structured_document_issues_adds_changelog_context() -> None:
    result = qualify_structured_document_issues(
        path="docs/current/example/system-specification.yaml",
        validator_name="system_specification",
        report_issues=(
            SimpleNamespace(code="system_id_missing", message="id required"),
        ),
    )

    assert result[0].code == "structured_file_system_specification_system_id_missing"
    assert result[0].message == (
        "Structured file docs/current/example/system-specification.yaml failed "
        "system_specification validation: id required"
    )
    assert result[0].path == "docs/current/example/system-specification.yaml"
