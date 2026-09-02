import pytest

from powdrr_lift.intrinsic_enrich import execute_enrich_tool


def test_enrich_converts_pytest_tool_output_to_structured_result() -> None:
    result = execute_enrich_tool(
        {
            "format": "pytest",
            "tool_output": {
                "command": ["uv", "run", "pytest", "-q"],
                "returncode": 1,
                "stdout": (
                    "FAILED tests/test_example.py::test_one - AssertionError: no\n"
                ),
                "stderr": "",
                "cwd": "/repo",
            },
        }
    )

    assert result["tool"] == "enrich"
    assert result["format"] == "pytest"
    assert result["output"]["status"] == "failed"
    assert result["output"]["failures"][0]["node_id"] == (
        "tests/test_example.py::test_one"
    )


def test_enrich_accepts_passing_pytest_result() -> None:
    result = execute_enrich_tool(
        {
            "format": "pytest",
            "tool_output": {
                "command": "uv run pytest -q",
                "returncode": 0,
                "stdout": "10 passed\n",
                "stderr": "",
                "cwd": "/repo",
            },
        }
    )

    assert result["output"] == {"status": "passed", "failures": []}


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"format": "pytest"}, "tool_output"),
        (
            {
                "format": "unknown",
                "tool_output": {},
            },
            "format",
        ),
        (
            {
                "format": "pytest",
                "tool_output": {"command": ["pytest"]},
            },
            "missing required fields",
        ),
    ],
)
def test_enrich_rejects_invalid_parameters(
    parameters: dict[str, object], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        execute_enrich_tool(parameters)
