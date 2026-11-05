from __future__ import annotations

import hashlib
import json

import pytest
from powdrr_service.structrr.code_symbols import (
    CodeSymbolArtifact,
    CodeSymbolError,
    CodeSymbolQuery,
    build_python_symbol_artifact,
    load_code_symbol_artifact,
    query_python_references,
    query_python_symbols,
)

REVISION = "a" * 40
CONFIGURATION_FINGERPRINT = hashlib.sha256(b"pyproject.toml").hexdigest()


def _build_artifact() -> tuple[bytes, CodeSymbolArtifact, dict[str, object]]:
    return build_python_symbol_artifact(
        source_revision=REVISION,
        component_id="component:python:repository",
        provider_version="1.39.9",
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        symbols_by_path={
            "src/policy.py": [
                {
                    "name": "Policy",
                    "kind": "class",
                    "range": {
                        "start_line": 1,
                        "start_character": 0,
                        "end_line": 8,
                        "end_character": 0,
                    },
                    "children": [
                        {
                            "name": "parse",
                            "kind": "method",
                            "range": {
                                "start_line": 3,
                                "start_character": 4,
                                "end_line": 8,
                                "end_character": 0,
                            },
                        }
                    ],
                }
            ]
        },
        source_by_path={
            "src/policy.py": (
                "class Policy:\n    def parse(self) -> bool:\n        return True\n"
            )
        },
    )


def test_python_symbol_artifact_serialization_is_deterministic_and_validated() -> None:
    first = _build_artifact()
    second = _build_artifact()

    assert first[0] == second[0]
    assert first[1] == second[1]
    document = load_code_symbol_artifact(first[0], first[1].to_data())
    assert sorted(symbol["qualified_name"] for symbol in document["symbols"]) == [
        "Policy",
        "Policy.parse",
    ]
    method = next(item for item in document["symbols"] if item["name"] == "parse")
    assert method["signature"] == "def parse(self) -> bool"
    assert first[1].content_digest == hashlib.sha256(first[0]).hexdigest()
    assert first[1].symbol_count == 2


def test_same_named_symbols_in_different_paths_have_distinct_stable_ids() -> None:
    content, reference, _ = build_python_symbol_artifact(
        source_revision=REVISION,
        component_id="component:python:repository",
        provider_version="1.39.9",
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        symbols_by_path={
            path: [
                {
                    "name": "parse",
                    "kind": "function",
                    "range": {
                        "start_line": 1,
                        "start_character": 0,
                        "end_line": 2,
                        "end_character": 0,
                    },
                }
            ]
            for path in ("src/first.py", "src/second.py")
        },
        source_by_path={
            "src/first.py": "def parse(value: str) -> str:\n    return value\n",
            "src/second.py": "def parse(value: bytes) -> bytes:\n    return value\n",
        },
    )

    document = load_code_symbol_artifact(content, reference.to_data())

    assert len(document["symbols"]) == 2
    assert {symbol["path"] for symbol in document["symbols"]} == {
        "src/first.py",
        "src/second.py",
    }
    assert len({symbol["symbol_id"] for symbol in document["symbols"]}) == 2
    assert {symbol["qualified_name"] for symbol in document["symbols"]} == {"parse"}


def test_partial_symbol_artifact_lists_unindexed_source_paths() -> None:
    content, reference, document = build_python_symbol_artifact(
        source_revision=REVISION,
        component_id="component:python:repository",
        provider_version="1.39.9",
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        symbols_by_path={"src/indexed.py": []},
        source_by_path={
            "src/indexed.py": "# Indexed successfully; no declarations.\n",
            "src/unindexed.py": "def parse(value: str) -> str:\n    return value\n",
        },
    )

    loaded = load_code_symbol_artifact(content, reference.to_data())
    component = loaded["components"][0]
    coverage = loaded["coverage"][0]

    assert reference.coverage == "partial"
    assert component["coverage"] == "partial"
    assert coverage["status"] == "partial"
    assert coverage["indexed_paths"] == ["src/indexed.py"]
    assert coverage["unindexed_paths"] == ["src/unindexed.py"]
    assert document["coverage"] == loaded["coverage"]


def test_symbol_query_is_scoped_stable_and_bounded() -> None:
    content, reference, _ = _build_artifact()
    document = load_code_symbol_artifact(content, reference.to_data())
    query = CodeSymbolQuery(
        structrr_snapshot_id="structrr:sha256:snapshot",
        query_kind="symbols",
        component_ids=("component:python:repository",),
        paths=("src/policy.py",),
        symbol_names=("parse",),
        limit=10,
        max_encoded_bytes=8_000,
        purpose="locate policy parser",
    )

    result = query_python_symbols(document, query, artifact_id=reference.artifact_id)

    assert result["artifact_id"] == reference.artifact_id
    assert [symbol["qualified_name"] for symbol in result["records"]] == [
        "Policy.parse"
    ]
    assert result["filters"]["paths"] == ["src/policy.py"]
    assert result["truncated"] is False
    assert result["result_fingerprint"]


def test_symbol_query_reports_truncation_when_byte_limit_is_reached() -> None:
    content, reference, _ = _build_artifact()
    document = load_code_symbol_artifact(content, reference.to_data())
    query = CodeSymbolQuery(
        structrr_snapshot_id="structrr:sha256:snapshot",
        query_kind="symbols",
        limit=10,
        max_encoded_bytes=1_000,
    )

    result = query_python_symbols(document, query, artifact_id=reference.artifact_id)

    assert result["omitted_count"] >= 1
    assert result["truncated"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= 1_000


def test_reference_query_uses_bounded_pinned_file_scope() -> None:
    _content, reference, document = _build_artifact()
    query = CodeSymbolQuery(
        structrr_snapshot_id="structrr:sha256:snapshot",
        query_kind="references",
        paths=("src/policy.py",),
        relationship_kinds=("references",),
        limit=1,
        purpose="find planned cross-file dependencies",
    )

    result = query_python_references(
        document,
        query,
        artifact_id=reference.artifact_id,
        references_by_path={
            "src/policy.py": ("tests/test_policy.py", "src/policy.py"),
            "src/other.py": ("tests/test_other.py",),
        },
    )

    assert result["query_kind"] == "references"
    assert result["records"] == [
        {
            "source_path": "src/policy.py",
            "target_path": "tests/test_policy.py",
            "kind": "references",
            "provider_id": "basedpyright",
        }
    ]
    assert result["omitted_count"] == 0
    assert result["truncated"] is False


def test_artifact_loader_rejects_digest_revision_paths_and_duplicate_ids() -> None:
    content, reference, document = _build_artifact()
    reference_data = reference.to_data()

    with pytest.raises(CodeSymbolError, match="digest"):
        load_code_symbol_artifact(content + b"# corrupt\n", reference_data)

    wrong_revision = dict(reference_data, source_revision="b" * 40)
    with pytest.raises(CodeSymbolError, match="revision"):
        load_code_symbol_artifact(content, wrong_revision)

    duplicate = dict(document)
    symbols = document["symbols"]
    assert isinstance(symbols, list) and symbols
    duplicate["symbols"] = [*symbols, symbols[0]]
    import yaml

    duplicate_content = yaml.safe_dump(
        duplicate, sort_keys=False, allow_unicode=True, width=1_000_000
    ).encode()
    duplicate_reference = dict(
        reference_data,
        artifact_id=(
            "structrr-code-symbols:sha256:"
            + hashlib.sha256(duplicate_content).hexdigest()
        ),
        content_digest=f"sha256:{hashlib.sha256(duplicate_content).hexdigest()}",
    )
    with pytest.raises(CodeSymbolError, match="unique"):
        load_code_symbol_artifact(duplicate_content, duplicate_reference)


def test_symbol_artifact_rejects_path_traversal() -> None:
    with pytest.raises(CodeSymbolError, match="normalized repository paths"):
        build_python_symbol_artifact(
            source_revision=REVISION,
            component_id="component:python:repository",
            provider_version="1.39.9",
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
            symbols_by_path={"../secret.py": []},
            source_by_path={"../secret.py": ""},
        )
