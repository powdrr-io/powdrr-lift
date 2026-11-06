"""Service-side repair contracts and deterministic patch acceptance."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlparse

from powdrr_contracts.ci import LocalReproduction

from powdrr_service.ci_response.evidence import CIFailureUnit

RepairDisposition = Literal["repository_change", "human_prerequisite", "unresolved"]


@dataclass(frozen=True, slots=True)
class RepairContract:
    """One-head, one-failure repair authorization compiled by the service."""

    repository: str
    pull_request: int
    head_sha: str
    failure_id: str
    failure_signature: str
    reproduction: LocalReproduction
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    required_behavior: str
    acceptance_command: tuple[str, ...]
    disposition: RepairDisposition = "repository_change"
    research_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.repository.strip() or self.pull_request < 1:
            raise ValueError("repair contract requires a repository and positive PR")
        if len(self.head_sha) not in (40, 64) or not re.fullmatch(
            r"[0-9a-f]+", self.head_sha
        ):
            raise ValueError("repair contract requires a full lowercase head SHA")
        if not self.failure_id.strip() or not self.failure_signature.strip():
            raise ValueError("repair contract requires one identified CI failure")
        if not self.allowed_paths:
            raise ValueError("repair contract must declare allowed paths")
        for path in (*self.allowed_paths, *self.protected_paths):
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
                raise ValueError(f"repair path is not repository-relative: {path}")
        if set(self.allowed_paths) & set(self.protected_paths):
            raise ValueError("a path cannot be both allowed and protected")
        if not self.required_behavior.strip() or not self.acceptance_command:
            raise ValueError("repair contract requires behavior and a check command")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.to_data(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": "ci-repair-contract-v1",
            "repository": self.repository,
            "pull_request": self.pull_request,
            "head_sha": self.head_sha,
            "failure_id": self.failure_id,
            "failure_signature": self.failure_signature,
            "reproduction": self.reproduction.to_dict(),
            "allowed_paths": list(self.allowed_paths),
            "protected_paths": list(self.protected_paths),
            "required_behavior": self.required_behavior,
            "acceptance_command": list(self.acceptance_command),
            "disposition": self.disposition,
            "research_refs": list(self.research_refs),
        }


def compile_miniswe_repair_prompt(
    contract: RepairContract,
    failure: CIFailureUnit,
    *,
    research_evidence: tuple[str, ...] = (),
) -> str:
    """Render a compact instruction for a single evidence-backed failure."""
    if (
        failure.failure_id != contract.failure_id
        or failure.head_sha != contract.head_sha
    ):
        raise ValueError("failure does not match the repair contract head and id")
    if contract.disposition != "repository_change":
        raise ValueError("only repository_change contracts may be sent to MiniSWE")
    repro_guidance = {
        "reproduced": (
            "The same failure reproduced locally. Fix that observed behavior."
        ),
        "not_reproduced": (
            "The comparable local command reached the relevant phase without this "
            "failure. Treat this correction as GitHub Actions specific; inspect only "
            "the workflow, runner, event, and setup differences supported by evidence."
        ),
        "not_executable_locally": (
            "The failure cannot execute locally (for example, runner provisioning). "
            "Treat it as GitHub Actions specific and do not invent a replacement "
            "runner."
        ),
        "not_comparable": (
            "Local evidence is not comparable. Do not claim the failure is local or "
            "GitHub Actions specific; use only the supplied CI evidence."
        ),
        "execution_error": (
            "Local reproduction errored before a conclusion. Do not call that a "
            "non-reproduction; use only the supplied CI evidence."
        ),
    }[contract.reproduction.status]
    evidence = "\n".join(f"- {item[:1200]}" for item in research_evidence[:3])
    if not evidence:
        evidence = "- No applicable external source was established. Do not guess."
    return "\n".join(
        (
            "Resolve exactly one GitHub Actions failure at the pinned PR head.",
            f"Repository: {contract.repository}; PR: #{contract.pull_request}",
            f"Required head: {contract.head_sha}",
            f"Failure id: {failure.failure_id}",
            f"Failure phase: {failure.phase}",
            f"Observed failure: {contract.failure_signature[:1200]}",
            f"Local reproduction: {contract.reproduction.status} — "
            f"{contract.reproduction.reason[:500]}",
            repro_guidance,
            "",
            "Applicable research evidence (facts only; cite its source in your "
            "reasoning):",
            evidence,
            "",
            f"Required behavior: {contract.required_behavior}",
            "Allowed paths: " + ", ".join(contract.allowed_paths),
            "Protected paths: " + ", ".join(contract.protected_paths),
            "Acceptance command: " + " ".join(contract.acceptance_command),
            "",
            "Do not delete, empty, weaken, skip, or xfail tests; replace assertions "
            "with "
            "assert True; catch errors and pass; remove jobs or matrix entries; add "
            "continue-on-error; mask command exit codes; or change unrelated behavior.",
            "If evidence points to a human or repository-admin prerequisite, do not "
            "fake a code fix. Report the exact prerequisite and stop.",
        )
    )


def compile_ci_repair_request(
    *,
    repository: str,
    pull_request: int,
    failure: CIFailureUnit,
    reproduction: LocalReproduction,
    changed_paths: tuple[str, ...],
    proposed_paths: tuple[str, ...],
    required_behavior: str,
    acceptance_command: tuple[str, ...],
    research_evidence: tuple[dict[str, str], ...] = (),
) -> dict[str, object]:
    """Compile a model-proposed target into a bounded worker request.

    The model may choose paths and describe the desired correction, but this
    compiler requires paths to belong to the observed PR diff and rejects test
    files. The worker receives only the compiled exact-head contract.
    """
    changed = set(changed_paths)
    paths = tuple(dict.fromkeys(proposed_paths))
    if not paths or len(paths) > 3:
        raise ValueError("CI repair must target one to three changed paths")
    if any(path not in changed for path in paths):
        raise ValueError("CI repair paths must belong to the observed PR diff")
    if any(
        part.casefold() in {"test", "tests", "testing"}
        for path in paths
        for part in PurePosixPath(path).parts[:-1]
    ):
        raise ValueError("CI repair cannot target test files")
    if any(
        PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or not PurePosixPath(path).parts
        for path in (*changed_paths, *paths)
    ):
        raise ValueError("CI repair paths must be safe repository-relative paths")
    if any(_is_test_path(path) for path in paths):
        raise ValueError("CI repair cannot target test files")
    if len(research_evidence) > 3:
        raise ValueError("CI repair accepts at most three research excerpts")
    for item in research_evidence:
        source_ref, url, excerpt = (
            item.get("source_ref", ""),
            item.get("url", ""),
            item.get("excerpt", ""),
        )
        parsed = urlparse(url)
        if (
            not source_ref.strip()
            or parsed.scheme != "https"
            or parsed.hostname != "docs.github.com"
            or not excerpt.strip()
            or len(excerpt) > 1200
        ):
            raise ValueError(
                "CI repair research evidence must be a captured official GitHub excerpt"
            )
        sha256 = item.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("CI repair evidence has an invalid content hash")
    refs = tuple(dict.fromkeys(item["source_ref"] for item in research_evidence))
    evidence_lines = tuple(
        f"{item['source_ref']} ({item['url']}; "
        f"sha256 {item['sha256']}): "
        f"{item['excerpt']}"
        for item in research_evidence
    )
    protected = tuple(path for path in changed_paths if _is_test_path(path))
    contract = RepairContract(
        repository=repository,
        pull_request=pull_request,
        head_sha=failure.head_sha,
        failure_id=failure.failure_id,
        failure_signature=failure.signature,
        reproduction=reproduction,
        allowed_paths=paths,
        protected_paths=protected,
        required_behavior=required_behavior,
        acceptance_command=acceptance_command,
        research_refs=refs,
    )
    prompt = compile_miniswe_repair_prompt(
        contract, failure, research_evidence=evidence_lines
    )
    return {
        "schema_version": "ci-repair-request-v1",
        "head_sha": failure.head_sha,
        "failure_id": failure.failure_id,
        "contract_fingerprint": contract.fingerprint,
        "contract": contract.to_data(),
        "prompt": prompt,
        "allowed_paths": list(contract.allowed_paths),
        "protected_paths": list(contract.protected_paths),
        "acceptance_command": list(contract.acceptance_command),
    }


def _is_test_path(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return (
        name.startswith(("test_", "tests_"))
        or name in {"test", "tests"}
        or name.startswith(("test.", "tests."))
        or name.endswith(("_test.py", "_tests.py", ".test.js", ".spec.js"))
        or ".test." in name
        or ".spec." in name
    )


_WEAKENING_PATTERNS = (
    re.compile(r"^\s*continue-on-error\s*:\s*true\s*$", re.MULTILINE | re.I),
    re.compile(r"\bpytest\.mark\.(?:skip|xfail)\b", re.I),
    re.compile(r"\bassert\s+True\b"),
    re.compile(r"\|\|\s*true\b"),
    re.compile(r"\bexit\s+0\b"),
)


def validate_repair_patch(
    *,
    contract: RepairContract,
    changed_files: dict[str, str],
) -> tuple[str, ...]:
    """Reject path escapes and obvious verification weakening deterministically."""
    findings: list[str] = []
    allowed = set(contract.allowed_paths)
    protected = set(contract.protected_paths)
    for path, content in sorted(changed_files.items()):
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
            findings.append(f"unsafe changed path: {path}")
            continue
        if path in protected:
            findings.append(f"protected path changed: {path}")
        elif path not in allowed:
            findings.append(f"path is outside the repair scope: {path}")
        if any(part in {"tests", "test"} for part in parsed.parts[:-1]):
            findings.append(f"test path is not an authorized repair target: {path}")
        if any(pattern.search(content) for pattern in _WEAKENING_PATTERNS):
            findings.append(f"verification weakening detected: {path}")
    return tuple(findings)


def repair_contract_from_data(value: Mapping[str, object]) -> RepairContract:
    """Reconstruct a service contract from its serialized worker request."""
    reproduction_data = value.get("reproduction")
    if not isinstance(reproduction_data, Mapping):
        raise ValueError("repair contract is missing its reproduction receipt")
    sequence_fields = (
        "allowed_paths",
        "protected_paths",
        "acceptance_command",
        "research_refs",
    )
    sequences: dict[str, tuple[str, ...]] = {}
    for name in sequence_fields:
        raw = value.get(name, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"repair contract field {name} must be a string list")
        sequences[name] = tuple(raw)
    disposition = value.get("disposition", "repository_change")
    if disposition not in {"repository_change", "human_prerequisite", "unresolved"}:
        raise ValueError("repair contract has an unsupported disposition")
    repository = value.get("repository")
    pull_request = value.get("pull_request")
    if not isinstance(repository, str) or not isinstance(pull_request, int):
        raise ValueError("repair contract repository identity is malformed")
    scalar_fields = ("head_sha", "failure_id", "failure_signature", "required_behavior")
    if not all(isinstance(value.get(name), str) for name in scalar_fields):
        raise ValueError("repair contract contains malformed text fields")
    return RepairContract(
        repository=repository,
        pull_request=pull_request,
        head_sha=str(value["head_sha"]),
        failure_id=str(value["failure_id"]),
        failure_signature=str(value["failure_signature"]),
        reproduction=LocalReproduction.from_dict(reproduction_data),
        allowed_paths=sequences["allowed_paths"],
        protected_paths=sequences["protected_paths"],
        required_behavior=str(value["required_behavior"]),
        acceptance_command=sequences["acceptance_command"],
        disposition=disposition,
        research_refs=sequences["research_refs"],
    )


def validate_ci_repair_attempt(
    *,
    repair_request: Mapping[str, object],
    attempt: Mapping[str, object],
    changed_files: Mapping[str, str],
) -> dict[str, object]:
    """Check the worker receipt and changed source against its service contract."""
    raw_contract = repair_request.get("contract")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("repair request omitted its serialized contract")
    contract = repair_contract_from_data(raw_contract)
    findings: list[str] = []
    if repair_request.get("contract_fingerprint") != contract.fingerprint:
        findings.append("repair contract fingerprint does not match its contents")
    if repair_request.get("head_sha") != contract.head_sha:
        findings.append("repair request head does not match its contract")
    if repair_request.get("failure_id") != contract.failure_id:
        findings.append("repair request failure ID does not match its contract")
    if attempt.get("head_sha") != contract.head_sha:
        findings.append("worker attempt ran at a different PR head")
    if attempt.get("failure_id") != contract.failure_id:
        findings.append("worker attempt addressed a different CI failure")
    if attempt.get("passed") is not True:
        findings.append("MiniSWE did not complete the scoped repair attempt")
    attempt_paths = attempt.get("changed_paths")
    if not isinstance(attempt_paths, list) or not all(
        isinstance(path, str) for path in attempt_paths
    ):
        findings.append("worker attempt omitted a valid changed-path list")
    elif set(attempt_paths) != set(changed_files):
        findings.append(
            "worker changed paths do not match the files read for validation"
        )
    findings.extend(
        validate_repair_patch(contract=contract, changed_files=dict(changed_files))
    )
    return {
        "passed": not findings,
        "findings": list(dict.fromkeys(findings)),
        "head_sha": contract.head_sha,
        "failure_id": contract.failure_id,
        "changed_paths": sorted(changed_files),
        "contract_fingerprint": contract.fingerprint,
    }


__all__ = [
    "RepairContract",
    "RepairDisposition",
    "compile_miniswe_repair_prompt",
    "repair_contract_from_data",
    "validate_ci_repair_attempt",
    "validate_repair_patch",
]
