"""Compile typed Python link declarations into assertion-level story evidence.

The author declares ``__powdrr_story_links__`` using Python literals. The service
writes the versioned manifest after checking actual collection and executable
assertion spans. Comments and author-supplied selectors have no authority.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.story_coverage import (
    StoryCoverageError,
    parse_story_case_refs,
)

LINK_SCHEMA = "story-test-links-v1"


def python_test_assertions(test: ast.FunctionDef) -> dict[int, ast.AST]:
    result: dict[int, ast.AST] = {}
    pending: list[ast.AST] = list(test.body)
    while pending:
        node = pending.pop()
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant):
                raise StoryCoverageError(
                    "constant-only assertion cannot establish story coverage"
                )
            result[node.lineno] = node
        elif isinstance(node, ast.With):
            if any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and isinstance(item.context_expr.func.value, ast.Name)
                and item.context_expr.func.value.id == "pytest"
                and item.context_expr.func.attr == "raises"
                for item in node.items
            ):
                result[node.lineno] = node
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr
                in {
                    "assertEqual",
                    "assertNotEqual",
                    "assertTrue",
                    "assertFalse",
                    "assertIn",
                    "assertNotIn",
                    "assertIs",
                    "assertIsNone",
                    "assertIsNotNone",
                    "assertRaises",
                }
            ):
                result[node.lineno] = node
        pending.extend(ast.iter_child_nodes(node))
    return result


def compile_story_test_links(
    files: Mapping[str, str],
    obligations: Sequence[Mapping[str, Any]],
    collected_selectors: Sequence[str],
) -> dict[str, Any]:
    """Resolve links against pinned obligations and real collected pytest items."""
    catalog = {
        record["obligation_id"]: record
        for record in obligations
        if record.get("story_refs")
    }
    if not catalog:
        return {}
    if len(catalog) != sum(bool(record.get("story_refs")) for record in obligations):
        raise StoryCoverageError("duplicate story test obligation")
    expected = {
        (identity, observation)
        for identity, record in catalog.items()
        for ref in parse_story_case_refs(record["story_refs"])
        for observation in ref.observation_ids
    }
    links = []
    covered: set[tuple[str, str]] = set()
    actual = set(collected_selectors)
    if not actual:
        raise StoryCoverageError("story tests require actual pytest collection")
    for path, source in sorted(files.items()):
        tree = ast.parse(source, filename=path)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        declarations = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__powdrr_story_links__"
                for target in node.targets
            )
        ]
        if not declarations:
            continue
        if len(declarations) != 1:
            raise StoryCoverageError(
                f"{path}: require one literal __powdrr_story_links__ declaration"
            )
        try:
            requests = ast.literal_eval(declarations[0].value)
        except (ValueError, TypeError, SyntaxError) as error:
            raise StoryCoverageError(
                "story links require typed literal declarations"
            ) from error
        if not isinstance(requests, dict):
            raise StoryCoverageError(
                "story links must map real test function names to link requests"
            )
        for name, records in requests.items():
            if name not in functions or not isinstance(records, list) or not records:
                raise StoryCoverageError(f"unknown or empty story-linked test: {name}")
            node = functions[name]
            assertions = python_test_assertions(node)
            base_selector = path + "::" + name
            selectors = sorted(
                selector
                for selector in actual
                if selector.split("[", 1)[0] == base_selector
            )
            if not selectors:
                raise StoryCoverageError(
                    f"uncollected story test selector: {base_selector}"
                )
            for request in records:
                if not isinstance(request, Mapping) or set(request) != {
                    "obligation_id",
                    "observations",
                    "description",
                    "boundary",
                    "substitutions",
                }:
                    raise StoryCoverageError(
                        "story link request has unknown or missing fields"
                    )
                identity = request["obligation_id"]
                if not isinstance(identity, str) or identity not in catalog:
                    raise StoryCoverageError(f"unknown story obligation: {identity}")
                for field in ("description", "boundary"):
                    if (
                        not isinstance(request[field], str)
                        or not request[field].strip()
                        or len(request[field]) > 2000
                    ):
                        raise StoryCoverageError(
                            f"story assertion {field} must be bounded nonempty text"
                        )
                substitutions = request["substitutions"]
                if not isinstance(substitutions, list) or any(
                    not isinstance(item, str) or not item.strip()
                    for item in substitutions
                ):
                    raise StoryCoverageError(
                        "substitutions must describe external dependency boundaries"
                    )
                observations = request["observations"]
                if not isinstance(observations, Mapping) or not observations:
                    raise StoryCoverageError(
                        "story links require observation/assertion pairs"
                    )
                spans = []
                for observation, lines in observations.items():
                    if (identity, observation) not in expected:
                        raise StoryCoverageError(
                            f"unknown observation {observation} for {identity}"
                        )
                    if (
                        not isinstance(lines, list)
                        or not lines
                        or any(
                            isinstance(line, bool)
                            or not isinstance(line, int)
                            or line not in assertions
                            for line in lines
                        )
                        or len(set(lines)) != len(lines)
                    ):
                        raise StoryCoverageError(
                            f"{base_selector}: observation {observation} must name "
                            "executable assertion start lines in that test"
                        )
                    for line in lines:
                        assertion = assertions[line]
                        spans.append(
                            {
                                "observation_id": observation,
                                "start_line": line,
                                "end_line": assertion.end_lineno,
                                "description": request["description"],
                            }
                        )
                    covered.add((identity, observation))
                for selector in selectors:
                    links.append(
                        {
                            "selector": selector,
                            "path": path,
                            "test_name": name,
                            "test_source_digest": "sha256:"
                            + hashlib.sha256(source.encode()).hexdigest(),
                            "obligation_id": identity,
                            "story_refs": catalog[identity]["story_refs"],
                            "assertions": spans,
                            "boundary": request["boundary"],
                            "substitutions": substitutions,
                        }
                    )
    missing = sorted(expected - covered)
    if missing:
        raise StoryCoverageError(
            f"story observations lack collected assertion links: {missing}"
        )
    data = {
        "schema_version": LINK_SCHEMA,
        "obligation_catalog_digest": content_fingerprint(list(obligations)),
        "links": links,
    }
    return {**data, "fingerprint": content_fingerprint(data)}


def story_link_coverage(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    for link in manifest.get("links", []):
        result.setdefault(link["obligation_id"], set()).add(link["selector"])
    return {key: sorted(values) for key, values in sorted(result.items())}


def require_story_test_quality(
    report: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    """Story assertion adequacy is a gate, including unavailable review results."""
    from powdrr_service.verification.execute_pr_test_quality import STORY_QUALITY_CHECKS

    expected = {
        (link["selector"], link["obligation_id"], check_id)
        for link in manifest.get("links", [])
        for check_id, _ in STORY_QUALITY_CHECKS
    }
    assessments = report.get("assessments", [])
    accepted = set()
    for assessment in assessments:
        ids = assessment.get("criterion_ids", [])
        for identity in ids:
            key = (assessment.get("test_id"), identity, assessment.get("check_id"))
            if key not in expected:
                continue
            if (
                assessment.get("decision_error")
                or assessment.get("decision_reports_shortcoming") is not False
            ):
                raise StoryCoverageError(
                    f"story test review blocked: {key}; "
                    f"{assessment.get('decision_error') or 'assertion shortcoming'}"
                )
            accepted.add(key)
    if expected - accepted:
        raise StoryCoverageError(
            f"story test review unavailable for {sorted(expected - accepted)}"
        )
