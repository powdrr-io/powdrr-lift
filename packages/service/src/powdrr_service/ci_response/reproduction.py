"""Conservative comparison of remote CI failures and local execution evidence."""

from __future__ import annotations

from powdrr_contracts.ci import LocalReproduction

from powdrr_service.ci_response.evidence import normalize_failure_signature


def compare_local_reproduction(
    *,
    ci_signature: str,
    local_output: str | None,
    command: tuple[str, ...],
    exit_code: int | None,
    phase_exercised: bool,
    environment_comparable: bool,
    locally_executable: bool = True,
) -> LocalReproduction:
    """Classify reproduction without turning setup failures into non-repros."""
    if not locally_executable:
        return LocalReproduction(
            "not_executable_locally",
            "The observed CI failure occurs before a local command can run.",
        )
    if not environment_comparable:
        return LocalReproduction(
            "not_comparable",
            "Local and CI execution environments differ in a relevant capability.",
            command=command,
            exit_code=exit_code,
        )
    if local_output is None or exit_code is None:
        return LocalReproduction(
            "execution_error" if local_output is None else "not_comparable",
            "Local reproduction did not return captured output and an exit status.",
            command=command,
        )
    local_signature = normalize_failure_signature(ci_signature)
    matching_line = any(
        normalize_failure_signature(line).removeprefix("e ") == local_signature
        for line in local_output.splitlines()
    )
    if matching_line and exit_code != 0:
        return LocalReproduction(
            "reproduced",
            "The normalized CI failure signature appears in local command output.",
            command=command,
            exit_code=exit_code,
            matching_failure=True,
        )
    if matching_line:
        return LocalReproduction(
            "not_comparable",
            "The output mentions the CI signature but the local command succeeded.",
            command=command,
            exit_code=exit_code,
        )
    if not phase_exercised:
        return LocalReproduction(
            "not_comparable",
            "The local command did not reach the CI failure phase.",
            command=command,
            exit_code=exit_code,
        )
    return LocalReproduction(
        "not_reproduced",
        "The comparable local command reached the relevant phase without "
        "the CI signature.",
        command=command,
        exit_code=exit_code,
        matching_failure=False,
    )
