from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from powdrr_lift.cli import build_parser
from powdrr_lift.workrr.workspace_launch import (
    default_workspace_data_dir,
    positive_port,
)


@pytest.mark.parametrize(
    ("platform_name", "environment", "home", "expected"),
    [
        (
            "darwin",
            {},
            Path("/Users/alex"),
            Path("/Users/alex/Library/Application Support/powdrr-lift"),
        ),
        (
            "linux",
            {"XDG_DATA_HOME": "/var/data/alex"},
            Path("/home/alex"),
            Path("/var/data/alex/powdrr-lift"),
        ),
        (
            "linux",
            {},
            Path("/home/alex"),
            Path("/home/alex/.local/share/powdrr-lift"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": "C:/Users/alex/AppData/Local"},
            Path("C:/Users/alex"),
            Path("C:/Users/alex/AppData/Local/powdrr-lift"),
        ),
    ],
)
def test_default_workspace_data_dir_uses_os_convention(
    platform_name: str,
    environment: dict[str, str],
    home: Path,
    expected: Path,
) -> None:
    assert (
        default_workspace_data_dir(
            platform_name=platform_name,
            environment=environment,
            home=home,
        )
        == expected
    )


def test_ui_command_accepts_launch_options() -> None:
    args = build_parser().parse_args(
        [
            "ui",
            "--repo-root",
            "/work/project",
            "--port",
            "8766",
            "--no-open",
            "--data-dir",
            "/tmp/powdrr-data",
        ]
    )

    assert args.command == "ui"
    assert args.repo_root == Path("/work/project")
    assert args.port == 8766
    assert args.no_open is True
    assert args.data_dir == Path("/tmp/powdrr-data")


@pytest.mark.parametrize("value", ["0", "-1", "65536", "nope"])
def test_ui_port_must_be_valid(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        positive_port(value)
