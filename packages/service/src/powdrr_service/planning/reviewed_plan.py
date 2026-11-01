"""Service decisions over implementation steps in a reviewed PR plan."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath


def reviewed_file_creation_conflicts(
    *,
    allowed_paths: Sequence[str],
    implementation_steps: Sequence[str],
    base_commit: str,
    existing_file_sha256: Mapping[str, str],
) -> tuple[str, ...]:
    """Identify create-file instructions that conflict with files in the base tree.

    ``existing_file_sha256`` is local Git evidence collected by the agent. This
    service function owns the interpretation of that evidence against the reviewed
    implementation steps.
    """
    findings: list[str] = []
    for raw_path in allowed_paths:
        relative_path = PurePosixPath(raw_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            continue
        matching_steps = tuple(
            step
            for step in implementation_steps
            if implementation_step_creates_path(step, raw_path)
        )
        digest = existing_file_sha256.get(relative_path.as_posix())
        if not matching_steps or digest is None:
            continue
        findings.append(
            f"reviewed plan directs creating {raw_path}, but that file already "
            f"exists in base commit {base_commit} (sha256={digest}); amend the "
            "plan to reuse or deliberately update the existing file. Evidence: "
            + "; ".join(matching_steps)
        )
    return tuple(findings)


def implementation_step_creates_path(step: str, raw_path: str) -> bool:
    """Return whether a creation instruction targets this path specifically."""
    path_pattern = re.compile(rf"(?<![\w./-]){re.escape(raw_path)}(?![\w./-])")
    creation_pattern = re.compile(
        r"\b(?:create|add|introduce|write|generate|new)\b", re.I
    )
    clause_break = re.compile(
        r"\b(?:and|then|that|which|while|when|runs?|executes?|references?|calls?)\b",
        re.I,
    )
    for path_match in path_pattern.finditer(step):
        prefix = step[: path_match.start()]
        for creation_match in reversed(tuple(creation_pattern.finditer(prefix))):
            between = prefix[creation_match.end() :]
            if len(between) > 96 or clause_break.search(between):
                continue
            return True
    return False


__all__ = ["implementation_step_creates_path", "reviewed_file_creation_conflicts"]
