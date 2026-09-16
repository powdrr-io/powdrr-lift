#!/usr/bin/env python3
"""Generate reproducible ground-truth Python execution failures as JSONL."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "type_mismatch",
        "TypeError",
        "len(42)",
        "A built-in receives an incompatible value.",
    ),
    (
        "type_mismatch",
        "TypeError",
        "'count=' + 3",
        "An operation combines incompatible types.",
    ),
    (
        "missing_parameter",
        "TypeError",
        "def greet(name):\n    return name\ngreet()",
        "A required function parameter is omitted.",
    ),
    (
        "unexpected_keyword",
        "TypeError",
        "def greet(name):\n    return name\ngreet(user='Ada')",
        "A call supplies an unknown keyword.",
    ),
    (
        "multiple_values_for_argument",
        "TypeError",
        "def greet(name):\n    return name\ngreet('Ada', name='Grace')",
        "A parameter is supplied positionally and by keyword.",
    ),
    (
        "positional_only_argument",
        "TypeError",
        "def greet(name, /):\n    return name\ngreet(name='Ada')",
        "A positional-only parameter is passed by keyword.",
    ),
    (
        "keyword_only_argument",
        "TypeError",
        "def greet(*, name):\n    return name\ngreet('Ada')",
        "A keyword-only parameter is passed positionally.",
    ),
    (
        "wrong_arity",
        "TypeError",
        "def add(left, right):\n    return left + right\nadd(1)",
        "A callable receives the wrong number of arguments.",
    ),
    (
        "name_not_defined",
        "NameError",
        "print(variable_that_does_not_exist)",
        "A referenced name is not defined.",
    ),
    (
        "attribute_missing",
        "AttributeError",
        "value = object()\nvalue.missing_attribute",
        "An object has no requested attribute.",
    ),
    (
        "module_not_found",
        "ModuleNotFoundError",
        "import module_that_does_not_exist",
        "An imported module cannot be found.",
    ),
    (
        "import_name_missing",
        "ImportError",
        "from math import name_that_does_not_exist",
        "A requested module member cannot be imported.",
    ),
    (
        "key_missing",
        "KeyError",
        "values = {'present': 1}\nvalues['missing']",
        "A mapping key is absent.",
    ),
    (
        "index_out_of_range",
        "IndexError",
        "values = [1]\nvalues[4]",
        "A sequence index is outside its bounds.",
    ),
    (
        "value_invalid",
        "ValueError",
        "int('not-an-integer')",
        "A value has the right broad type but invalid contents.",
    ),
    (
        "zero_division",
        "ZeroDivisionError",
        "1 / 0",
        "An arithmetic operation divides by zero.",
    ),
    (
        "assertion_failed",
        "AssertionError",
        "assert 2 + 2 == 5",
        "An executable assertion is false.",
    ),
    (
        "file_not_found",
        "FileNotFoundError",
        "open('file_that_does_not_exist.txt')",
        "A requested file is absent.",
    ),
    (
        "syntax_error",
        "SyntaxError",
        "if True print('missing colon')",
        "The source cannot be parsed.",
    ),
    (
        "indentation_error",
        "IndentationError",
        "def broken():\nreturn 1",
        "The source has invalid indentation.",
    ),
)
ERROR_LINE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::(?:\s(?P<message>.*))?)?$"
)


def extract_error(stderr: str) -> tuple[str, str]:
    """Return the final exception class and message from a Python traceback."""
    for line in reversed(stderr.splitlines()):
        match = ERROR_LINE.match(line.strip())
        if match:
            return match.group("type"), match.group("message") or ""
    raise ValueError(f"could not find an exception line in stderr: {stderr!r}")


def run_case(
    python_executable: str, code: str, timeout: float
) -> tuple[str, str, str, int]:
    completed = subprocess.run(
        [python_executable, "-c", code], capture_output=True, text=True, timeout=timeout
    )
    exception_type, message = extract_error(completed.stderr)
    return exception_type, message, completed.stderr, completed.returncode


def generate(
    output: Path, python_executable: str, repetitions: int, timeout: float
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for repetition in range(repetitions):
        for index, (label, expected_type, code, scenario) in enumerate(CASES, 1):
            actual_type, message, traceback, returncode = run_case(
                python_executable, code, timeout
            )
            if actual_type != expected_type or returncode == 0:
                raise RuntimeError(
                    f"case {label} produced {actual_type!r}/{returncode}, "
                    f"expected {expected_type!r}/failure"
                )
            records.append(
                {
                    "id": f"{label}-{repetition + 1:03d}-{index:03d}",
                    "text": f"{actual_type}: {message}" if message else actual_type,
                    "label": label,
                    "exception_type": actual_type,
                    "message": message,
                    "scenario": scenario,
                    "code": code,
                    "traceback": traceback,
                    "returncode": returncode,
                    "source": "synthetic-python-subprocess",
                }
            )
    with output.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--python-executable", default=sys.executable)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    count = generate(
        args.output, args.python_executable, args.repetitions, args.timeout
    )
    print(f"wrote {count} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
