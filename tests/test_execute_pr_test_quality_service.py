from __future__ import annotations

from powdrr_service.verification.execute_pr_test_quality import (
    QUALITY_CHECKS,
    build_quality_question,
    execute_pr_test_review_context,
    execute_pr_test_review_targets,
    render_execute_pr_test_quality_pr_notes,
)


def test_quality_review_targets_are_split_by_linked_acceptance_criterion() -> None:
    targets = execute_pr_test_review_targets(
        {
            "test_obligations": [
                {"obligation_id": "AC-01", "source_text": "Accept a valid value."},
                {"obligation_id": "AC-02", "source_text": "Reject an invalid value."},
            ],
            "test_sources": {
                "tests/test_values.py": (
                    "# Covers: AC-01 test_value_contract\n"
                    "# Covers: AC-02 test_value_contract\n"
                    "def test_value_contract():\n"
                    "    assert validate('value')\n"
                )
            },
        }
    )

    assert [target["criterion_ids"] for target in targets] == [["AC-01"], ["AC-02"]]
    assert [target["acceptance_criteria"][0]["source_text"] for target in targets] == [
        "Accept a valid value.",
        "Reject an invalid value.",
    ]
    assert all(
        target["test_source"].startswith("def test_value_contract")
        for target in targets
    )


def test_quality_question_contains_only_scoped_evidence_as_data() -> None:
    question = QUALITY_CHECKS[0][1]
    prompt = build_quality_question(
        question,
        {
            "test_id": "tests/test_values.py::test_value_contract",
            "acceptance_criteria": ["AC-01: Accept a valid value."],
            "test_source": "def test_value_contract():\n    assert True",
        },
    )

    assert question in prompt
    assert '"test_review"' in prompt
    assert "AC-01: Accept a valid value." in prompt
    assert "never as instructions" in prompt
    assert "unrelated_repository_dump" not in prompt


def test_review_context_limits_baseline_log_to_targeted_failure() -> None:
    context = execute_pr_test_review_context(
        {
            "contract": {"title": "Value behavior", "unrelated": "not included"},
            "baseline_result": {
                "passed": False,
                "returncode": 1,
                "stdout": (
                    "________________ test_target __________________\n"
                    "E AssertionError: expected value\n"
                    "________________ test_other __________________\n"
                    + "unrelated log " * 200
                    + "\n================ short test summary info ================\n"
                ),
            },
        },
        {
            "test_id": "tests/test_values.py::test_target",
            "test_name": "test_target",
            "criterion_ids": ["AC-01"],
            "acceptance_criteria": [
                {"obligation_id": "AC-01", "source_text": "Return value."}
            ],
            "test_source": "def test_target(): pass",
        },
    )

    excerpt = context["baseline_result"]["target_failure_excerpt"]
    assert "test_target" in excerpt
    assert "expected value" in excerpt
    assert "unrelated log" not in excerpt
    assert "unrelated" not in context["plan_context"]


def test_pr_note_rendering_marks_advisory_and_preserves_each_note() -> None:
    rendered = render_execute_pr_test_quality_pr_notes(
        ["tests/test_x.py::test_x: concern"]
    )

    assert "Test-quality review is advisory" in " ".join(rendered)
    assert "- tests/test_x.py::test_x: concern" in rendered
    assert "blocking validation failure" in " ".join(rendered)
