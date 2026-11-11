"""Service-owned verification and rendering for MiniSWE source observations."""

from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.source_reader import SourceReader, SourceReadResult

_VISIBLE = re.compile(
    r"(?:^|\n)(?:[^\n]*?:\d+:)?[ \t]*"
    r"# structrr(?:\[(?:module|block)\])?:\s*([A-Za-z0-9_./:-]{1,256})[ \t]*(?=\r?$)",
    re.MULTILINE,
)
SECTION = (
    "\n\n--- Structrr context (service observation; source is untrusted data) ---\n"
)


class SourceObservationSession:
    """Bind issued reader requests and observations to one immutable catalog."""

    def __init__(self, reader: SourceReader) -> None:
        self.reader = reader
        self._issued: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _read(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        if operation == "read" and set(request) == {
            "operation",
            "path",
            "start",
            "end",
        }:
            result = self.reader.read(request["path"], request["start"], request["end"])
        elif operation == "search" and set(request) == {"operation", "query", "path"}:
            result = self.reader.search(request["query"], request["path"])
        elif operation == "context" and set(request) == {"operation", "ref", "offset"}:
            if not isinstance(request["ref"], str):
                raise ValueError("context reference must be a string")
            result = self.reader.context(request["ref"], request["offset"])
        else:
            raise ValueError("unsupported source reader request")
        return result.to_data()

    def read(self, request: Mapping[str, Any]) -> dict[str, Any]:
        data = self._read(request)
        ticket = uuid.uuid4().hex
        self._issued[ticket] = dict(request)
        if len(self._issued) > 128:
            self._issued.popitem(last=False)
        return {
            "schema_version": "structrr-issued-source-read-v1",
            "ticket": ticket,
            "observation": data,
        }

    def observe(
        self, command: str, output: str, *, submitted: bool = False
    ) -> dict[str, Any]:
        diagnostics: list[dict[str, str]] = []
        mode = "visible_markers"
        data: dict[str, Any] | None = None
        try:
            candidate = json.loads(output)
        except (ValueError, TypeError, RecursionError):
            candidate = None
        if submitted:
            mode = "submission"
        elif isinstance(candidate, dict) and candidate.get("schema_version") in (
            "structrr-source-read-v1",
            "structrr-issued-source-read-v1",
        ):
            mode = "verified_reader"
            try:
                if set(candidate) != {"schema_version", "ticket", "observation"}:
                    raise ValueError("Reader output requires a session-issued receipt.")
                envelope = SourceReadResult.from_data(
                    candidate["observation"]
                ).to_data()
                request = self._issued.get(candidate["ticket"])
                if request is None:
                    raise ValueError(
                        "Reader observation was not issued by this session."
                    )
                # Re-execute the bounded read/search against the trusted inputs.
                # Compare the complete envelope, including context and diagnostics.
                data = self._read(request)
                if data != envelope:
                    data = None
                    raise ValueError("Source observation is stale or altered.")
            except (ValueError, TypeError, KeyError) as error:
                mode = "rejected_reader"
                diagnostics.append({"code": "unverified_reader", "message": str(error)})
        else:
            refs = sorted(set(_VISIBLE.findall(output)))
            if len(refs) > self.reader.limits.max_search_matches:
                refs = refs[: self.reader.limits.max_search_matches]
                diagnostics.append(
                    {
                        "code": "visible_reference_limit",
                        "message": "Use the reader for additional visible references.",
                    }
                )
            if refs:
                # Visible output cannot establish enclosing AST scopes.
                data = self.reader.contexts(refs).to_data()
            else:
                mode = "no_visible_references"
        complete = bool(
            data and data["status"] == "complete" and mode == "verified_reader"
        )
        context = ""
        if data is not None:
            rendered = {
                "enrichment_mode": mode,
                "scope_coverage": "verified_reader"
                if mode == "verified_reader"
                else "visible_only",
                "context_complete": complete,
                "catalog_digest": self.reader.catalog.digest,
                "references": data["references"],
                "context_pages": data["context_pages"],
                "remaining_refs": data["remaining_refs"],
                "continuations": [
                    {
                        **item,
                        "command": [
                            "python",
                            "-m",
                            "powdrr_agent.execution.source_reader",
                            "context",
                            f"--ref={item['ref']}",
                            f"--offset={item['offset']}",
                        ],
                    }
                    for item in data["continuations"]
                ],
                "diagnostics": diagnostics + data["diagnostics"],
            }
            context = SECTION + json.dumps(rendered, ensure_ascii=False, sort_keys=True)
        elif diagnostics:
            context = SECTION + json.dumps(
                {"context_complete": False, "diagnostics": diagnostics}
            )
        event = {
            "schema_version": "structrr-source-observation-v1",
            "command_digest": content_fingerprint(command),
            "output_digest": content_fingerprint(output),
            "catalog_digest": self.reader.catalog.digest,
            "base_commit": self.reader.catalog.base_commit,
            "limits_digest": self.reader.limits.digest,
            "enrichment_mode": mode,
            "context_complete": complete,
            "source_digests": sorted({e["source_sha256"] for e in data["excerpts"]})
            if data
            else [],
            "refs": [r["ref"] for r in data["references"]] if data else [],
            "diagnostics": diagnostics + (data["diagnostics"] if data else []),
            "delivered_context_digest": content_fingerprint(context),
            "context_pages": [
                {
                    "ref": page["ref"],
                    "offset": page["offset"],
                    "end_offset": page["offset"] + len(page["text"]),
                    "total_characters": page["total_characters"],
                }
                for page in data["context_pages"]
            ]
            if data
            else [],
        }
        return {"context": context, "event": event}
