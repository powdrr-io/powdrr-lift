from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from powdrr_lift.cli import build_parser
from powdrr_lift.workrr.workspace_launch import (
    _load_or_create_instance_id,
    _workspace_instance_is_running,
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


def test_workspace_instance_id_is_persisted_for_data_directory(tmp_path: Path) -> None:
    first = _load_or_create_instance_id(tmp_path)

    assert len(first) >= 32
    assert _load_or_create_instance_id(tmp_path) == first
    assert (tmp_path / ".workspace-instance-id").read_text(encoding="ascii") == first


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"workspace_instance_id": "matching-id"}, True),
        (200, {"workspace_instance_id": "other-id"}, False),
        (200, {"status": "ok"}, False),
        (503, {"workspace_instance_id": "matching-id"}, False),
    ],
)
def test_running_workspace_requires_matching_healthy_instance(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: dict[str, str],
    expected: bool,
) -> None:
    class Response:
        def __init__(self) -> None:
            self.status = status

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    response = Response()
    monkeypatch.setattr(
        "powdrr_lift.workrr.workspace_launch.urllib.request.urlopen",
        lambda *_args, **_kwargs: response,
    )

    assert (
        _workspace_instance_is_running("http://127.0.0.1:8765", "matching-id")
        is expected
    )
