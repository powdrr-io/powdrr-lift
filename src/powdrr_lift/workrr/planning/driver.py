"""Compatibility import for the service-owned planning question machine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from powdrr_service.planning.driver import (
    PlanningAssembly,
    SourceLedger,
)
from powdrr_service.planning.driver import (
    SourceLedgerQuestionMachine as _SourceLedgerQuestionMachine,
)
from powdrr_service.planning.evidence import Evidence
from powdrr_service.planning.source import SegmentedSource


class SourceLedgerQuestionMachine(_SourceLedgerQuestionMachine):
    """Adapt the legacy repository-root argument to the service-owned policy."""

    def __init__(
        self,
        source: SegmentedSource,
        *,
        repository_root: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        excerpt_reader = None
        if repository_root is not None:
            from powdrr_lift.workrr.planning.evidence import read_evidence_excerpt

            def excerpt_reader(evidence: Evidence) -> str:
                return read_evidence_excerpt(repository_root, evidence)

            kwargs.setdefault("evidence_excerpt_reader", excerpt_reader)
        super().__init__(source, **kwargs)


__all__ = ["PlanningAssembly", "SourceLedger", "SourceLedgerQuestionMachine"]
