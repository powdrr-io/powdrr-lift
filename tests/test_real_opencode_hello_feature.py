from __future__ import annotations

import pytest

from powdrr_lift.cli import main


@pytest.mark.parametrize("command", ["run-coding-agent", "run_coding_agent"])
def test_legacy_coding_agent_cli_is_unavailable(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
