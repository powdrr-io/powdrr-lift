from __future__ import annotations

import pytest

from powdrr_lift.integrations.harbor._env import env_flag_is_enabled


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_env_flag_is_enabled_for_supported_true_values(value: str) -> None:
    assert env_flag_is_enabled(value)


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "on"])
def test_env_flag_is_disabled_for_other_values(value: str | None) -> None:
    assert not env_flag_is_enabled(value)
