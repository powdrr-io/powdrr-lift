"""Interactive provider selection shared by workflow entry points."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

from powdrr_service.providers.config import provider_definition

from powdrr_lift.errors import PowdrrExecutionError
from powdrr_lift.workrr.providers import available_provider_names


def available_workflow_providers() -> tuple[str, ...]:
    """Return API-backed providers that have usable credentials configured."""
    return available_provider_names()


def choose_workflow_provider(
    *,
    input_func: Callable[[], str] = input,
    stdout: TextIO = sys.stdout,
) -> str:
    """Present configured API providers and return the user's selection."""
    providers = available_workflow_providers()
    if not providers:
        raise PowdrrExecutionError(
            "No workflow-chat providers are configured. Set an API key for at "
            "least one supported provider before starting workflow-chat."
        )
    stdout.write("Available workflow-chat providers:\n")
    for index, provider in enumerate(providers, start=1):
        definition = provider_definition(provider)
        stdout.write(f"  {index}. {definition.display_name} ({provider})\n")
    stdout.write("Choose a provider by number or name: ")
    stdout.flush()
    while True:
        answer = input_func().strip()
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(providers):
                stdout.write("\n")
                return providers[index]
        normalized = answer.casefold()
        for provider in providers:
            if normalized == provider.casefold():
                stdout.write("\n")
                return provider
        stdout.write(
            f"Choose a number from 1 to {len(providers)} or enter a provider name: "
        )
        stdout.flush()
