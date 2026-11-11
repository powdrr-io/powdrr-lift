"""MiniSWE environment with service-verified source context over local IPC."""

from __future__ import annotations

import os
from typing import Any

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig
from minisweagent.exceptions import Submitted
from pydantic import field_validator

from powdrr_agent.execution.source_reader import context_request


class StructrrLocalEnvironmentConfig(LocalEnvironmentConfig):
    source_reference_mode: str = "off"
    source_context_endpoint: str = ""

    @field_validator("source_reference_mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if value not in {"off", "observe", "enforce"}:
            raise ValueError("invalid source reference mode")
        return value


class StructrrLocalEnvironment(LocalEnvironment):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(config_class=StructrrLocalEnvironmentConfig, **kwargs)
        self.source_mode = self.config.source_reference_mode
        self.endpoint = self.config.source_context_endpoint
        if self.source_mode != "off":
            if not self.endpoint:
                raise ValueError("source context endpoint is required")
            identity = context_request(self.endpoint, {"operation": "hello"})
            if identity.get("mode") != self.source_mode:
                raise ValueError("source reference mode disagrees with trusted session")
            # Children need only the limited reader service, never a catalog path.
            self.config.env = {
                **self.config.env,
                "POWDRR_STRUCTRR_ENDPOINT": self.endpoint,
            }

    def execute(
        self, action: dict[str, Any], cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]:
        # Base execution owns timeout, errors, and Submitted. Submission raises
        # before enrichment, leaving the final artifact completely untouched.
        try:
            result = super().execute(action, cwd, timeout=timeout)
        except Submitted:
            if self.source_mode != "off":
                context_request(
                    self.endpoint,
                    {
                        "operation": "submission",
                        "command": action.get("command", ""),
                        "output": self._last_output["output"],
                    },
                )
            raise
        if self.source_mode == "off":
            return result
        response = context_request(
            self.endpoint,
            {
                "operation": "observe",
                "command": action.get("command", ""),
                "output": result["output"],
            },
        )
        context = response["context"]
        result["structrr_original_output"] = result["output"]
        result["structrr_context"] = context
        result["output"] += context
        result["extra"] = {
            **result.get("extra", {}),
            "structrr_observation": response["event"],
            "structrr_context": context,
        }
        return result

    def _check_finished(self, output: dict[str, Any]) -> None:
        self._last_output = output
        super()._check_finished(output)


class StructrrObservationModel:
    """Append context after the real model formatter truncates source output."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)

    def format_observation_messages(
        self,
        message: dict[str, Any],
        outputs: list[dict[str, Any]],
        template_vars: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        source_outputs = [
            {
                **output,
                "output": output.get("structrr_original_output", output["output"]),
            }
            for output in outputs
        ]
        messages = self.model.format_observation_messages(
            message, source_outputs, template_vars
        )
        if len(messages) < len(outputs):
            raise RuntimeError("Structrr requires one formatted observation per action")
        # MiniSWE pads unexecuted actions when submission interrupts a batch.
        for rendered, output in zip(messages[: len(outputs)], outputs, strict=True):
            if not isinstance(rendered.get("content"), str):
                raise RuntimeError("Structrr requires text observation content")
            rendered["content"] += output.get("structrr_context", "")
        return messages


def configure_source_environment(config: dict[str, Any]) -> bool:
    """Apply inherited service configuration to normal, task-file, and resume runs."""
    inherited_mode = os.environ.get("POWDRR_STRUCTRR_MODE", "off")
    mode = inherited_mode
    environment = config.setdefault("environment", {})
    mode = environment.get("source_reference_mode", mode)
    if inherited_mode == "enforce" and mode != "enforce":
        raise ValueError("cannot downgrade an enforced source reference session")
    if mode == "off":
        return False
    if mode not in {"observe", "enforce"}:
        raise ValueError("invalid source reference mode")
    environment.update(
        {
            "environment_class": (
                "powdrr_agent.execution.minisweagent_environment."
                "StructrrLocalEnvironment"
            ),
            "source_reference_mode": mode,
            "source_context_endpoint": environment.get("source_context_endpoint")
            or os.environ.get("POWDRR_STRUCTRR_ENDPOINT", ""),
        }
    )
    return True
