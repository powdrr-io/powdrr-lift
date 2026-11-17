"""Publish selected implementation files from a clean branch."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
    path = source_root / ".powdrr" / "publication-intents" / f"{intent_id}.json"
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
