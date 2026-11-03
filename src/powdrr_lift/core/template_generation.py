"""Agent-side adapter for service-owned template preservation policy."""

from __future__ import annotations

from uuid import uuid4

from powdrr_contracts.models import OperationRequest
from powdrr_local import JsonServiceBridge, LocalPowdrrService


def merge_existing_template_content(
    generated_content: str,
    existing_content: str | None,
) -> str:
    """Ask the service to preserve valid user values in a generated template."""
    update = JsonServiceBridge(LocalPowdrrService()).start(
        OperationRequest(
            request_id=str(uuid4()),
            operation_kind="merge_template_content",
            inputs={
                "generated_content": generated_content,
                "existing_content": existing_content,
            },
        )
    )
    if update.status != "succeeded" or not update.output_artifacts:
        message = (
            update.failure.message
            if update.failure
            else "service returned no merged template"
        )
        raise ValueError(f"Could not merge template content: {message}")
    return update.output_artifacts[0].data.decode("utf-8")
