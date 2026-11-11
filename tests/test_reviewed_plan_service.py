from __future__ import annotations

from powdrr_service.planning.reviewed_plan import (
    implementation_step_updates_path,
    reviewed_file_creation_conflicts,
)


def test_reviewed_plan_flags_only_existing_paths_explicitly_to_be_created() -> None:
    findings = reviewed_file_creation_conflicts(
        allowed_paths=("tests/test_existing.py", "scripts/verify.sh"),
        implementation_steps=(
            "Create tests/test_existing.py with a behavior assertion.",
            "Add a workflow that runs scripts/verify.sh.",
        ),
        base_commit="abc123",
        existing_file_sha256={
            "tests/test_existing.py": "test-digest",
            "scripts/verify.sh": "script-digest",
        },
    )

    assert len(findings) == 1
    assert "tests/test_existing.py" in findings[0]
    assert "test-digest" in findings[0]
    assert "Create tests/test_existing.py" in findings[0]


def test_reviewed_plan_does_not_flag_references_or_missing_base_files() -> None:
    findings = reviewed_file_creation_conflicts(
        allowed_paths=("src/module.py", "tests/new_test.py"),
        implementation_steps=(
            "Update src/module.py and add validation to it.",
            "Create tests/new_test.py with a public behavior assertion.",
        ),
        base_commit="abc123",
        existing_file_sha256={"src/module.py": "module-digest"},
    )

    assert findings == ()


def test_reviewed_plan_allows_explicit_create_or_update_of_existing_file() -> None:
    findings = reviewed_file_creation_conflicts(
        allowed_paths=(".github/workflows/ci.yml",),
        implementation_steps=(
            "Create or update .github/workflows/ci.yml with the reviewed settings.",
        ),
        base_commit="abc123",
        existing_file_sha256={".github/workflows/ci.yml": "workflow-digest"},
    )

    assert findings == ()


def test_implementation_step_updates_path_recognizes_explicit_update_language() -> None:
    assert implementation_step_updates_path(
        "Create or update .github/workflows/ci.yml with the reviewed settings.",
        ".github/workflows/ci.yml",
    )
    assert implementation_step_updates_path(
        "Update the existing .github/workflows/ci.yml with the reviewed settings.",
        ".github/workflows/ci.yml",
    )
    assert not implementation_step_updates_path(
        "Create .github/workflows/ci.yml with the reviewed settings.",
        ".github/workflows/ci.yml",
    )


def test_reviewed_plan_ignores_unsafe_paths() -> None:
    findings = reviewed_file_creation_conflicts(
        allowed_paths=("../outside.py", "/absolute.py"),
        implementation_steps=(
            "Create ../outside.py.",
            "Create /absolute.py.",
        ),
        base_commit="abc123",
        existing_file_sha256={
            "../outside.py": "outside-digest",
            "/absolute.py": "absolute-digest",
        },
    )

    assert findings == ()
