from __future__ import annotations

from pathlib import Path

from powdrr_service.structrr.validation import discover_validation_profiles


def _workflow(root: Path, content: str) -> None:
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(content, encoding="utf-8")


def test_unittest_ci_command_does_not_replace_pytest_profile(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    _workflow(
        tmp_path,
        "jobs:\n  checks:\n    steps:\n"
        "      - run: pytest -q tests\n"
        "      - run: python -m unittest discover -s scripts\n",
    )

    profiles = discover_validation_profiles(tmp_path)

    pytest = next(profile for profile in profiles if profile.provider == "pytest")
    unittest = next(profile for profile in profiles if profile.provider == "unittest")
    assert pytest.command == ("pytest", "-q", "tests")
    assert unittest.command == ("python", "-m", "unittest", "discover", "-s", "scripts")


def test_unittest_runner_scripts_get_a_distinct_provider(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    _workflow(
        tmp_path,
        "jobs:\n  tests:\n    steps:\n"
        "      - run: python -Wall tests/runtests.py -v2\n",
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.name == "django-unittest"
    )

    assert profile.provider == "unittest"
    assert profile.command == ("python", "-Wall", "tests/runtests.py", "-v2")


def test_reusable_workflow_jobs_remain_explicit_and_unresolved(
    tmp_path: Path,
) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "check-fmt.yml").write_text(
        "on: workflow_call\njobs: {}\n", encoding="utf-8"
    )
    (workflow_dir / "ci.yml").write_text(
        "jobs:\n  check-fmt:\n    uses: ./.github/workflows/check-fmt.yml\n"
        "  check:\n"
        "    uses: tox-dev/github-workflows/.github/workflows/check.yaml@abc123\n",
        encoding="utf-8",
    )

    profiles = discover_validation_profiles(tmp_path)

    local = next(
        profile for profile in profiles if profile.name == "reusable-check-fmt"
    )
    remote = next(
        profile for profile in profiles if profile.name == "reusable-check-tox-workflow"
    )
    assert local.command == remote.command == ()
    assert local.unresolved and remote.unresolved
    assert local.evidence == (
        ".github/workflows/ci.yml",
        ".github/workflows/check-fmt.yml",
    )


def test_tox_matrix_expression_is_an_unresolved_record(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text(
        "[testenv:py]\ncommands = python -m pytest\n", encoding="utf-8"
    )
    _workflow(
        tmp_path,
        "jobs:\n  test:\n    strategy:\n      matrix:\n        env: [py, lint]\n"
        "    steps:\n      - run: tox run -e ${{ matrix.env }}\n",
    )

    profile = next(
        item
        for item in discover_validation_profiles(tmp_path)
        if item.name == "tox-dynamic-matrix"
    )

    assert profile.command == ()
    assert profile.execution_kind == "unresolved"
    declared_command = (profile.settings or {}).get("declared_command")
    assert isinstance(declared_command, (list, tuple))
    assert "matrix.env" in declared_command
    assert profile.unresolved


def test_custom_ci_scripts_and_catalog_commands_are_accounted_for(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check").write_text("#!/bin/sh\nruff check .\n", encoding="utf-8")
    (scripts / "run-deterministic-workflow-suite.sh").write_text(
        "#!/bin/sh\nuv run powdrr-lift workflow-scenario-suite\n",
        encoding="utf-8",
    )
    _workflow(
        tmp_path,
        "jobs:\n  checks:\n    steps:\n"
        "      - run: scripts/check\n"
        "      - run: uv run powdrr-lift validate-procedrr-processes\n"
        "      - run: ./scripts/run-deterministic-workflow-suite.sh\n",
    )

    profiles = discover_validation_profiles(tmp_path)
    by_name = {profile.name: profile for profile in profiles}

    assert by_name["httpx-check-script"].command == ()
    assert by_name["httpx-check-script"].unresolved
    assert by_name["validate-procedrr-processes"].command == (
        "uv",
        "run",
        "powdrr-lift",
        "validate-procedrr-processes",
    )
    assert by_name["workflow-scenario-suite"].command == (
        "./scripts/run-deterministic-workflow-suite.sh",
    )


def test_backtick_line_continuations_do_not_create_a_fake_pytest_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\ndependencies = ["pytest"]\n',
        encoding="utf-8",
    )
    _workflow(
        tmp_path,
        "jobs:\n  windows:\n    steps:\n"
        "      - shell: pwsh\n"
        "        run: |\n"
        "          python -m pytest `\n"
        "            -k memray `\n"
        "            --memray\n",
    )

    profiles = discover_validation_profiles(tmp_path)
    test_profiles = [profile for profile in profiles if profile.provider == "pytest"]

    assert any(
        profile.command == ("python", "-m", "pytest", "-k", "memray", "--memray")
        for profile in test_profiles
    )
    assert not any(
        "`" in token for profile in test_profiles for token in profile.command
    )
