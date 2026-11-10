"""Evidence-bound agreement with intended design, including architecture checks.

Behavioral execution evidence is supplied by a trusted validator, never inferred
from declarations in candidate source. Unsupported or missing evidence is unknown.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.design import DesignHandoff, subject_identity
from powdrr_service.structrr.reference_definitions import ReferenceDefinition
from powdrr_service.structrr.source_manifest import SourceManifest


def check_design_agreement(
    handoff: DesignHandoff,
    *,
    root: Path,
    manifest: SourceManifest,
    evidence: Sequence[Mapping[str, Any]] = (),
    explicit_only: bool = False,
) -> dict[str, Any]:
    """Evaluate every declared check and every required semantic subject.

    The manifest is a caller-owned, complete source inventory. Its contents are
    checked against disk before evaluation. This function executes no commands.
    Evidence must name one subject and the exact handoff and source content.
    """
    data = handoff.to_data()
    desired = data["desired"]
    findings: list[dict[str, Any]] = []
    errors = list(manifest.extraction_errors)
    root = root.resolve()
    files = {item.path: item for item in manifest.files}
    for observation in manifest.files:
        try:
            path = _source_path(root, observation.path)
            digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if (
                digest != observation.content_digest
                or bool(path.stat().st_mode & 0o111) != observation.executable
            ):
                errors.append(f"source changed since observation: {observation.path}")
        except (OSError, ValueError) as error:
            errors.append(str(error))

    by_subject: dict[str, list[Mapping[str, Any]]] = {}
    for item in evidence:
        key = item.get("subject")
        if isinstance(key, str):
            by_subject.setdefault(key, []).append(item)
        else:
            errors.append("evidence requires an identified subject")

    # Decisions, rationale and guidance explain the design. They require source
    # agreement only when they explicitly declare verification. Requirements,
    # invariants, features and structural subjects require evidence by default.
    required_sections = {
        "requirements",
        "invariants",
        "features",
        "entities",
        "entity_relationships",
        "source_subjects",
        "source_bindings",
        "files",
        "active_intent",
        "acceptance_criteria",
        "required_test_cases",
        "statements",
    }
    seen: set[str] = set()
    for section, records in desired["document"].items():
        if not isinstance(records, list):
            continue
        for record in records:
            canonical_required = False
            if section == "reference_definitions":
                definition = ReferenceDefinition.from_data(record)
                if definition.lifecycle != "active":
                    continue
                canonical_required = definition.kind in {"feature", "invariant"}
            subject = f"{section}:{subject_identity(section, record)}"
            checks = record.get("verification", [])
            if explicit_only and not checks:
                continue
            if (
                section not in required_sections
                and not canonical_required
                and not checks
                and record.get("required") is not True
            ):
                continue
            seen.add(subject)
            optional = record.get("required") is False
            status, reason = "unknown", "no current evidence for required design"
            if checks:
                if not isinstance(checks, list) or not all(
                    isinstance(check, Mapping) for check in checks
                ):
                    status, reason = "unknown", "verification must be a list of checks"
                else:
                    outcomes = [_check_source(root, check, files) for check in checks]
                    status = (
                        "contradicted"
                        if any(item[0] == "contradicted" for item in outcomes)
                        else "unknown"
                        if any(item[0] == "unknown" for item in outcomes)
                        else "satisfied"
                    )
                    reason = "; ".join(item[1] for item in outcomes)
            else:
                status, reason = _evidence_outcome(
                    by_subject.get(subject, []),
                    data["fingerprint"],
                    manifest.product_digest,
                )
            findings.append(
                {
                    "subject": subject,
                    "status": status,
                    "reason": reason,
                    "required": not optional,
                }
            )
    # Proposal-level acceptance, preservation and exclusions remain obligations.
    for section in (
        () if explicit_only else ("acceptance_criteria", "must_preserve", "non_goals")
    ):
        for ordinal, statement in enumerate(data["proposal"][section], 1):
            subject = f"proposal:{section}:{ordinal}"
            seen.add(subject)
            status, reason = _evidence_outcome(
                by_subject.get(subject, []),
                data["fingerprint"],
                manifest.product_digest,
            )
            findings.append(
                {
                    "subject": subject,
                    "statement": statement,
                    "status": status,
                    "reason": reason,
                    "required": True,
                }
            )
    for subject in sorted(set(by_subject) - seen):
        errors.append(f"evidence references unknown subject: {subject}")
    passed = (
        manifest.coverage_complete
        and not errors
        and all(item["status"] == "satisfied" for item in findings if item["required"])
    )
    report = {
        "schema_version": "structrr-design-agreement-v1",
        "scope": "explicit_source_checks"
        if explicit_only
        else "complete_desired_design",
        "handoff_fingerprint": data["fingerprint"],
        "desired_fingerprint": desired["fingerprint"],
        "candidate_product_digest": manifest.product_digest,
        "manifest_fingerprint": manifest.fingerprint,
        "passed": passed,
        "findings": findings,
        "errors": errors,
    }
    report["fingerprint"] = content_fingerprint(report)
    return report


def _evidence_outcome(
    matches: Sequence[Mapping[str, Any]], handoff_fingerprint: str, product_digest: str
) -> tuple[str, str]:
    if len(matches) != 1:
        return "unknown", "missing or duplicate evidence for subject"
    item = matches[0]
    refs = item.get("evidence_refs")
    if (
        item.get("handoff_fingerprint") != handoff_fingerprint
        or item.get("candidate_product_digest") != product_digest
        or item.get("status") not in {"satisfied", "contradicted", "unknown"}
        or not isinstance(refs, list)
        or not refs
        or not all(isinstance(ref, str) and ref.strip() for ref in refs)
        or not isinstance(item.get("reason"), str)
        or not item["reason"].strip()
    ):
        return "unknown", "evidence is stale, unbound, or incomplete"
    return str(item["status"]), str(item["reason"])


def _source_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    target = (root / path).resolve()
    if path.is_absolute() or ".." in path.parts or not target.is_relative_to(root):
        raise ValueError(f"source check path escapes repository: {relative}")
    return target


def _check_source(
    root: Path, check: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[str, str]:
    kind, relative = check.get("kind"), check.get("path")
    if not isinstance(relative, str) or not relative:
        return "unknown", "source check requires a path"
    if kind not in {"python-symbol", "python-import", "file-exists"}:
        return "unknown", f"unsupported source check: {kind!r}"
    present = check.get("present", True)
    if not isinstance(present, bool):
        return "unknown", "source check present must be boolean"
    try:
        path = _source_path(root, relative)
        if kind == "file-exists":
            actual = path.is_file()
            if actual and relative not in files:
                return "unknown", f"source file is absent from manifest: {relative}"
        else:
            if relative not in files:
                return "unknown", f"source file is absent from manifest: {relative}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if kind == "python-symbol":
                symbol = check.get("symbol")
                if not isinstance(symbol, str) or not symbol:
                    return "unknown", "python-symbol requires a qualified symbol"
                actual = symbol in _symbols(tree)
            else:
                module = check.get("module")
                if not isinstance(module, str) or not module:
                    return "unknown", "python-import requires a module"
                imports: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        prefix = "." * node.level + (node.module or "")
                        imports.append(prefix)
                        imports.extend(
                            prefix + ("" if prefix.endswith(".") else ".") + alias.name
                            for alias in node.names
                        )
                actual = any(
                    name == module or name.startswith(module + ".") for name in imports
                )
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        return "unknown", str(error)
    return (
        "satisfied" if actual == present else "contradicted",
        f"{kind} {relative}: expected present={present}, observed={actual}",
    )


def _symbols(tree: ast.AST, prefix: str = "") -> set[str]:
    result: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            result.add(name)
            result.update(_symbols(node, name + "."))
        else:
            result.update(_symbols(node, prefix))
    return result
