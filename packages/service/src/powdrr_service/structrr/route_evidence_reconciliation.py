"""Exact candidate-bound reconciliation for compiled obligation evidence routes."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

_STATUSES = {"passed", "failed", "blocked", "not_collected"}


def reconcile_obligation_route_evidence(
    contracts: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    *,
    candidate_tree: str,
) -> dict[str, Any]:
    """Require exactly one passing record for each compiled route on this candidate."""
    if not candidate_tree:
        raise ValueError("candidate tree identity is required")

    expected: set[tuple[str, str]] = set()
    issues: list[dict[str, str]] = []
    for contract in contracts:
        obligation_id = contract.get("obligation_id")
        routes = contract.get("review_routes")
        if not isinstance(obligation_id, str) or not obligation_id:
            raise ValueError("route contracts require obligation IDs")
        if not isinstance(routes, list) or not routes:
            raise ValueError("route contracts require a non-empty route list")
        for route in routes:
            if not isinstance(route, str) or not route:
                raise ValueError("route contracts require non-empty route names")
            key = (obligation_id, route)
            if key in expected:
                raise ValueError("compiled obligation route pairs must be unique")
            expected.add(key)

    by_route: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in evidence:
        obligation_id = record.get("obligation_id")
        route = record.get("route")
        if isinstance(obligation_id, str) and isinstance(route, str):
            by_route[(obligation_id, route)].append(record)
        else:
            issues.append(
                {
                    "kind": "unlinked_route",
                    "message": "route evidence has no obligation and route identity",
                }
            )

    accepted: list[dict[str, str]] = []
    for obligation_id, route in sorted(expected):
        key = (obligation_id, route)
        matches = by_route.pop(key, [])
        if not matches:
            issues.append(
                _issue(key, "missing_route", "compiled route has no evidence")
            )
            continue
        if len(matches) != 1:
            issues.append(
                _issue(key, "duplicate_route", "compiled route has multiple records")
            )
            continue
        record = matches[0]
        if record.get("candidate_tree") != candidate_tree:
            issues.append(
                _issue(key, "stale_candidate", "route evidence names another candidate")
            )
            continue
        status = record.get("status")
        if status not in _STATUSES:
            issues.append(_issue(key, "invalid_status", "route status is unsupported"))
        elif status != "passed":
            issues.append(_issue(key, status, f"route evidence status is {status!r}"))
        else:
            accepted.append({"obligation_id": obligation_id, "route": route})

    for (obligation_id, route), records in sorted(by_route.items()):
        issues.append(
            _issue(
                (obligation_id, route),
                "unlinked_route",
                f"{len(records)} evidence record(s) do not match a compiled route",
            )
        )

    return {
        "passed": not issues and len(accepted) == len(expected),
        "candidate_tree": candidate_tree,
        "expected_route_count": len(expected),
        "accepted_routes": accepted,
        "issues": issues,
    }


def _issue(key: tuple[str, str], kind: str, message: str) -> dict[str, str]:
    return {
        "kind": kind,
        "obligation_id": key[0],
        "route": key[1],
        "message": message,
    }


__all__ = ["reconcile_obligation_route_evidence"]
