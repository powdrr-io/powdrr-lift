from __future__ import annotations

import pytest

from powdrr_lift.integrations.harbor._env import (
    env_flag_is_enabled,
    pip_install_command,
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
