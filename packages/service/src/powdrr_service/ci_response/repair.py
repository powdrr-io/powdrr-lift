"""Service-side repair contracts and deterministic patch acceptance."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

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


__all__ = [
    "RepairContract",
    "RepairDisposition",
    "compile_miniswe_repair_prompt",
    "validate_repair_patch",
]
