from __future__ import annotations

import pytest

from powdrr_lift.integrations.harbor._env import (
    env_flag_is_enabled,
    jev_allowed_domains,
    pip_install_command,
    provider_environment,
)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_env_flag_is_enabled_for_supported_true_values(value: str) -> None:
    assert env_flag_is_enabled(value)


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "on"])
def test_env_flag_is_disabled_for_other_values(value: str | None) -> None:
    assert not env_flag_is_enabled(value)


def test_pip_install_command_omits_user_flag_inside_virtualenv() -> None:
    command = pip_install_command("some-package==1.2.3", "--upgrade")

    assert command == (
        "if python3 -c 'import sys; raise SystemExit("
        "sys.prefix != sys.base_prefix)'; "
        "then python3 -m pip install --user --upgrade some-package==1.2.3; "
        "else python3 -m pip install --upgrade some-package==1.2.3; fi"
    )


def test_provider_environment_forwards_jev_and_research_credentials() -> None:
    configured = {
        "TYPESAFEAI_API_KEY": "typesafe-secret",
        "TAVILY_API_KEY": "tavily-secret",
    }

    assert provider_environment(configured.get) == configured


def test_jev_default_endpoint_is_always_allowed() -> None:
    assert jev_allowed_domains() == ["api.typesafe.ai"]


def test_custom_jev_endpoint_host_is_also_allowed() -> None:
    assert jev_allowed_domains("https://jev.internal.example/v1") == [
        "api.typesafe.ai",
        "jev.internal.example",
    ]
