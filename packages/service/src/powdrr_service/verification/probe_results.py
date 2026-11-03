"""Service-side interpretation of locally executed behavior probe results."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from powdrr_contracts.verification import PostRunProbeResult


def baseline_probe_failures(results: Sequence[PostRunProbeResult]) -> list[str]:
    """Return probes that passed or failed for an unexpected baseline reason."""
    problems = []
    for result in results:
        if not baseline_probe_is_expected_failure(result):
            problems.append(
                f"{result.probe_id} ({result.scenario_id}) returned "
                f"{result.status}; expected a baseline failure through "
                "AssertionError, TypeError, or ImportError. "
                f"{_truncate(result.output or result.error or '', 1600)}"
            )
    return problems


def baseline_probe_is_expected_failure(result: PostRunProbeResult) -> bool:
    """Recognize baseline exceptions that mean the requested API is absent."""
    if result.status != "failed":
        return False
    terminal_line = next(
        (line.strip() for line in reversed(result.output.splitlines()) if line.strip()),
        "",
    )
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)(?::|$)", terminal_line)
    if match is None:
        return False
    exception_name = match.group(1).rsplit(".", 1)[-1]
    return exception_name in {
        "AssertionError",
        "ImportError",
        "ModuleNotFoundError",
        "TypeError",
    }


def probe_failures(results: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Convert failed probe results into focused coding-agent repair findings."""
    return [
        {
            "scenario_id": str(item.get("scenario_id", "unknown")),
            "reason": (
                f"probe {item.get('probe_id', 'unknown')} {item.get('status')}: "
                f"{_truncate(str(item.get('output') or item.get('error') or ''), 2400)}"
            ),
        }
        for item in results
        if item.get("status") in {"failed", "timed_out", "unavailable"}
    ]


def _truncate(value: str, limit: int) -> str:
    normalized = re.sub(r"\n{3,}", "\n\n", value).strip()
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


__all__ = [
    "baseline_probe_failures",
    "baseline_probe_is_expected_failure",
    "probe_failures",
]
