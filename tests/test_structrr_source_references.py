from __future__ import annotations

import hashlib
import json

import pytest
from powdrr_service.structrr.source_references import (
    SourceReferenceIndex,
    applicable_references,
    find_owner,
    index_python_source,
)


def index(source: str) -> SourceReferenceIndex:
    return index_python_source("retry.py", source.encode())


def codes(result: SourceReferenceIndex) -> list[str]:
    return [item.code for item in result.diagnostics]


def test_comments_only_case_preservation_and_opaque_ids() -> None:
    result = index('''"""# structrr: ignored:docstring"""
text = "# structrr: ignored:string"
# Ordinary prose mentioning structrr: ignored
# structrr: Feature/Exact.ID-_09
def run(): pass
''')
    assert not result.diagnostics
    assert [a.ref for a in result.annotations] == ["Feature/Exact.ID-_09"]
    assert result.annotations[0].owner == find_owner(result, "function", "run")


def test_fstring_expression_comments_are_not_source_annotations() -> None:
    result = index(
        'text = f"""{(\n# structrr: hidden\n42\n)}"""\n'
        "# structrr: visible\ndef run(): pass\n"
    )
    assert not result.diagnostics
    assert [a.ref for a in result.annotations] == ["visible"]


def test_nested_class_owners_keep_lexical_qualification() -> None:
    result = index("""# structrr: outer
class Outer:
    # structrr: inner
    class Inner:
        # structrr: method
        def run(self):
            pass
""")
    assert not result.diagnostics
    assert find_owner(result, "class", "Outer.Inner")
    assert find_owner(result, "function", "Outer.Inner.run")
    assert {a.ref for a in applicable_references(result, 7, 7)} == {
        "outer",
        "inner",
        "method",
    }


@pytest.mark.parametrize(
    "marker",
    [
        "# structrr:",
        "# structrr: two ids",
        "# structrr[range]: ref",
        "# structrr: naïve",
        "# structrr: " + "a" * 257,
        "# structrr prose",
        "# structrr: ref # extra",
        "value = 1  # structrr: ref",
    ],
)
def test_malformed_reserved_markers(marker: str) -> None:
    result = index(marker + "\ndef run(): pass\n")
    assert codes(result) == ["malformed_annotation"]
    assert not result.annotations


def test_maximum_id_and_whitespace() -> None:
    result = index(
        "# structrr:  " + "a" * 256 + "  \n# ordinary comment\ndef run(): pass\n"
    )
    assert result.annotations[0].ref == "a" * 256
    assert not result.diagnostics


def test_decorators_async_nested_classes_and_inherited_body_context() -> None:
    result = index("""# structrr[module]: module:retry
# structrr: class:policy
class Policy:
    # structrr: invariant:identity
    @decorate(
        option=True,
    )
    @other
    async def run(self):
        # structrr: decision:helper
        def helper():
            return "héllo"
        return helper()
""")
    assert not result.diagnostics
    owner = find_owner(result, "async_function", "Policy.run")
    assert owner and (owner.start_line, owner.end_line) == (5, 13)
    inherited = applicable_references(result, 12, 12)
    assert {a.ref for a in inherited} == {
        "module:retry",
        "class:policy",
        "invariant:identity",
        "decision:helper",
    }
    assert find_owner(result, "function", "Policy.run.helper") is not None


def test_block_compound_subtree_and_simple_statement_extent() -> None:
    result = index("""def run(value):
    # structrr[block]: condition
    if value:
        # structrr[block]: assignment
        answer = 1
        return answer
    return 0
""")
    assert not result.diagnostics
    assert {a.ref for a in applicable_references(result, 5, 5)} == {
        "condition",
        "assignment",
    }
    assert {a.ref for a in applicable_references(result, 6, 6)} == {"condition"}
    assert not applicable_references(result, 7, 7)


def test_multiple_scope_read_and_duplicate_effective_ids_retain_locations() -> None:
    result = index("""# structrr[module]: shared
# structrr: shared
def first():
    pass
# structrr: second
def second():
    pass
""")
    assert not result.diagnostics
    references = applicable_references(result, 4, 7)
    assert [a.ref for a in references] == ["second", "shared", "shared"]
    assert [a.line for a in references if a.ref == "shared"] == [1, 2]


@pytest.mark.parametrize(
    "source,expected",
    [
        ("# structrr: ref\n\ndef run(): pass\n", "dangling_annotation"),
        ("# structrr: ref\nvalue = 1\ndef run(): pass\n", "invalid_scope"),
        ("# structrr: ref\n", "dangling_annotation"),
        ("def run():\n    # structrr[module]: ref\n    pass\n", "invalid_scope"),
        ("value = 1\n# structrr[module]: ref\n", "invalid_scope"),
        (
            "# structrr: ref\n@decorator\n# structrr: other\ndef run(): pass\n",
            "dangling_annotation",
        ),
        (" # structrr: ref\ndef run(): pass\n", "dangling_annotation"),
    ],
)
def test_adjacency_and_scope_errors(source: str, expected: str) -> None:
    assert expected in codes(index(source))


def test_module_allows_shebang_encoding_and_docstring() -> None:
    result = index('''#!/usr/bin/env python
# coding: utf-8
"""module documentation"""
# structrr[module]: module:identity
import os
''')
    assert not result.diagnostics
    assert applicable_references(result, 5, 5)[0].ref == "module:identity"


def test_duplicate_owner_group_is_diagnostic_but_nested_reuse_is_allowed() -> None:
    result = index("""# structrr: ref
# explanatory comment
# structrr: ref
def run():
    # structrr[block]: ref
    pass
""")
    assert codes(result) == ["duplicate_annotation"]
    assert len(applicable_references(result, 6, 6)) == 3


def test_duplicate_qualified_names_require_span_disambiguation() -> None:
    result = index("""if condition:
    # structrr: first
    def run(): pass
else:
    # structrr: second
    def run(): pass
""")
    assert not result.diagnostics
    with pytest.raises(ValueError, match="ambiguous"):
        find_owner(result, "function", "run")
    assert [a.ref for a in applicable_references(result, 3, 3)] == ["first"]
    assert find_owner(result, "class", "missing") is None


@pytest.mark.parametrize(
    "source",
    [
        b"# structrr: ref\ndef broken(:\n",
        b"# structrr: ref\nx = '''unterminated\n",
        b"# structrr: ref\nx = (\n",
        b"# structrr: ref\ndef run():\n  pass\n pass\n",
    ],
)
def test_invalid_python_retains_discovered_comments_without_claiming_scope(
    source: bytes,
) -> None:
    result = index_python_source("broken.py", source)
    assert result.parse_status == "scope_resolution_failed"
    assert "scope_resolution_failed" in codes(result)
    assert result.annotations[0].ref == "ref"
    assert all(a.owner is None for a in result.annotations)
    assert not result.owners
    assert not applicable_references(result, 2, 2)
    assert applicable_references(result, 1, 1)[0].ref == "ref"


@pytest.mark.parametrize(
    "source",
    [b"\xff\x00", b"# coding: unknown-encoding\n", b"# coding: ascii\n# \xff\n"],
)
def test_decoding_failures_are_structured(source: bytes) -> None:
    result = index_python_source("broken.py", source)
    assert result.parse_status == "scope_resolution_failed"
    assert not result.annotations


def test_crlf_tabs_encoding_and_byte_digest() -> None:
    source = (
        b"# coding: latin-1\r\n# structrr: exact:ID\r\n"
        b"def run():\r\n\treturn '\xe9'\r\n"
    )
    result = index_python_source("retry.py", source)
    assert not result.diagnostics
    assert result.line_count == 4
    assert result.source_sha256 == "sha256:" + hashlib.sha256(source).hexdigest()
    assert applicable_references(result, 4, 4)[0].ref == "exact:ID"


def test_unicode_separator_in_literal_is_not_a_source_line_boundary() -> None:
    result = index("text = 'a\u2028b'\n# structrr: ref\ndef run(): pass\n")
    assert result.line_count == 3
    assert result.annotations[0].line == 2
    assert applicable_references(result, 3, 3)[0].ref == "ref"


def test_empty_and_unannotated_files_and_source_is_never_executed() -> None:
    assert index("").line_count == 0
    assert not index("raise RuntimeError('must not execute')\n").annotations
    assert not index("def run(): pass\n").diagnostics


def test_serialization_is_exact_and_rejects_invalid_or_inconsistent_records() -> None:
    result = index("# structrr: ref\ndef run(): pass\n")
    assert (
        SourceReferenceIndex.from_data(json.loads(json.dumps(result.to_data())))
        == result
    )
    for mutate in (
        lambda d: d.update(schema_version="future"),
        lambda d: d.update(line_count=True),
        lambda d: d.update(parse_status="scope_resolution_failed"),
        lambda d: d["annotations"][0].update(ref="bad id"),
        lambda d: d["annotations"][0]["owner"].update(end_line=999),
        lambda d: d.update(extra="unexpected"),
    ):
        data = result.to_data()
        mutate(data)
        with pytest.raises(ValueError):
            SourceReferenceIndex.from_data(data)


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (1, 999), (True, 1)])
def test_invalid_read_ranges(start: int, end: int) -> None:
    with pytest.raises(ValueError):
        applicable_references(index("pass\n"), start, end)


@pytest.mark.parametrize(
    "path", ["/outside.py", "../outside.py", "src/file.ts", "src\\file.py", ""]
)
def test_invalid_configuration_paths_fail_explicitly(path: str) -> None:
    with pytest.raises(ValueError):
        index_python_source(path, b"pass\n")
