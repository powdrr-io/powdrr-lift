from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from powdrr_lift.structrr.validation import discover_validation_profiles
from powdrr_lift.structrr.validation_tasks import discover_tox_configuration


def test_discovers_tox_ini_environment_and_preserves_native_wrapper(
    tmp_path: Path,
) -> None:
    (tmp_path / "tox.ini").write_text(
        """
[tox]
envlist = lint, py312

[testenv:lint]
commands =
    ruff check --select \"E,F\" src

[testenv:py312]
commands = pytest -m \"not integration\"
""",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)

    assert len(profiles) == 1
    assert profiles[0].provider == "aggregate"
    assert profiles[0].command == ("tox", "-e", "lint,py312")
    assert profiles[0].selectors == ("lint", "py312")
    assert profiles[0].settings == {
        "environments": ["lint", "py312"],
        "configured_commands": {
            "lint": [["ruff", "check", "--select", "E,F", "src"]],
            "py312": [["pytest", "-m", "not integration"]],
        },
    }


def test_uses_literal_ci_tox_invocation_and_reports_ambiguous_configs(
    tmp_path: Path,
) -> None:
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenvlist = lint, py312\n[testenv]\ncommands = pytest\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tox]\nenv_list = ["type"]\n', encoding="utf-8"
    )
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "checks.yml").write_text(
        "jobs:\n  test:\n    steps:\n"
        '      - run: uv run tox -e lint -- -k "edge case"\n',
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)
    configuration = discover_tox_configuration(tmp_path)

    tox_profile = next(
        profile for profile in profiles if profile.provider == "aggregate"
    )
    assert tox_profile.command == (
        "uv",
        "run",
        "tox",
        "-e",
        "lint",
        "--",
        "-k",
        "edge case",
    )
    assert tox_profile.selectors == ("lint",)
    assert tox_profile.evidence == (
        "tox.ini",
        "pyproject.toml",
        ".github/workflows/checks.yml",
    )
    assert tox_profile.unresolved
    assert configuration is not None
    assert configuration.config_files == ("tox.ini", "pyproject.toml")


def test_reads_tox_four_pyproject_and_standalone_toml_forms(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.tox]
env_list = ["lint", "py312"]

[tool.tox.env_run_base]
commands = [["pytest", "-m", "not integration"]]
""",
        encoding="utf-8",
    )

    profile = discover_validation_profiles(tmp_path)[0]
    assert profile.command == ("tox", "-e", "lint,py312")
    assert profile.selectors == ("lint", "py312")
    assert profile.settings is not None
    assert profile.settings["configured_commands"] == {
        "*": [["pytest", "-m", "not integration"]]
    }

    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "tox.toml").write_text(
        'env_list = ["type"]\n\n[env.type]\ncommands = [["mypy", "src"]]\n',
        encoding="utf-8",
    )
    profile = discover_validation_profiles(tmp_path)[0]
    assert profile.command == ("tox", "-e", "type")
    assert profile.settings is not None
    assert profile.settings["configured_commands"] == {"type": [["mypy", "src"]]}


def test_tox_config_without_environments_or_commands_does_not_invent_run(
    tmp_path: Path,
) -> None:
    (tmp_path / "tox.ini").write_text("[tox]\n", encoding="utf-8")

    assert discover_validation_profiles(tmp_path) == ()


def test_tox_aggregate_does_not_duplicate_its_configured_child_checks(
    tmp_path: Path,
) -> None:
    (tmp_path / "tox.ini").write_text(
        """
[tox]
envlist = lint

[testenv:lint]
commands =
    ruff check src
    pytest tests
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project.optional-dependencies]\ndev = ["ruff", "pytest"]\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    profiles = discover_validation_profiles(tmp_path)

    assert [profile.provider for profile in profiles] == ["aggregate"]
    assert profiles[0].settings is not None
    assert profiles[0].settings["configured_commands"] == {
        "lint": [["ruff", "check", "src"], ["pytest", "tests"]]
    }


def test_tox_factor_expressions_are_preserved_as_unresolved(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[tox]\nenvlist = py{311,312}-lint\n[testenv]\ncommands = ruff check .\n",
        encoding="utf-8",
    )

    profile = discover_validation_profiles(tmp_path)[0]

    assert profile.selectors == ("py{311,312}-lint",)
    assert any("retained symbolically" in item for item in profile.unresolved)


def test_discovers_nox_sessions_from_ast_without_running_noxfile(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed"
    (tmp_path / "noxfile.py").write_text(
        """
from pathlib import Path
Path("executed").write_text("no")
import nox

nox.options.sessions = ["lint", "tests"]

@nox.session(name="lint", python=["3.11", "3.12"])
def lint(session):
    session.install("ruff")
    session.run("ruff", "check", "src")

@nox.session
@nox.parametrize("python", ["3.11", "3.12"])
def tests(session):
    session.install("pytest")
    session.run("pytest", "-m", "not integration")
""",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)

    assert [profile.name for profile in profiles] == ["nox-lint", "nox-tests"]
    assert profiles[0].command == ("nox", "-s", "lint")
    assert profiles[0].settings is not None
    lint_settings = cast(dict[str, Any], profiles[0].settings)
    assert lint_settings["sessions"]["lint"]["python"] == ["3.11", "3.12"]
    assert lint_settings["sessions"]["lint"]["calls"] == [
        {
            "method": "install",
            "arguments": ["ruff"],
            "keyword_arguments": {},
            "line": 10,
            "dynamic": False,
        },
        {
            "method": "run",
            "arguments": ["ruff", "check", "src"],
            "keyword_arguments": {},
            "line": 11,
            "dynamic": False,
        },
    ]
    assert profiles[1].settings is not None
    test_settings = cast(dict[str, Any], profiles[1].settings)
    assert test_settings["sessions"]["tests"]["parameters"] == {
        "python": ["3.11", "3.12"]
    }
    assert not marker.exists()


def test_nox_ci_command_retains_arguments_and_workflow_evidence(tmp_path: Path) -> None:
    (tmp_path / "noxfile.py").write_text(
        'import nox\n@nox.session\ndef tests(session):\n    session.run("pytest")\n',
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/tests.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n"
        '      - run: uv run nox -s tests -- -k "edge case"\n',
        encoding="utf-8",
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.provider == "aggregate"
    )

    assert profile.command == (
        "uv",
        "run",
        "nox",
        "-s",
        "tests",
        "--",
        "-k",
        "edge case",
    )
    assert profile.selectors == ("tests",)
    assert profile.evidence == ("noxfile.py", ".github/workflows/tests.yml")


def test_nox_dynamic_session_arguments_remain_unresolved(tmp_path: Path) -> None:
    (tmp_path / "noxfile.py").write_text(
        """
import nox

@nox.session
def tests(session):
    command = "pytest"
    session.run(command, "tests")
""",
        encoding="utf-8",
    )

    profile = discover_validation_profiles(tmp_path)[0]

    assert profile.command == ("nox", "-s", "tests")
    assert profile.unresolved == (
        "Session tests contains dynamic install/run arguments.",
    )
    assert profile.settings is not None
    dynamic_settings = cast(dict[str, Any], profile.settings)
    call = dynamic_settings["sessions"]["tests"]["calls"][0]
    assert call["dynamic"] is True
    assert call["arguments"] == ["command", "tests"]


def test_noxfile_parse_failure_emits_unresolved_check(tmp_path: Path) -> None:
    (tmp_path / "noxfile.py").write_text("import nox\ndef broken(:\n", encoding="utf-8")

    profile = discover_validation_profiles(tmp_path)[0]

    assert profile.name == "nox-all"
    assert profile.execution_kind == "unresolved"
    assert profile.command == ()
    assert "Could not statically parse noxfile.py" in profile.unresolved[0]
