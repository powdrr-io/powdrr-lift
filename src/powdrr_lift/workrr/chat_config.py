"""Configuration shared by the Procedrr workflow chat surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from powdrr_service.providers.config import DEFAULT_MODEL, LLMModelMapping


@dataclass(frozen=True, slots=True)
class WorkflowChatConfig:
    processes_dir: Path
    repo_root: Path | None = None
    output_dir: Path | None = None
    provider: str = "auto"
    normal_provider: str | None = None
    adversarial_provider: str | None = None
    model: str = DEFAULT_MODEL
    llm_mappings: tuple[tuple[str, LLMModelMapping], ...] = ()
    api_key: str | None = None
    base_url: str | None = None
    max_turns: int = 8
    max_stalled_roundtrips: int = 3
    provider_retry_attempts: int = 3
    provider_retry_delay_seconds: float = 30.0
    verbose: bool = False
    execution_id: str | None = None
    conversation_id: str | None = None
    initial_message: str | None = None
