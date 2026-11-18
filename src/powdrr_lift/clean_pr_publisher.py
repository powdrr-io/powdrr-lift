"""Publish selected implementation files from a clean branch."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from powdrr_service.changelog import parse_change_log
from powdrr_service.changelog.template import is_structured_document_path

from powdrr_lift.errors import PowdrrExecutionError

_STANDARD_PR_DESCRIPTION_HEADINGS = (
    "## Objective",
    "## Scope",
    "## Implementation Details",
    "## Acceptance Criteria",
    "## Validation",
    "## Advisory Test-Quality Notes",
)
_STANDARD_PR_DESCRIPTION_SECTIONS = (
    ("objective", _STANDARD_PR_DESCRIPTION_HEADINGS[0]),
    ("scope", _STANDARD_PR_DESCRIPTION_HEADINGS[1]),
    ("implementation_details", _STANDARD_PR_DESCRIPTION_HEADINGS[2]),
    ("acceptance_criteria", _STANDARD_PR_DESCRIPTION_HEADINGS[3]),
    ("validation", _STANDARD_PR_DESCRIPTION_HEADINGS[4]),
    ("advisory_test_quality_notes", _STANDARD_PR_DESCRIPTION_HEADINGS[5]),
)


def build_publication_authorization(
    *,
    base: str,
    files: Sequence[str],
    accepted_contract_digest: str,
    verification_receipts: Sequence[Mapping[str, Any]],
    lineage: Mapping[str, Any],
    worktree_root: Path,
) -> dict[str, Any]:
    """Bind publication to the exact current candidate and its accepted evidence."""
    normalized_files = sorted(_relative_paths(files))
    if (
        not isinstance(accepted_contract_digest, str)
        or not accepted_contract_digest.strip()
    ):
        raise PowdrrExecutionError("accepted_contract_digest must be non-empty.")
    if not verification_receipts or any(
        not isinstance(receipt, Mapping) or receipt.get("passed") is not True
        for receipt in verification_receipts
    ):
        raise PowdrrExecutionError(
            "Publication requires successful verification receipts for the candidate."
        )
    if not isinstance(lineage, Mapping) or not lineage:
        raise PowdrrExecutionError("Publication requires source-run lineage.")
    root = worktree_root.resolve()
    content: dict[str, str] = {}
    for relative in normalized_files:
        path = root / relative
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise PowdrrExecutionError(
                f"Publication path escapes the worktree: {relative}"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise PowdrrExecutionError(
                f"Publication candidate must contain regular files: {relative}"
            )
        content[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    base_ref = _resolve_base_ref(root, _branch(base, field="base"))
    base_commit = _run(["git", "rev-parse", f"{base_ref}^{{commit}}"], cwd=root).strip()
    body = {
        "schema_version": "powdrr-publication-authorization-v1",
        "base": base,
        "base_commit": base_commit,
        "candidate_content": content,
        "candidate_digest": hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "accepted_contract_digest": accepted_contract_digest,
        "changed_paths": normalized_files,
        "verification_receipts": [dict(receipt) for receipt in verification_receipts],
        "lineage": dict(lineage),
    }
    body["authorization_digest"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _validate_publication_authorization(
    authorization: object,
    *,
    base: str,
    files: Sequence[str],
    worktree_root: Path,
) -> dict[str, Any]:
    if not isinstance(authorization, Mapping):
        raise PowdrrExecutionError("A publication authorization is required.")
    required = {
        "schema_version",
        "base",
        "base_commit",
        "candidate_content",
        "candidate_digest",
        "accepted_contract_digest",
        "changed_paths",
        "verification_receipts",
        "lineage",
        "authorization_digest",
    }
    if set(authorization) != required:
        raise PowdrrExecutionError("Publication authorization has an invalid shape.")
    expected = build_publication_authorization(
        base=base,
        files=files,
        accepted_contract_digest=str(authorization.get("accepted_contract_digest", "")),
        verification_receipts=authorization.get("verification_receipts", []),
        lineage=authorization.get("lineage", {}),
        worktree_root=worktree_root,
    )
    if dict(authorization) != expected:
        raise PowdrrExecutionError(
            "Publication candidate changed after verification; regenerate its "
            "authorization."
        )
    return expected


def render_standard_pr_description(sections: Mapping[str, Any]) -> str:
    """Render model-authored section content into Powdrr's fixed PR template."""
    expected_keys = {name for name, _ in _STANDARD_PR_DESCRIPTION_SECTIONS}
    if set(sections) != expected_keys:
        missing = sorted(expected_keys - set(sections))
        unexpected = sorted(set(sections) - expected_keys)
        raise PowdrrExecutionError(
            "PR description sections must contain exactly the standard fields; "
            f"missing={missing!r}, unexpected={unexpected!r}."
        )

    rendered: list[str] = []
    for name, heading in _STANDARD_PR_DESCRIPTION_SECTIONS:
        content = sections[name]
        if not isinstance(content, str) or not content.strip():
            raise PowdrrExecutionError(
                f"PR description section {name!r} must contain non-empty text."
            )
        rendered.append(f"{heading}\n\n{content.strip()}")
    body = "\n\n".join(rendered)
    validate_standard_pr_description(body)
    return body


def validate_standard_pr_description(body: str) -> None:
    """Require the section structure used by Powdrr-generated PR descriptions."""
    expected_start = f"{_STANDARD_PR_DESCRIPTION_HEADINGS[0]}\n\n"
    if not body.startswith(expected_start):
        raise PowdrrExecutionError(
            "PR description must start with the required Objective heading and a "
            "blank line."
        )

    content_start = len(expected_start)
    for heading in _STANDARD_PR_DESCRIPTION_HEADINGS[1:]:
        marker = f"\n\n{heading}\n\n"
        heading_start = body.find(marker, content_start)
        if heading_start < 0:
            raise PowdrrExecutionError(
                "PR description must contain the required headings in order, each "
                "on its own line with a blank line after it."
            )
        if not body[content_start:heading_start].strip():
            raise PowdrrExecutionError(
                f"PR description section before {heading!r} must not be empty."
            )
        content_start = heading_start + len(marker)

    if not body[content_start:].strip():
        raise PowdrrExecutionError(
            "PR description's Advisory Test-Quality Notes section must not be empty."
        )


def render_issue_worker_publication_request(
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Render an implementation or plan-amendment PR request from typed evidence."""
    kind = _required_text(parameters.get("publication_kind"), "publication_kind")
    task_context = _required_mapping(parameters.get("task_context"), "task_context")
    input_state = task_context.get("input_state")
    if not isinstance(input_state, Mapping):
        input_state = task_context
    proposed = input_state.get("proposed_pr_context")
    if not isinstance(proposed, Mapping):
        proposed = task_context.get("proposed_pr_context")
    proposed = _required_mapping(proposed, "proposed_pr_context")
    proposed_pr_id = _required_text(
        task_context.get("proposed_pr_id", input_state.get("proposed_pr_id")),
        "proposed_pr_id",
    )
    item_title = _required_text(proposed.get("title"), "proposed_pr_context.title")
    if kind == "implementation":
        return _render_implementation_publication_request(
            proposed=proposed,
            proposed_pr_id=proposed_pr_id,
            implementation_result=_required_mapping(
                parameters.get("implementation_result"), "implementation_result"
            ),
            validation_results=parameters.get("validation_results"),
        )
    if kind == "plan-amendment":
        return _render_plan_amendment_publication_request(
            proposed=proposed,
            proposed_pr_id=proposed_pr_id,
            item_title=item_title,
            failure_review=_required_mapping(
                parameters.get("failure_review"), "failure_review"
            ),
            repair_request=_required_mapping(
                parameters.get("repair_request"), "repair_request"
            ),
            amendment_result=_required_mapping(
                parameters.get("amendment_result"), "amendment_result"
            ),
            validation_results=parameters.get("validation_results"),
        )
    raise PowdrrExecutionError(
        "publication_kind must be 'implementation' or 'plan-amendment'."
    )


def _render_implementation_publication_request(
    *,
    proposed: Mapping[str, Any],
    proposed_pr_id: str,
    implementation_result: Mapping[str, Any],
    validation_results: object,
) -> dict[str, Any]:
    structrr = _required_mapping(
        implementation_result.get("structrr"), "implementation_result.structrr"
    )
    if structrr.get("passed") is not True:
        raise PowdrrExecutionError("Implementation Structrr validation did not pass.")
    integration_tests = implementation_result.get("integration_tests")
    if (
        not isinstance(integration_tests, Mapping)
        or integration_tests.get("passed") is not True
    ):
        raise PowdrrExecutionError("Implementation integration tests did not pass.")
    if implementation_result.get("passed") is not True:
        raise PowdrrExecutionError("Implementation did not pass its acceptance gates.")

    title_source = _required_text(proposed.get("title"), "proposed_pr_context.title")
    title = _shorten_title(title_source, 100)
    objective = _required_text(
        proposed.get("objective"), "proposed_pr_context.objective"
    )
    scope = _required_text_list(proposed.get("scope"), "proposed_pr_context.scope")
    criteria = _required_text_list(
        proposed.get("acceptance_criteria"),
        "proposed_pr_context.acceptance_criteria",
    )
    changed_paths = _required_text_list(
        implementation_result.get("changed_paths"),
        "implementation_result.changed_paths",
    )
    validations = _render_validation_results(validation_results)
    notes_value = integration_tests.get("quality_notes", [])
    notes = _deduplicated_notes(notes_value)
    sections = {
        "objective": objective,
        "scope": "\n".join(f"- {item}" for item in scope),
        "implementation_details": "Changed files:\n"
        + "\n".join(f"- `{path}`" for path in changed_paths),
        "acceptance_criteria": "\n".join(f"- {item}" for item in criteria),
        "validation": validations,
        "advisory_test_quality_notes": (
            "\n".join(f"- {note}" for note in notes) if notes else "None."
        ),
    }
    return _publication_request(
        title=title,
        commit_message=f"Implement {title}",
        publication_branch=f"powdrr/{proposed_pr_id}-implementation",
        user_request=f"Implement the reviewed plan item: {title_source}.",
        sections=sections,
    )


def _render_plan_amendment_publication_request(
    *,
    proposed: Mapping[str, Any],
    proposed_pr_id: str,
    item_title: str,
    failure_review: Mapping[str, Any],
    repair_request: Mapping[str, Any],
    amendment_result: Mapping[str, Any],
    validation_results: object,
) -> dict[str, Any]:
    if amendment_result.get("passed") is not True:
        raise PowdrrExecutionError("Plan amendment did not pass validation.")
    evidence = _required_text_list(
        failure_review.get("evidence"), "failure_review.evidence"
    )
    changed_paths = _required_text_list(
        amendment_result.get("changed_paths"), "amendment_result.changed_paths"
    )
    discovery = _required_mapping(
        repair_request.get("discovery"), "repair_request.discovery"
    )
    summary = _required_text(
        discovery.get("summary"), "repair_request.discovery.summary"
    )
    requested_outcome = _required_text(
        discovery.get("requested_outcome"), "repair_request.discovery.requested_outcome"
    )
    operations = repair_request.get("operations")
    if not isinstance(operations, list) or not operations:
        raise PowdrrExecutionError("Plan amendment requires typed repair operations.")
    rendered_operations = [
        _render_repair_operation(operation) for operation in operations
    ]
    plan_id = _required_text(proposed.get("plan_id"), "proposed_pr_context.plan_id")
    item_id = _required_text(proposed.get("item_id"), "proposed_pr_context.item_id")
    issue_number = proposed.get("github_issue_number")
    issue_reference = (
        f" and proposed PR issue #{issue_number}"
        if isinstance(issue_number, int)
        and not isinstance(issue_number, bool)
        and issue_number > 0
        else ""
    )
    title = _shorten_title(f"Correct plan: {item_title}", 100)
    validation = _render_validation_results(validation_results)
    explanation = _required_text(
        failure_review.get("explanation"), "failure_review.explanation"
    )
    sections = {
        "objective": (
            f"{explanation}\n\nDiscovery: {summary}\n\nThis amendment is linked "
            f"to original plan `{plan_id}`{issue_reference}."
        ),
        "scope": (
            f"Plan `{plan_id}`, item `{item_id}`.\n\nChanged files:\n"
            + "\n".join(f"- `{path}`" for path in changed_paths)
        ),
        "implementation_details": "Typed plan repair operations:\n"
        + "\n".join(f"- {operation}" for operation in rendered_operations),
        "acceptance_criteria": (
            f"Requested outcome: {requested_outcome}\n\nAffected item: `{item_id}`.\n\n"
            "Evidence:\n" + "\n".join(f"- {item}" for item in evidence)
        ),
        "validation": validation,
        "advisory_test_quality_notes": "None.",
    }
    return _publication_request(
        title=title,
        commit_message=f"Correct plan: {_shorten_title(item_title, 75)}",
        publication_branch=f"powdrr/{proposed_pr_id}-plan-amendment",
        user_request=(
            f"Correct the reviewed plan defect for {plan_id}/{item_id}: {summary}"
        ),
        sections=sections,
    )


def _publication_request(
    *,
    title: str,
    commit_message: str,
    publication_branch: str,
    user_request: str,
    sections: Mapping[str, str],
) -> dict[str, Any]:
    render_standard_pr_description(sections)
    return {
        "user_request": user_request,
        "workflow_context": {
            "feature-name": title,
            "commit-message": commit_message,
            "base-branch": "main",
            "publication-branch": publication_branch,
            "pr-description-sections": dict(sections),
        },
    }


def _render_validation_results(value: object) -> str:
    if not isinstance(value, list) or not value:
        raise PowdrrExecutionError("Publication requires validation command results.")
    rendered: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise PowdrrExecutionError(
                f"Validation result {index + 1} must be an object."
            )
        result = entry.get("validation_result", entry.get("result", entry))
        if not isinstance(result, Mapping):
            raise PowdrrExecutionError(
                f"Validation result {index + 1} has no structured result."
            )
        command = result.get("command")
        if command is None:
            command = entry.get("id")
            if isinstance(command, Mapping):
                command = command.get("id")
        if isinstance(command, Sequence) and not isinstance(command, str):
            command = " ".join(str(token) for token in command)
        command = _required_text(command, f"validation_results[{index}].command")
        signals: list[bool] = []
        if "passed" in result:
            signals.append(result.get("passed") is True)
        if "status" in result:
            signals.append(
                result.get("status") in {"pass", "passed", "success", "succeeded"}
            )
        if "returncode" in result:
            returncode = result.get("returncode")
            signals.append(
                isinstance(returncode, int)
                and not isinstance(returncode, bool)
                and returncode == 0
            )
        if not signals or not all(signals):
            raise PowdrrExecutionError(f"Validation command did not pass: {command}.")
        summary = _validation_summary(result)
        rendered.append(f"- `{command}` — passed{summary}")
    return "\n".join(rendered)


def _validation_summary(result: Mapping[str, Any]) -> str:
    for key in ("summary", "result_summary"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return f" ({value.strip()})"
    output = result.get("stdout")
    if isinstance(output, str):
        match = re.search(r"\b(\d+)\s+passed\b", output)
        if match:
            return f" ({match.group(1)} tests passed)"
    return ""


def _render_repair_operation(operation: object) -> str:
    if not isinstance(operation, Mapping):
        raise PowdrrExecutionError("Each plan repair operation must be an object.")
    name = _required_text(operation.get("operation"), "repair operation")
    identifiers = [
        f"{key}={operation[key]}"
        for key in ("item_id", "requirement_id", "dependency_id", "path")
        if isinstance(operation.get(key), str) and operation[key].strip()
    ]
    return f"{name}" + (f" ({', '.join(identifiers)})" if identifiers else "")


def _deduplicated_notes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise PowdrrExecutionError("Test-quality notes must be a list of strings.")
    notes: list[str] = []
    seen: set[str] = set()
    for note in value:
        if not isinstance(note, str) or not note.strip():
            raise PowdrrExecutionError("Test-quality notes must be non-empty strings.")
        normalized = " ".join(note.split())
        identity = normalized.casefold()
        if identity not in seen:
            seen.add(identity)
            notes.append(normalized)
    return notes


def _required_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PowdrrExecutionError(f"{field} must be an object.")
    return value


def _required_text_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise PowdrrExecutionError(f"{field} must be a non-empty list of strings.")
    return [item.strip() for item in value]


def _shorten_title(value: str, maximum: int) -> str:
    title = _required_text(value, "PR title")
    if len(title) <= maximum:
        return title
    prefix = title[: maximum - 3].rstrip()
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return f"{prefix}..."


def normalize_clean_pr_publication_parameters(
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve PR metadata and either template sections or a legacy body."""
    normalized = dict(parameters)
    context = parameters.get("workflow_context")
    if not isinstance(context, Mapping):
        return normalized

    aliases = {
        "base": ("base-branch", "base"),
        "publication_branch": ("publication-branch", "publication_branch"),
        "title": ("feature-name", "pull-request-title", "title"),
        "commit_message": ("commit-message", "commit_message"),
        "publication_authorization": (
            "publication-authorization",
            "publication_authorization",
        ),
        "require_publication_authorization": (
            "require-publication-authorization",
            "require_publication_authorization",
        ),
    }
    for target, keys in aliases.items():
        current = normalized.get(target)
        if current is None or (isinstance(current, str) and not current.strip()):
            normalized[target] = next(
                (context[key] for key in keys if key in context), None
            )

    sections = context.get("pr-description-sections")
    if sections is None:
        sections = context.get("pr_description_sections")
    if sections is not None:
        normalized["description_sections"] = sections
    elif "body" not in normalized:
        normalized["body"] = next(
            (
                context[key]
                for key in (
                    "populated-pr-description",
                    "pull-request-description",
                    "description",
                )
                if key in context
            ),
            None,
        )
    return normalized


def verify_publication_scope(
    parameters: Mapping[str, Any], *, worktree_root: Path
) -> dict[str, Any]:
    """Check the complete proposed file list without relying on an LLM review."""
    try:
        paths = _relative_paths(parameters.get("paths"))
    except PowdrrExecutionError as exc:
        return {"confirmed": False, "findings": [str(exc)]}
    root = worktree_root.resolve()
    findings: list[str] = []
    for relative in paths:
        parts = Path(relative).parts
        if any(
            part in {".git", ".powdrr", "__pycache__", ".pytest_cache"}
            for part in parts
        ):
            findings.append(
                f"Generated or repository-internal path is not publishable: {relative}"
            )
            continue
        candidate = root / relative
        try:
            candidate.parent.resolve().relative_to(root)
        except ValueError:
            findings.append(f"Publication path escapes the worktree: {relative}")
            continue
        if not candidate.exists() and not candidate.is_symlink():
            findings.append(
                f"Publication path is missing from the worktree: {relative}"
            )
        elif candidate.is_dir() and not candidate.is_symlink():
            findings.append(f"Publication path is a directory, not a file: {relative}")
    return {"confirmed": not findings, "findings": findings, "paths": paths}


def publish_clean_pull_request(
    parameters: Mapping[str, Any], *, worktree_root: Path
) -> str:
    """Copy only approved files to a clean publication branch and open a PR."""
    parameters = normalize_clean_pr_publication_parameters(parameters)
    files = _relative_paths(parameters.get("files_to_publish"))
    base = _branch(parameters.get("base"), field="base")
    branch = _branch(parameters.get("publication_branch"), field="publication_branch")
    if not branch.startswith("powdrr/"):
        raise PowdrrExecutionError(
            "publication_branch must be scoped under the powdrr/ namespace."
        )
    title = _required_text(parameters.get("title"), "title")
    description_sections = parameters.get("description_sections")
    require_standard_description = parameters.get("require_standard_pr_description")
    if require_standard_description is not None and not isinstance(
        require_standard_description, bool
    ):
        raise PowdrrExecutionError(
            "require_standard_pr_description must be a boolean when provided."
        )
    if require_standard_description and description_sections is None:
        raise PowdrrExecutionError(
            "Clean PR publication requires structured description sections."
        )
    if description_sections is not None:
        if not isinstance(description_sections, Mapping):
            raise PowdrrExecutionError(
                "description_sections must be an object when provided."
            )
        body = render_standard_pr_description(description_sections)
    else:
        body = _required_text(parameters.get("body"), "body")
    if require_standard_description:
        validate_standard_pr_description(body)
    commit_message = _required_text(parameters.get("commit_message"), "commit_message")
    source_root = worktree_root.resolve()
    source_branch = _run(["git", "branch", "--show-current"], cwd=source_root).strip()
    if not source_branch:
        raise PowdrrExecutionError("Cannot publish from a detached source worktree.")
    authorization_value = parameters.get("publication_authorization")
    require_authorization = parameters.get("require_publication_authorization") is True
    if require_authorization or authorization_value is not None:
        authorization = _validate_publication_authorization(
            authorization_value,
            base=base,
            files=files,
            worktree_root=source_root,
        )
        intent_path = _persist_publication_intent(
            source_root,
            branch=branch,
            authorization=authorization,
            title=title,
            body=body,
        )
    else:
        authorization = None
        intent_path = None

    with tempfile.TemporaryDirectory(prefix="powdrr-publication-") as temp_root:
        publication_root = Path(temp_root) / "worktree"
        base_ref = _resolve_base_ref(source_root, base)
        _prepare_publication_worktree(
            source_root, publication_root, base_ref=base_ref, branch=branch
        )
        try:
            publication_start = _run(
                ["git", "rev-parse", "HEAD"], cwd=publication_root
            ).strip()
            _copy_publication_files(source_root, publication_root, files)
            if authorization is not None:
                _validate_publication_authorization(
                    authorization,
                    base=base,
                    files=files,
                    worktree_root=publication_root,
                )
            _run(["git", "add", "--", *files], cwd=publication_root)
            staged = _run(
                ["git", "diff", "--cached", "--name-only"], cwd=publication_root
            ).splitlines()
            if not set(staged).issubset(set(files)):
                raise PowdrrExecutionError(
                    "Clean publication branch staged a different file set: "
                    f"expected {sorted(files)!r}, found {sorted(staged)!r}."
                )
            if staged:
                _run(["git", "commit", "-m", commit_message], cwd=publication_root)
            committed = set(
                _run(
                    ["git", "diff", "--name-only", f"{publication_start}..HEAD"],
                    cwd=publication_root,
                ).splitlines()
            )
            if staged and committed != set(files):
                raise PowdrrExecutionError(
                    "Clean publication commit contains paths outside the approved "
                    f"file set: {sorted(committed ^ set(files))!r}."
                )
            if not staged and committed:
                raise PowdrrExecutionError(
                    "Clean publication found unexpected commits on an unchanged "
                    f"publication request: {sorted(committed)!r}."
                )
            _run(
                ["git", "push", "--set-upstream", "origin", branch],
                cwd=publication_root,
            )
            url = _create_or_update_pull_request(
                publication_root,
                branch=branch,
                base=base,
                title=title,
                body=body,
            )
            _ensure_pull_request_changelog(
                publication_root,
                pull_request_url=url,
                base=base,
                title=title,
                description_sections=(
                    description_sections
                    if isinstance(description_sections, Mapping)
                    else None
                ),
                branch=branch,
            )
            if intent_path is not None:
                _complete_publication_intent(
                    intent_path,
                    pull_request_url=url,
                    head=_run(
                        ["git", "rev-parse", "HEAD"], cwd=publication_root
                    ).strip(),
                )
            return url
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(publication_root)],
                cwd=source_root,
                capture_output=True,
                text=True,
                check=False,
            )


def _ensure_pull_request_changelog(
    publication_root: Path,
    *,
    pull_request_url: str,
    base: str,
    title: str,
    description_sections: Mapping[str, Any] | None,
    branch: str,
) -> None:
    match = re.search(r"/pull/(\d+)(?:$|[?#])", pull_request_url)
    if match is None:
        raise PowdrrExecutionError(
            "Cannot generate a changelog for invalid pull request URL: "
            f"{pull_request_url!r}."
        )
    pr_number = match.group(1)
    changelog_relative_path = f"docs/changelogs/PR-{pr_number}-changelog.yaml"
    changelog_path = publication_root / changelog_relative_path
    base_ref = _resolve_base_ref(publication_root, base)
    changed_paths = [
        path
        for path in _run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=publication_root,
        ).splitlines()
        if path and not _is_pr_changelog_path(path)
    ]

    previous: Mapping[str, Any] = {}
    if changelog_path.is_file():
        try:
            loaded = yaml.safe_load(changelog_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise PowdrrExecutionError(
                f"Existing PR changelog is unreadable: {changelog_path}: {error}"
            ) from error
        if not isinstance(loaded, Mapping):
            raise PowdrrExecutionError(
                f"Existing PR changelog must contain a mapping: {changelog_path}"
            )
        previous = loaded

    previous_file_records = (
        {
            (
                str(record.get("path")),
                str(record.get("type")),
                _span_pair(record.get("span")),
            ): record
            for record in previous.get("files", [])
            if isinstance(record, Mapping)
        }
        if isinstance(previous.get("files", []), list)
        else {}
    )
    files: list[dict[str, Any]] = []
    structured_files: list[str] = []
    objective_value = (
        description_sections.get("objective")
        if description_sections is not None
        else None
    )
    objective = (
        objective_value.strip()
        if isinstance(objective_value, str) and objective_value.strip()
        else title
    )
    for path in sorted(changed_paths):
        if is_structured_document_path(path):
            structured_files.append(path)
            continue
        existed = (
            subprocess.run(
                ["git", "cat-file", "-e", f"{base_ref}:{path}"],
                cwd=publication_root,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        diff = subprocess.run(
            [
                "git",
                "diff",
                "--unified=0",
                "--no-color",
                f"{base_ref}...HEAD",
                "--",
                path,
            ],
            cwd=publication_root,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if diff.returncode != 0:
            detail = diff.stderr.strip() or diff.stdout.strip()
            raise PowdrrExecutionError(
                f"Could not inspect PR diff for changelog path {path!r}: {detail}"
            )
        spans = [
            (int(hunk.group(1)), int(hunk.group(2) or "1"))
            for line in diff.stdout.splitlines()
            if (hunk := re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line))
        ]
        # Binary files have no text hunk and are intentionally omitted from
        # changelog file spans by the shared validator as well.
        for start_line, line_count in spans:
            if line_count == 0:
                continue
            file_type = "modified" if existed else "added"
            span = {
                "start_line": start_line,
                "end_line": start_line + line_count - 1,
            }
            previous_record = previous_file_records.get(
                (path, file_type, (start_line, start_line + line_count - 1))
            )
            files.append(
                {
                    "path": path,
                    "type": file_type,
                    "span": span,
                    "summary": (
                        previous_record.get("summary")
                        if isinstance(previous_record, Mapping)
                        and isinstance(previous_record.get("summary"), str)
                        else f"Changed as part of {title}."
                    ),
                    "rationale": (
                        previous_record.get("rationale")
                        if isinstance(previous_record, Mapping)
                        and isinstance(previous_record.get("rationale"), str)
                        else objective
                    ),
                }
            )

    prior_intent = previous.get("intent")
    intent = (
        dict(prior_intent)
        if isinstance(prior_intent, Mapping)
        else {
            "problem": f"The change described by {title!r} is not yet available.",
            "goal": objective,
        }
    )
    descriptor: dict[str, Any] = {
        "schema": "https://powdrr.io/schema/changelog-v2",
        "change_id": f"PR-{pr_number}",
        "title": title,
        "intent": intent,
        "human-decisions": previous.get("human-decisions", []),
        "structured_files": sorted(structured_files),
        "files": files,
    }
    for section in (
        "entities",
        "entity_relationships",
        "invariants",
        "guidance",
        "features",
        "acceptance_criteria",
        "proposed_prs",
        "verification",
    ):
        descriptor[section] = previous.get(
            section, [] if section != "verification" else {}
        )

    text = yaml.safe_dump(
        descriptor,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    try:
        parse_change_log(text)
    except (TypeError, ValueError) as error:
        raise PowdrrExecutionError(
            f"Generated PR changelog is invalid: {error}"
        ) from error
    from powdrr_lift.change_log_validation import build_validation_report

    validation = build_validation_report(
        text,
        branch_name="HEAD",
        repo_root=publication_root,
        default_branch=base_ref,
    )
    if not validation.validation_successful:
        findings = "; ".join(issue.message for issue in validation.issues)
        raise PowdrrExecutionError(
            f"Generated PR changelog failed diff validation: {findings}"
        )

    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(text, encoding="utf-8")
    _run(["git", "add", "--", changelog_relative_path], cwd=publication_root)
    staged = _run(
        ["git", "diff", "--cached", "--name-only"], cwd=publication_root
    ).splitlines()
    if staged and staged != [changelog_relative_path]:
        raise PowdrrExecutionError(
            f"PR changelog generation staged unexpected paths: {sorted(staged)!r}."
        )
    if staged:
        _run(
            ["git", "commit", "-m", f"Add PR-{pr_number} changelog descriptor"],
            cwd=publication_root,
        )
        _run(["git", "push", "origin", branch], cwd=publication_root)


def _span_pair(value: object) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    start_line = value.get("start_line")
    end_line = value.get("end_line")
    if isinstance(start_line, int) and isinstance(end_line, int):
        return start_line, end_line
    return None


def _is_pr_changelog_path(path: str) -> bool:
    return re.fullmatch(r"docs/changelogs/PR-\d+-changelog\.yaml", path) is not None


def _persist_publication_intent(
    source_root: Path,
    *,
    branch: str,
    authorization: Mapping[str, Any],
    title: str,
    body: str,
) -> Path:
    intent_id = hashlib.sha256(
        f"{branch}\0{authorization['authorization_digest']}".encode()
    ).hexdigest()
    git_path = Path(
        _run(
            ["git", "rev-parse", "--git-path", "powdrr/publication-intents"],
            cwd=source_root,
        ).strip()
    )
    intent_root = git_path if git_path.is_absolute() else source_root / git_path
    path = intent_root / f"{intent_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    intent = {
        "schema_version": "powdrr-publication-intent-v1",
        "intent_id": intent_id,
        "branch": branch,
        "authorization": dict(authorization),
        "title": title,
        "body_digest": hashlib.sha256(body.encode()).hexdigest(),
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PowdrrExecutionError(
                "Persisted publication intent is unreadable."
            ) from exc
        if not isinstance(existing, Mapping) or any(
            existing.get(key) != value
            for key, value in intent.items()
            if key not in {"title", "body_digest"}
        ):
            raise PowdrrExecutionError(
                "Publication intent identity conflicts with stored state."
            )
        existing_intent = dict(existing)
        if (
            existing_intent.get("title") != title
            or existing_intent.get("body_digest") != intent["body_digest"]
        ):
            existing_intent.update(
                {"title": title, "body_digest": intent["body_digest"]}
            )
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(existing_intent, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        return path
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(intent, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _complete_publication_intent(
    path: Path, *, pull_request_url: str, head: str
) -> None:
    try:
        intent = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PowdrrExecutionError(
            "Persisted publication intent is unreadable."
        ) from exc
    if not isinstance(intent, dict):
        raise PowdrrExecutionError("Persisted publication intent is malformed.")
    prior_url = intent.get("pull_request_url")
    prior_head = intent.get("head")
    if prior_url is not None and (prior_url != pull_request_url or prior_head != head):
        raise PowdrrExecutionError(
            "Publication intent was completed with a different PR head."
        )
    intent["pull_request_url"] = pull_request_url
    intent["head"] = head
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(intent, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _prepare_publication_worktree(
    source_root: Path,
    publication_root: Path,
    *,
    base_ref: str,
    branch: str,
) -> None:
    existing = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        _run(
            ["git", "worktree", "add", str(publication_root), branch],
            cwd=source_root,
        )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_ref, "HEAD"],
            cwd=publication_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if ancestor.returncode != 0:
            raise PowdrrExecutionError(
                f"Existing publication branch {branch!r} is not based on {base_ref!r}."
            )
        return
    _run(
        ["git", "worktree", "add", "--detach", str(publication_root), base_ref],
        cwd=source_root,
    )
    _run(["git", "switch", "-c", branch], cwd=publication_root)


def _copy_publication_files(
    source_root: Path, publication_root: Path, files: Sequence[str]
) -> None:
    for relative in files:
        source = source_root / relative
        try:
            source.parent.resolve().relative_to(source_root)
        except ValueError as exc:
            raise PowdrrExecutionError(
                f"Publication path escapes the source worktree: {relative!r}."
            ) from exc
        if not source.exists() and not source.is_symlink():
            raise PowdrrExecutionError(
                f"Publication file does not exist in the source worktree: {relative!r}."
            )
        destination = publication_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        elif destination.exists() or destination.is_symlink():
            destination.unlink()
        if source.is_symlink():
            destination.symlink_to(source.readlink())
        elif source.is_file():
            shutil.copy2(source, destination)
        else:
            raise PowdrrExecutionError(
                f"Publication path is not a regular file: {relative!r}."
            )


def _create_or_update_pull_request(
    publication_root: Path,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> str:
    existing = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url,state"],
        cwd=publication_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        try:
            payload = json.loads(existing.stdout)
        except json.JSONDecodeError as exc:
            raise PowdrrExecutionError(
                "GitHub returned invalid JSON while looking up the publication PR."
            ) from exc
        if isinstance(payload, Mapping) and payload.get("state") in {"OPEN", "DRAFT"}:
            _run(
                ["gh", "pr", "edit", branch, "--title", title, "--body", body],
                cwd=publication_root,
            )
            url = payload.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
            raise PowdrrExecutionError("Existing publication PR has no valid URL.")
    output = _run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=publication_root,
    ).strip()
    if not output.startswith("https://") or "/pull/" not in output:
        raise PowdrrExecutionError(
            "GitHub did not return a pull-request URL for the clean publication branch."
        )
    return output


def _resolve_base_ref(source_root: Path, base: str) -> str:
    _run(["git", "fetch", "--no-tags", "origin", base], cwd=source_root)
    remote_ref = f"refs/remotes/origin/{base}"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{remote_ref}^{{commit}}"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return remote_ref
    local_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if local_ref.returncode == 0:
        return f"refs/heads/{base}"
    raise PowdrrExecutionError(
        f"Cannot resolve publication base branch {base!r} from origin or local refs."
    )


def _run(command: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PowdrrExecutionError(
            f"Command failed while publishing a clean PR ({command[0]}): {detail}"
        )
    return result.stdout


def _relative_paths(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PowdrrExecutionError("files_to_publish must be an array of paths.")
    if not value:
        raise PowdrrExecutionError("files_to_publish must not be empty.")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PowdrrExecutionError("files_to_publish contains an invalid path.")
        path = Path(item.strip())
        if path.is_absolute() or ".." in path.parts:
            raise PowdrrExecutionError(
                "files_to_publish paths must stay inside the repository."
            )
        paths.append(path.as_posix())
    if len(paths) != len(set(paths)):
        raise PowdrrExecutionError("files_to_publish must not contain duplicates.")
    return paths


def _branch(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"{field} must be a non-empty branch name.")
    branch = value.strip()
    if any(character in branch for character in "..~^:?*[\\ "):
        raise PowdrrExecutionError(f"{field} is not a safe branch name.")
    return branch


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PowdrrExecutionError(f"{field} must be a non-empty string.")
    return value
