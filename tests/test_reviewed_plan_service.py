from __future__ import annotations

from powdrr_service.planning.reviewed_plan import reviewed_file_creation_conflicts


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
