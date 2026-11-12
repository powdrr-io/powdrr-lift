from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from powdrr_lift.cli import main


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _plan(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "execution-plan-v1",
                "plan_id": "hello-plan",
                "proposed_pr_fingerprint": "hello-plan-fingerprint",
                "units": [
                    {
                        "unit_id": "hello-world",
                        "objective": "Create a hello world program.",
                        "paths": ["hello_world.py"],
                        "validation_profiles": ["hello-world-output"],
                        "acceptance_criteria": [
                            "the program prints Hello, world!",
                        ],
                    }
                ],
                "allowed_paths": ["hello_world.py"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _compile_request(
    repo: Path, plan: Path, request: Path, *, allowed_command: str
) -> str:
    base_commit = _head(repo)
    assert (
        main(
            [
                "compile-implementation-request",
                "--plan",
                str(plan),
                "--unit-id",
                "hello-world",
                "--request-id",
                "hello-request",
                "--base-commit",
                base_commit,
                "--output",
                str(request),
                "--allowed-command",
                allowed_command,
            ]
        )
        == 0
    )
    return base_commit


def test_cli_compiles_and_runs_a_complete_coding_agent_handoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo")
    plan = _plan(tmp_path / "plan.json")
    request = tmp_path / "request.json"
    allowed_command = f"{sys.executable} *"
    base_commit = _compile_request(repo, plan, request, allowed_command=allowed_command)
    compiled_output = json.loads(capsys.readouterr().out)
    assert compiled_output["request_id"] == "hello-request"
    assert compiled_output["base_commit"] == base_commit
    assert compiled_output["allowed_paths"] == ["hello_world.py"]
    assert compiled_output["validation_profiles"] == ["hello-world-output"]

    artifacts = tmp_path / "artifacts"
    with pytest.raises(SystemExit) as error:
        main(["run-coding-agent"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not (repo / "hello_world.py").exists()
    assert not artifacts.exists()


def test_cli_rejects_legacy_coding_agent_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["run-coding-agent"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
