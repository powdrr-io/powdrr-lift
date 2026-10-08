from __future__ import annotations

from pathlib import Path

from powdrr_lift.structrr.validation import discover_validation_profiles


def test_discovers_project_checks_from_configuration_and_ci(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project.optional-dependencies]
dev = ["pytest", "ruff", "mypy"]

[tool.ruff]
line-length = 88

[tool.mypy]
python_version = "3.12"
""",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        """
jobs:
  check:
    steps:
      - run: uv run ruff format --check .
      - run: uv run ruff check .
      - run: uv run mypy src tests
      - run: uv run pytest -n auto tests
""",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(
        tmp_path, explicit_command=("uv", "run", "pytest", "tests", "test_app.py")
    )

    assert [profile.name for profile in profiles] == [
        "feature-validation",
        "ruff-format-check",
        "ruff-check",
        "mypy",
        "pytest",
    ]
    assert profiles[1].command == ("uv", "run", "ruff", "format", "--check", ".")
    assert profiles[2].command == ("uv", "run", "ruff", "check", ".")
    assert profiles[3].command == ("uv", "run", "mypy", "src", "tests")
    assert profiles[4].command == ("uv", "run", "pytest", "-n", "auto", "tests")


def test_explicit_validation_command_is_used_when_no_tooling_is_detected(
    tmp_path: Path,
) -> None:
    profiles = discover_validation_profiles(
        tmp_path, explicit_command=("python", "-m", "pytest")
    )

    assert profiles[0].name == "feature-validation"
    assert profiles[0].command == ("python", "-m", "pytest")


def test_ignores_unresolved_ci_templates_for_local_validation_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        """
jobs:
  test:
    steps:
      - run: pytest tests --${{ matrix.dependency }}-only
""",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)

    assert [profile.name for profile in profiles] == ["pytest"]
    assert profiles[0].command == ("pytest", "-q")


def test_records_github_workflow_context_for_multiline_validation(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length=88\n")
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "checks.yml").write_text(
        """
name: Checks
on:
  pull_request:
    branches: [main]
    paths: ['src/**', 'tests/**']
jobs:
  validate:
    if: github.event.pull_request.draft == false
    defaults:
      run:
        shell: bash
        working-directory: app
    steps:
      - name: Run formatter
        run: |
          uv run ruff format --check .
          uv run ruff check .
""",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)
    formatter = next(
        profile for profile in profiles if profile.name == "ruff-format-check"
    )

    assert formatter.ci_origins[0]["triggers"] == {
        "pull_request": {"branches": ["main"], "paths": ["src/**", "tests/**"]}
    }
    origin = formatter.ci_origins[0]
    assert origin["job_if"] == "github.event.pull_request.draft == false"
    assert origin["shell"] == "bash"
    assert origin["working_directory"] == "app"
    assert "uv run ruff check ." in str(origin["run"])


def test_discovers_polyglot_project_validation(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest", "lint": "eslint ."}}\n',
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text("module example.test\n", encoding="utf-8")

    profiles = discover_validation_profiles(tmp_path)

    assert [(profile.name, profile.command) for profile in profiles] == [
        ("npm-test", ("npm", "test")),
        ("go-test", ("go", "test", "./...")),
    ]


def test_returns_no_profiles_when_repository_declares_no_validator(
    tmp_path: Path,
) -> None:
    profiles = discover_validation_profiles(tmp_path)

    assert profiles == ()
