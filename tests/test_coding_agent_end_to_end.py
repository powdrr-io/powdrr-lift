from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

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


def _fake_opencode(path: Path) -> Path:
    path.write_text(
        f"""#!{sys.executable}
import json
from pathlib import Path

Path("hello_world.py").write_text('print("Hello, world!")\\n', encoding="utf-8")
print(json.dumps({{"type": "session.completed"}}))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


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
    tmp_path: Path, capsys
) -> None:
    repo = _git_repo(tmp_path / "repo")
    plan = _plan(tmp_path / "plan.json")
    request = tmp_path / "request.json"
    fake = _fake_opencode(tmp_path / "fake-opencode")
    allowed_command = f"{sys.executable} *"
    base_commit = _compile_request(repo, plan, request, allowed_command=allowed_command)
    compiled_output = json.loads(capsys.readouterr().out)
    assert compiled_output["request_id"] == "hello-request"
    assert compiled_output["base_commit"] == base_commit
    assert compiled_output["allowed_paths"] == ["hello_world.py"]
    assert compiled_output["validation_profiles"] == ["hello-world-output"]

    artifacts = tmp_path / "artifacts"
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
            "hello-attempt",
            "--opencode-executable",
            str(fake),
            "--validation-profile",
            f"hello-world-output={sys.executable} hello_world.py",
            "--timeout-seconds",
            "30",
            "--validation-timeout-seconds",
            "30",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["changed_paths"] == ["hello_world.py"]
    assert output["out_of_scope_paths"] == []
    assert output["validation"]["status"] == "passed"
    assert output["validation"]["results"][0]["stdout"] == "Hello, world!\n"
    assert (repo / "hello_world.py").read_text(encoding="utf-8") == (
        'print("Hello, world!")\n'
    )
    assert (
        subprocess.run(
            ["git", "status", "--short"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == "?? hello_world.py\n"
    )
    assert (artifacts / "requests" / "hello-request.json").exists()
    assert (artifacts / "attempts" / "hello-attempt.json").exists()
    assert (artifacts / "validations" / "hello-attempt.json").exists()


def test_cli_fails_closed_when_declared_validation_fails(
    tmp_path: Path, capsys
) -> None:
    repo = _git_repo(tmp_path / "repo")
    plan = _plan(tmp_path / "plan.json")
    request = tmp_path / "request.json"
    fake = _fake_opencode(tmp_path / "fake-opencode")
    _compile_request(repo, plan, request, allowed_command=f"{sys.executable} *")
    capsys.readouterr()

    artifacts = tmp_path / "artifacts"
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
            "hello-attempt",
            "--opencode-executable",
            str(fake),
            "--validation-profile",
            f'hello-world-output={sys.executable} -c "import sys; sys.exit(3)"',
        ]
    )

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["validation"]["status"] == "failed"
    assert output["validation"]["results"][0]["returncode"] == 3
    assert (artifacts / "validations" / "hello-attempt.json").exists()
