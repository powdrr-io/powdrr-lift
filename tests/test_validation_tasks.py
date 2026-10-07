from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from powdrr_lift.structrr.validation import discover_validation_profiles
from powdrr_lift.structrr.validation_tasks import (
    discover_just_configuration,
    discover_make_configuration,
    discover_pre_commit_configuration,
    discover_task_configuration,
    discover_tox_configuration,
    make_target_closure,
    task_runner_closure,
)


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


def test_make_includes_and_dependencies_are_preserved_as_one_aggregate(
    tmp_path: Path,
) -> None:
    (tmp_path / "Makefile").write_text(
        ".PHONY: check lint test types\n"
        "include make/tasks.mk\n"
        "check: lint test types\n\t@$(MAKE) lint test types\n",
        encoding="utf-8",
    )
    included = tmp_path / "make/tasks.mk"
    included.parent.mkdir()
    included.write_text(
        "lint:\n\truff check --select 'E,F' src\n\n"
        "test:\n\tpytest tests -m 'not integration'\n\n"
        "types:\n\tmypy src\n",
        encoding="utf-8",
    )

    configuration = discover_make_configuration(tmp_path)
    profiles = discover_validation_profiles(tmp_path)

    assert configuration is not None
    assert configuration.config_files == ("Makefile", "make/tasks.mk")
    assert [profile.name for profile in profiles] == ["make-check"]
    profile = profiles[0]
    assert profile.provider == "aggregate"
    assert profile.command == ("make", "check")
    assert profile.config_files == ("Makefile", "make/tasks.mk")
    assert profile.settings is not None
    settings = cast(dict[str, Any], profile.settings)
    target_settings = settings["targets"]
    assert target_settings["check"]["prerequisites"] == ["lint", "test", "types"]
    assert target_settings["test"]["recipes"] == ["pytest tests -m 'not integration'"]
    assert {item.provider for item in profiles} == {"aggregate"}
    closure, diagnostics = make_target_closure(configuration, ("check",))
    assert [item.name for item in closure] == ["check", "lint", "test", "types"]
    assert not diagnostics


def test_make_ci_command_and_workflow_path_preserve_quoted_arguments(
    tmp_path: Path,
) -> None:
    (tmp_path / "Makefile").write_text(
        ".PHONY: test\ntest:\n\tpytest tests\n", encoding="utf-8"
    )
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n"
        '      - run: make -C . test PYTEST_ARGS="-k edge case"\n',
        encoding="utf-8",
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.name.startswith("make-")
    )

    assert profile.command == (
        "make",
        "-C",
        ".",
        "test",
        "PYTEST_ARGS=-k edge case",
    )
    assert profile.selectors == ("test",)
    assert profile.evidence == (
        "validation-input:Makefile",
        "validation-input:.github/workflows/ci.yml",
    )


def test_make_dynamic_recipes_and_include_cycles_remain_unresolved(
    tmp_path: Path,
) -> None:
    (tmp_path / "Makefile").write_text(
        ".PHONY: check\ninclude extra.mk\n"
        "check: included\n\t$(PYTEST) tests | tee results\n",
        encoding="utf-8",
    )
    (tmp_path / "extra.mk").write_text(
        "included:\ninclude Makefile\n", encoding="utf-8"
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.name.startswith("make-")
    )

    assert any("include cycle" in item for item in profile.unresolved)
    assert any("Shell or variable expansion" in item for item in profile.unresolved)


def test_make_setup_target_is_not_assumed_to_be_validation(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        ".PHONY: install\ninstall:\n\tpython -m pip install -e .\n",
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  setup:\n    steps:\n      - run: make install\n", encoding="utf-8"
    )

    profile = discover_validation_profiles(tmp_path)[0]

    assert profile.provider == "custom"
    assert any("target names alone" in item for item in profile.unresolved)


def test_make_aggregate_does_not_hide_unselected_make_targets(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        ".PHONY: test lint\ntest:\n\tpytest tests\nlint:\n\truff check src\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 88\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  test:\n    steps:\n      - run: make test\n", encoding="utf-8"
    )

    profiles = discover_validation_profiles(tmp_path)

    assert "make-test" in [profile.name for profile in profiles]
    assert "pytest" not in [profile.name for profile in profiles]
    assert {"ruff-check", "ruff-format-check"}.issubset(
        {profile.name for profile in profiles}
    )


def test_pre_commit_configuration_preserves_hook_environment_and_filters(
    tmp_path: Path,
) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "default_stages: [pre-commit, pre-push]\n"
        "fail_fast: true\n"
        "repos:\n"
        "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
        "    rev: v0.8.0\n"
        "    hooks:\n"
        "      - id: ruff\n"
        "        args: [--fix]\n"
        "        files: '\\.py$'\n"
        "        types: [python]\n"
        "        pass_filenames: false\n"
        "        additional_dependencies: [typing-extensions]\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: unit-tests\n"
        "        name: unit tests\n"
        "        entry: pytest tests/unit\n"
        "        language: system\n"
        "        types: [python]\n",
        encoding="utf-8",
    )

    configuration = discover_pre_commit_configuration(tmp_path)

    assert configuration is not None
    assert configuration.config_file == ".pre-commit-config.yaml"
    assert configuration.settings == {
        "default_stages": ["pre-commit", "pre-push"],
        "fail_fast": True,
    }
    ruff_hook, local_hook = configuration.hooks
    assert (ruff_hook.repo, ruff_hook.rev, ruff_hook.args) == (
        "https://github.com/astral-sh/ruff-pre-commit",
        "v0.8.0",
        ("--fix",),
    )
    assert ruff_hook.pass_filenames is False
    assert ruff_hook.files == r"\.py$"
    assert ruff_hook.additional_dependencies == ("typing-extensions",)
    assert (local_hook.repo, local_hook.entry, local_hook.language) == (
        "local",
        "pytest tests/unit",
        "system",
    )
    assert not configuration.unresolved


def test_pre_commit_ci_invocation_is_an_aggregate_with_config_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: lint\n"
        "        entry: ruff check src\n"
        "        language: system\n",
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  checks:\n    steps:\n"
        "      - run: uv run pre-commit run lint --all-files\n",
        encoding="utf-8",
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.provider == "aggregate" and item.name.startswith("pre-commit-")
    )

    assert profile.command == (
        "uv",
        "run",
        "pre-commit",
        "run",
        "lint",
        "--all-files",
    )
    assert profile.selectors == ("lint",)
    assert profile.config_files == (".pre-commit-config.yaml",)
    assert profile.evidence == (
        "validation-input:.pre-commit-config.yaml",
        "validation-input:.github/workflows/ci.yml",
    )
    assert profile.settings is not None
    hook_list = cast(list[dict[str, object]], profile.settings["hooks"])
    hook_settings = hook_list[0]
    assert hook_settings["entry"] == "ruff check src"


def test_pre_commit_aggregate_does_not_duplicate_its_configured_validator(
    tmp_path: Path,
) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: lint\n"
        "        entry: ruff check src\n"
        "        language: system\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 88\n", encoding="utf-8"
    )
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  checks:\n    steps:\n      - run: pre-commit run lint --all-files\n",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)

    assert "pre-commit-lint" in [profile.name for profile in profiles]
    assert "ruff-check" not in [profile.name for profile in profiles]


def test_pre_commit_cli_config_and_files_arguments_are_not_hook_ids(
    tmp_path: Path,
) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos: [{repo: local, hooks: [{id: default, entry: true, "
        "language: system}]}]\n",
        encoding="utf-8",
    )
    custom_config = tmp_path / "config/hooks.yaml"
    custom_config.parent.mkdir()
    custom_config.write_text(
        "repos: [{repo: local, hooks: [{id: lint, entry: 'ruff check', "
        "language: system}]}]\n",
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  checks:\n    steps:\n"
        "      - run: pre-commit run lint --config config/hooks.yaml "
        "--files src/a.py\n",
        encoding="utf-8",
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.name.startswith("pre-commit-")
    )

    assert profile.selectors == ("lint",)
    assert profile.config_files == ("config/hooks.yaml",)
    assert profile.settings is not None
    hook_list = cast(list[dict[str, object]], profile.settings["hooks"])
    assert hook_list[0]["id"] == "lint"


def test_just_ci_invocation_preserves_dependencies_and_recipes(
    tmp_path: Path,
) -> None:
    (tmp_path / "Justfile").write_text(
        "lint:\n\truff check src\n\n"
        "test:\n\tpytest tests -m 'not integration'\n\n"
        "ci: lint test\n\tjust lint\n\tjust test\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 88\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  checks:\n    steps:\n      - run: just ci\n",
        encoding="utf-8",
    )

    configuration = discover_just_configuration(tmp_path)
    profiles = discover_validation_profiles(tmp_path)
    profile = next(item for item in profiles if item.name == "just-ci")

    assert configuration is not None
    closure, diagnostics = task_runner_closure(configuration, ("ci",))
    assert [task.name for task in closure] == ["ci", "lint", "test"]
    assert not diagnostics
    assert profile.provider == "aggregate"
    assert profile.command == ("just", "ci")
    assert profile.settings is not None
    settings = cast(dict[str, Any], profile.settings)
    assert settings["tasks"]["test"]["commands"] == [
        "pytest tests -m 'not integration'"
    ]
    assert "ruff-check" not in [item.name for item in profiles]
    assert "pytest" not in [item.name for item in profiles]


def test_taskfile_configuration_and_invocation_keep_native_context(
    tmp_path: Path,
) -> None:
    taskfile = tmp_path / "Taskfile.yml"
    taskfile.write_text(
        "version: '3'\n"
        "tasks:\n"
        "  lint:\n"
        "    cmds: [ruff check src]\n"
        "  test:\n"
        "    cmds:\n"
        "      - cmd: pytest tests -m 'not integration'\n"
        "        silent: true\n"
        "  check:\n"
        "    deps: [lint, test]\n"
        "    dir: backend\n"
        "    cmds: [task lint, task test]\n",
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  checks:\n    steps:\n"
        "      - run: task --taskfile Taskfile.yml check\n",
        encoding="utf-8",
    )

    configuration = discover_task_configuration(tmp_path)
    profiles = discover_validation_profiles(tmp_path)
    profile = next(item for item in profiles if item.name == "task-check")

    assert configuration is not None
    closure, diagnostics = task_runner_closure(configuration, ("check",))
    assert [task.name for task in closure] == ["check", "lint", "test"]
    assert any("modifiers" in item for item in diagnostics)
    assert any("dir" in item for item in diagnostics)
    assert profile.provider == "aggregate"
    assert profile.command == ("task", "--taskfile", "Taskfile.yml", "check")
    assert profile.selectors == ("check",)
    assert profile.config_files == ("Taskfile.yml",)
    assert any("modifiers" in item for item in profile.unresolved)
    assert profile.settings is not None
    task_settings = cast(dict[str, Any], profile.settings["tasks"])
    assert task_settings["check"]["execution_settings"]["dir"] == "backend"


def test_just_parameter_and_taskfile_include_remain_unresolved(
    tmp_path: Path,
) -> None:
    (tmp_path / "justfile").write_text(
        "set shell := ['bash', '-cu']\n"
        "alias check := test\n"
        "test *args:\n\tpytest {{args}}\n",
        encoding="utf-8",
    )
    taskfile = tmp_path / "Taskfile.yaml"
    taskfile.write_text(
        "version: '3'\n"
        "includes: {shared: ./tasks/Taskfile.yaml}\n"
        "tasks: {check: {deps: ['shared:test']}}\n",
        encoding="utf-8",
    )

    just_configuration = discover_just_configuration(tmp_path)
    task_configuration = discover_task_configuration(tmp_path)

    assert just_configuration is not None
    assert any(
        "Parameterized Just recipe" in item
        for item in just_configuration.tasks["test"].unresolved
    )
    assert any("Just alias" in item for item in just_configuration.unresolved)
    assert any(
        "Just setting/variable" in item for item in just_configuration.unresolved
    )
    assert task_configuration is not None
    assert any("includes" in item for item in task_configuration.unresolved)
