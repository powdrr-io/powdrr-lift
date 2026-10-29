"""Service-owned evidence records and structural validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

_EVIDENCE_KINDS = frozenset(
    {"implementation", "test", "configuration", "execution_receipt"}
)
EvidenceKind = Literal["implementation", "test", "configuration", "execution_receipt"]


class EvidenceError(ValueError):
    """Invalid pinned evidence record."""


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    base_commit: str
    path: str
    start_line: int
    end_line: int
    content_sha256: str
    kind: EvidenceKind
    symbol: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Evidence:
        expected = {
            "id",
            "base_commit",
            "path",
            "start_line",
            "end_line",
            "content_sha256",
            "kind",
            "symbol",
        }
        if set(value) != expected:
            raise EvidenceError("evidence record fields are invalid")
        evidence_id = value["id"]
        commit = value["base_commit"]
        path = value["path"]
        start = value["start_line"]
        end = value["end_line"]
        digest = value["content_sha256"]
        kind = value["kind"]
        symbol = value["symbol"]
        if not isinstance(evidence_id, str) or not re.fullmatch(
            r"evidence-[0-9]{4,}", evidence_id
        ):
            raise EvidenceError("evidence ID is invalid")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise EvidenceError("evidence base commit is invalid")
        if not isinstance(path, str):
            raise EvidenceError("evidence path is invalid")
        _validate_repository_path(path)
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            raise EvidenceError("evidence line range is invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EvidenceError("evidence content digest is invalid")
        if not isinstance(kind, str) or kind not in _EVIDENCE_KINDS:
            raise EvidenceError("evidence kind is invalid")
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
            raise EvidenceError("evidence symbol must be a name or null")
        if kind in {"configuration", "execution_receipt"} and symbol is not None:
            raise EvidenceError("file evidence cannot carry a symbol name")
        if kind == "test" and not any(
            "test" in part.casefold() or "fixture" in part.casefold()
            for part in PurePosixPath(path).parts
        ):
            raise EvidenceError("test evidence must come from a test or fixture path")
        return cls(
            evidence_id,
            commit,
            path,
            start,
            end,
            digest,
            cast(EvidenceKind, kind),
            symbol,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "base_commit": self.base_commit,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content_sha256": self.content_sha256,
            "kind": self.kind,
            "symbol": self.symbol,
        }


def _validate_repository_path(path: str) -> None:
    normalized = PurePosixPath(path)
    if (
        not path
        or normalized.is_absolute()
        or normalized.as_posix() != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise EvidenceError("evidence path must be a normalized repository path")


__all__ = ["Evidence", "EvidenceError"]
