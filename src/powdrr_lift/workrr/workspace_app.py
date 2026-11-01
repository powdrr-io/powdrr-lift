"""Compose the local workspace API with the Procedrr design-chat adapter."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from powdrr_local.web import create_app
from powdrr_local.workspace import WorkspaceStore

from powdrr_lift.workrr.chat_config import WorkflowChatConfig
from powdrr_lift.workrr.workspace_chat_turn import WorkspaceChatTurnDispatcher


def build_workspace_app(
    *,
    data_dir: Path,
    bootstrap_secret: str,
    expected_origin: str,
    provider: str = "auto",
    normal_provider: str | None = None,
    adversarial_provider: str | None = None,
    model: str = "glm-5.2",
    api_key: str | None = None,
    base_url: str | None = None,
) -> FastAPI:
    """Build a runnable workspace API with repository-aware chat dispatch.

    The caller owns process launch, loopback binding, and browser startup. This
    factory supplies the real Procedrr adapter and keeps workspace data outside
    registered repositories.
    """
    resolved_data_dir = data_dir.expanduser().resolve()
    store = WorkspaceStore(resolved_data_dir / "workspace.sqlite3")
    process_directory = (
        Path(__file__).resolve().parents[3] / "docs" / "procedrr" / "processes"
    )
    workflow_config = WorkflowChatConfig(
        processes_dir=process_directory,
        provider=provider,
        normal_provider=normal_provider,
        adversarial_provider=adversarial_provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        defer_user_input=True,
    )
    turn_dispatcher = WorkspaceChatTurnDispatcher(
        store,
        store,
        workflow_config,
    )
    return create_app(
        store=store,
        bootstrap_secret=bootstrap_secret,
        expected_origin=expected_origin,
        repository_registry=store,
        turn_dispatcher=turn_dispatcher,
    )
