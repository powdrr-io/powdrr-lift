"""Evaluate static validation discovery against a reviewed repository corpus."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from powdrr_service.structrr.validation import discover_validation_profiles

_REVISION = re.compile(r"^[0-9a-f]{40}$")


def validate_corpus_definition(value: object) -> tuple[str, ...]:
    """Return structural issues in a pinned, manually reviewed corpus file."""
    if not isinstance(value, Mapping):
        return ("corpus root must be a mapping",)
    if value.get("schema_version") != "validation-corpus-v1":
        return ("schema_version must be validation-corpus-v1",)
    repositories = value.get("repositories")
    if not isinstance(repositories, Sequence) or isinstance(repositories, (str, bytes)):
        return ("repositories must be a list",)
    issues: list[str] = []
    ids: set[str] = set()
    for index, entry in enumerate(repositories):
        path = f"repositories[{index}]"
        if not isinstance(entry, Mapping):
            issues.append(f"{path} must be a mapping")
            continue
        repository_id = entry.get("id")
        if not isinstance(repository_id, str) or not repository_id.strip():
            issues.append(f"{path}.id must be non-empty")
        elif repository_id in ids:
            issues.append(f"{path}.id duplicates {repository_id!r}")
        else:
            ids.add(repository_id)
        if not isinstance(entry.get("repository"), str) or not str(
            entry.get("repository", "")
        ).startswith("https://github.com/"):
            issues.append(f"{path}.repository must be a GitHub HTTPS URL")
        revision = entry.get("revision")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            issues.append(f"{path}.revision must be a full 40-character commit SHA")
        if not isinstance(entry.get("license"), str) or not entry.get("license"):
            issues.append(f"{path}.license must record the reviewed SPDX identifier")
        if not isinstance(entry.get("license_evidence"), str) or not entry.get(
            "license_evidence"
        ):
            issues.append(f"{path}.license_evidence must explain the license source")
        for field in ("coverage", "evidence", "obligations"):
            items = entry.get(field)
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                issues.append(f"{path}.{field} must be a list")
            elif field != "obligations" and any(
                not isinstance(item, str) or not item.strip() for item in items
            ):
                issues.append(f"{path}.{field} must contain non-empty strings")
        obligations = entry.get("obligations", ())
        if isinstance(obligations, Sequence) and not isinstance(
            obligations, (str, bytes)
        ):
            obligation_ids: set[str] = set()
            for obligation_index, obligation in enumerate(obligations):
                obligation_path = f"{path}.obligations[{obligation_index}]"
                if not isinstance(obligation, Mapping):
                    issues.append(f"{obligation_path} must be a mapping")
                    continue
                obligation_id = obligation.get("id")
                if not isinstance(obligation_id, str) or not obligation_id.strip():
                    issues.append(f"{obligation_path}.id must be non-empty")
                elif obligation_id in obligation_ids:
                    issues.append(f"{obligation_path}.id duplicates {obligation_id!r}")
                else:
                    obligation_ids.add(obligation_id)
                if obligation.get("state") not in {"executable", "unresolved"}:
                    issues.append(
                        f"{obligation_path}.state must be executable or unresolved"
                    )
                if obligation.get("requiredness") not in {"unknown", "required"}:
                    issues.append(
                        f"{obligation_path}.requiredness must be unknown or required"
                    )
                if not isinstance(obligation.get("profile"), str):
                    issues.append(f"{obligation_path}.profile must be a string")
                command = obligation.get("command")
                if (
                    not isinstance(command, Sequence)
                    or isinstance(command, (str, bytes))
                    or any(not isinstance(token, str) for token in command)
                ):
                    issues.append(f"{obligation_path}.command must be a token list")
                if not isinstance(obligation.get("cwd", "."), str):
                    issues.append(f"{obligation_path}.cwd must be a string")
                evidence = obligation.get("evidence", ())
                if (
                    not isinstance(evidence, Sequence)
                    or isinstance(evidence, (str, bytes))
                    or any(
                        not isinstance(path, str) or not path.strip()
                        for path in evidence
                    )
                ):
                    issues.append(f"{obligation_path}.evidence must be a list of paths")
    return tuple(issues)


def evaluate_repository(entry: Mapping[str, Any], root: str | Path) -> dict[str, Any]:
    """Compare manually reviewed obligations with one repository checkout."""
    repository_root = Path(root).resolve()
    profiles = discover_validation_profiles(repository_root)
    expected = tuple(entry.get("obligations", ()))
    used_profiles: set[int] = set()
    missed: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    provider_mismatches: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, Any]] = []
    expected_unresolved = 0
    exact_executable = 0

    for obligation in expected:
        if not isinstance(obligation, Mapping):
            continue
        obligation_id = str(obligation.get("id", ""))
        state = str(obligation.get("state", "executable"))
        if state == "unresolved":
            expected_unresolved += 1
        candidates = [
            (index, profile)
            for index, profile in enumerate(profiles)
            if profile.name == obligation.get("profile")
        ]
        if not candidates:
            missed.append(
                {
                    "obligation": obligation_id,
                    "profile": obligation.get("profile"),
                    "requiredness": obligation.get("requiredness", "unknown"),
                }
            )
            continue
        index, profile = candidates[0]
        used_profiles.add(index)
        expected_command = tuple(obligation.get("command", ()))
        expected_cwd = str(obligation.get("cwd", "."))
        actual_cwd = profile.cwd or "."
        command_matches = (
            profile.command == expected_command if state == "executable" else True
        )
        context_matches = actual_cwd == expected_cwd
        if not command_matches or not context_matches:
            mismatches.append(
                {
                    "obligation": obligation_id,
                    "profile": profile.name,
                    "expected_command": list(expected_command),
                    "actual_command": list(profile.command),
                    "expected_cwd": expected_cwd,
                    "actual_cwd": actual_cwd,
                }
            )
        expected_provider = obligation.get("provider")
        if expected_provider and profile.provider != expected_provider:
            provider_mismatches.append(
                {
                    "obligation": obligation_id,
                    "profile": profile.name,
                    "expected_provider": expected_provider,
                    "actual_provider": profile.provider,
                }
            )
        expected_evidence = obligation.get("evidence", ())
        observed_evidence = set(profile.evidence)
        missing_paths = [
            path
            for path in expected_evidence
            if (f"validation-input:{path}" not in observed_evidence)
            and (path not in observed_evidence)
        ]
        for path in missing_paths:
            evidence_gaps.append(
                {
                    "obligation": obligation_id,
                    "profile": profile.name,
                    "path": path,
                    "file_exists": (repository_root / path).is_file(),
                }
            )
        if state == "unresolved":
            if not profile.unresolved:
                mismatches.append(
                    {
                        "obligation": obligation_id,
                        "profile": profile.name,
                        "expected_state": "unresolved",
                        "actual_state": "executable",
                    }
                )
        elif profile.unresolved:
            mismatches.append(
                {
                    "obligation": obligation_id,
                    "profile": profile.name,
                    "expected_state": "executable",
                    "actual_state": "unresolved",
                    "reasons": list(profile.unresolved),
                }
            )
        elif command_matches and context_matches:
            exact_executable += 1

    unexpected = [
        {
            "profile": profile.name,
            "command": list(profile.command),
            "cwd": profile.cwd,
            "requiredness": (
                profile.requiredness.get("status", "unknown")
                if profile.requiredness
                else "unknown"
            ),
        }
        for index, profile in enumerate(profiles)
        if index not in used_profiles
    ]
    invented_required = [
        profile for profile in unexpected if profile["requiredness"] == "required"
    ]
    unresolved = [
        {
            "profile": profile.name,
            "reasons": list(profile.unresolved),
        }
        for profile in profiles
        if profile.unresolved
    ]
    return {
        "id": entry.get("id"),
        "repository": entry.get("repository"),
        "revision": entry.get("revision"),
        "license": entry.get("license"),
        "expected_obligations": len(expected),
        "missed_obligations": missed,
        "invented_required_checks": invented_required,
        "unexpected_discoveries": unexpected,
        "command_context_mismatches": mismatches,
        "provider_mismatches": provider_mismatches,
        "evidence_gaps": evidence_gaps,
        "expected_unresolved_obligations": expected_unresolved,
        "observed_unresolved_profiles": unresolved,
        "exact_executable_discoveries": exact_executable,
        "discovered_profiles": len(profiles),
        "unreviewed_discoveries": len(unexpected),
    }


def render_corpus_markdown(report: Mapping[str, Any]) -> str:
    """Render a concise corpus report with separate completeness measures."""
    repositories = report.get("repositories", ())
    lines = [
        "# Validation discovery corpus evaluation",
        "",
        f"Corpus revision: `{report.get('corpus_revision', 'unspecified')}`.",
        "",
        "This is static discovery evidence. It does not establish that a command "
        "was executed or that a remote check is required.",
        "",
        "| "
        + " | ".join(
            (
                "Repository",
                "Pinned revision",
                "License",
                "Exact argv matches",
                "Missed obligations",
                "Unreviewed discoveries",
                "Invented required checks",
                "Command/context mismatches",
                "Provider mismatches",
                "Evidence gaps",
                "Unresolved profiles",
            )
        )
        + " |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in repositories:
        cells = (
            f"[{item.get('id', 'unknown')}]({item.get('repository', '')})",
            f"`{str(item.get('revision', 'unknown'))[:12]}`",
            f"`{item.get('license', 'unknown')}`",
            str(item.get("exact_executable_discoveries", 0)),
            str(len(item.get("missed_obligations", ()))),
            str(item.get("unreviewed_discoveries", 0)),
            str(len(item.get("invented_required_checks", ()))),
            str(len(item.get("command_context_mismatches", ()))),
            str(len(item.get("provider_mismatches", ()))),
            str(len(item.get("evidence_gaps", ()))),
            str(len(item.get("observed_unresolved_profiles", ()))),
        )
        lines.append("| " + " | ".join(cells) + " |")
    totals = report.get("totals", {})
    total_lines = (
        ("Expected obligations", "expected_obligations"),
        ("Exact executable discoveries", "exact_executable_discoveries"),
        ("Missed obligations", "missed_obligations"),
        ("Unreviewed discovery candidates", "unreviewed_discoveries"),
        ("Invented required checks", "invented_required_checks"),
        ("Command/context mismatches", "command_context_mismatches"),
        ("Evidence gaps", "evidence_gaps"),
        ("Expected unresolved obligations", "expected_unresolved_obligations"),
        ("Observed unresolved profiles", "observed_unresolved_profiles"),
    )
    lines.extend(
        [
            "",
            "## Totals",
            "",
            *(f"- {label}: {totals.get(key, 0)}" for label, key in total_lines),
        ]
    )
    for item in repositories:
        details = (
            ("Missed obligations", item.get("missed_obligations", ())),
            ("Command/context mismatches", item.get("command_context_mismatches", ())),
            ("Provider mismatches", item.get("provider_mismatches", ())),
            ("Evidence gaps", item.get("evidence_gaps", ())),
        )
        if not any(rows for _, rows in details):
            continue
        lines.extend(["", f"## {item.get('id', 'unknown')}", ""])
        for heading, rows in details:
            if not rows:
                continue
            lines.extend([f"### {heading}", ""])
            for row in rows:
                lines.append(f"- `{json.dumps(row, sort_keys=True)}`")
        unresolved_rows = item.get("observed_unresolved_profiles", ())
        if unresolved_rows:
            lines.extend(["### Sample unresolved profiles", ""])
            for row in unresolved_rows[:5]:
                lines.append(f"- `{json.dumps(row, sort_keys=True)}`")
            if len(unresolved_rows) > 5:
                lines.append(f"- … and {len(unresolved_rows) - 5} more.")
    return "\n".join(lines) + "\n"


__all__ = [
    "evaluate_repository",
    "render_corpus_markdown",
    "validate_corpus_definition",
]
