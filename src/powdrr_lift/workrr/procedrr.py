"""Workrr transport adapter for procedrr judge responses."""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json

from powdrr_lift.workflow_chat_selection import WorkflowChatConfig
from powdrr_lift.workflow_chat_transport import _complete_json_with_repair
from powdrr_lift.workrr.protocol import WorkflowLLMClient


class ProcedrrResponseError(RuntimeError):
    """A procedrr judge could not produce a schema-valid response."""


class WorkrrProcedrrClient:
    """Adapt Workrr's structured repair transport to the procedrr protocol."""

    def __init__(
        self,
        client: WorkflowLLMClient,
        *,
        skills_dir: Path,
        max_retries: int = 3,
    ) -> None:
        self._client = client
        self._config = WorkflowChatConfig(
            skills_dir=skills_dir,
            provider_retry_attempts=max_retries,
            provider_retry_delay_seconds=0,
        )

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if response_schema is None:
            raise ProcedrrResponseError("Procedrr judges require an output schema.")

        def parse(payload: dict[str, Any]) -> dict[str, Any]:
            try:
                validate_json(payload, response_schema)
            except JsonSchemaError as exc:
                raise RuntimeError(f"response_schema: {exc.message}") from exc
            return payload

        result = _complete_json_with_repair(
            self._client,
            messages,
            context="procedrr judge",
            model="procedrr",
            parser=parse,
            repair_instructions=(
                "Return only one JSON object matching the declared schema. "
                "Preserve the original objective and correct the specific "
                "schema error; do not repeat the invalid object."
            ),
            config=self._config,
            input_func=lambda: "abort",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            response_schema=response_schema,
        )
        if not isinstance(result, dict):
            raise ProcedrrResponseError(
                "Workrr exhausted structured repair for a procedrr judge."
            )
        return result


__all__ = ["ProcedrrResponseError", "WorkrrProcedrrClient"]
