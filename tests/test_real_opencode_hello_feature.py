from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from powdrr_lift.cli import main


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "hello_world.py").write_text('print("Hello, world!")\n', encoding="utf-8")
    subprocess.run(["git", "add", "hello_world.py"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial hello world app",
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


@pytest.mark.real_coding_loop
def test_real_opencode_adds_a_specified_hello_world_feature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run the complete plan/request/real-OpenCode/validation handoff.

    This is deliberately opt-in because it invokes a real model and may incur
    provider cost. The fixture is a tiny real repository so failures remain
    easy to inspect when the model behaves unexpectedly.
    """
    if os.environ.get("POWDRR_LIFT_RUN_LIVE_CODING_LOOP") != "1":
        pytest.skip("set POWDRR_LIFT_RUN_LIVE_CODING_LOOP=1 to run the live test")
    if shutil.which("opencode") is None:
        pytest.skip("opencode is not installed")

    repo = _git_repo(tmp_path / "repo")
    plan = tmp_path / "feature-plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": "execution-plan-v1",
                "plan_id": "hello-world-second-line",
                "proposed_pr_fingerprint": "hello-world-second-line-v1",
                "units": [
                    {
                        "unit_id": "add-second-greeting",
                        "objective": (
                            "Update hello_world.py by adding a second line that "
                            "prints exactly Hello from Powdrr!. Keep the existing "
                            "Hello, world! line unchanged."
                        ),
                        "paths": ["hello_world.py"],
                        "validation_profiles": ["hello-world-output"],
                        "acceptance_criteria": [
                            "hello_world.py still prints Hello, world! first",
                            "hello_world.py prints Hello from Powdrr! second",
                            "no file other than hello_world.py is changed",
                        ],
                    }
                ],
                "allowed_paths": ["hello_world.py"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    request = tmp_path / "implementation-request.json"
    base_commit = _head(repo)
    assert (
        main(
            [
                "compile-implementation-request",
                "--plan",
                str(plan),
                "--unit-id",
                "add-second-greeting",
                "--request-id",
                "hello-world-second-line-request",
                "--base-commit",
                base_commit,
                "--output",
                str(request),
                "--context-ref",
                "fixture:hello-world-app",
                "--allowed-command",
                "python3 *",
            ]
        )
        == 0
    )
    capsys.readouterr()

    artifacts = tmp_path / "artifacts"
    timeout = os.environ.get("POWDRR_LIVE_CODING_AGENT_TIMEOUT", "600")
    result = main(
        [
            "run-coding-agent",
            "--request",
            str(request),
            "--worktree",
            str(repo),
            "--output-dir",
            str(artifacts),
            "--attempt-id",
            "hello-world-second-line-attempt",
            "--opencode-executable",
            "opencode",
            "--timeout-seconds",
            timeout,
            "--validation-timeout-seconds",
            "30",
            "--validation-profile",
            "hello-world-output=python3 hello_world.py",
        ]
    )

    assert result == 0, capsys.readouterr().out
    output = json.loads(capsys.readouterr().out)
    assert output["provider"] == "opencode"
    assert output["status"] == "completed"
    assert output["changed_paths"] == ["hello_world.py"]
    assert output["out_of_scope_paths"] == []
    assert output["validation"]["status"] == "passed"
    assert output["validation"]["results"][0]["stdout"] == (
        "Hello, world!\nHello from Powdrr!\n"
    )
    assert (repo / "hello_world.py").read_text(encoding="utf-8") == (
        'print("Hello, world!")\nprint("Hello from Powdrr!")\n'
    )
    assert (
        subprocess.run(
            ["git", "status", "--short"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == " M hello_world.py\n"
    )
    assert (artifacts / "requests" / "hello-world-second-line-request.json").exists()
    assert (artifacts / "attempts" / "hello-world-second-line-attempt.json").exists()
    assert (artifacts / "validations" / "hello-world-second-line-attempt.json").exists()
