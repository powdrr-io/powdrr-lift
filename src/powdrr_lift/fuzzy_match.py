"""Find repository paths using find-like filters and fuzzy name matching."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def execute_fuzzy_match(
    command: str | Sequence[str],
    *,
    worktree_root: Path,
) -> dict[str, Any]:
    """Execute a structured fuzzy-match command and return JSON-compatible data."""
    command_items = _command_items(command)
    if not command_items or command_items[0] != "fuzzy-match":
        raise ValueError("fuzzy-match command must start with 'fuzzy-match'.")
    root, options = _parse_options(command_items[1:])
    search_root = _resolve_root(root, worktree_root)
    if not search_root.exists():
        return {
            "query": options["query"],
            "root": str(search_root),
            "matches": [],
        }
    if not search_root.is_dir():
        raise ValueError(f"fuzzy-match search root must be a directory: {root}")

    matches: list[dict[str, Any]] = []
    for path in _walk_paths(search_root, options["mindepth"], options["maxdepth"]):
        if options["type"] == "f" and not path.is_file():
            continue
        if options["type"] == "d" and not path.is_dir():
            continue
        relative_path = str(path.relative_to(worktree_root))
        name = path.name
        if options["path_pattern"] and not fnmatch.fnmatch(
            relative_path,
            options["path_pattern"],
        ):
            continue
        score = _fuzzy_score(options["query"], name, relative_path)
        if score < options["threshold"]:
            continue
        matches.append(
            {
                "path": relative_path,
                "name": name,
                "type": "file" if path.is_file() else "directory",
                "score": round(score, 4),
            }
        )

    matches.sort(key=lambda match: (-match["score"], match["path"]))
    return {
        "query": options["query"],
        "root": str(search_root),
        "matches": matches,
    }


def _command_items(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        import shlex

        return shlex.split(command)
    return [item for item in command if isinstance(item, str) and item]


def _parse_options(command: Sequence[str]) -> tuple[str, dict[str, Any]]:
    if not command or command[0].startswith("-"):
        raise ValueError("fuzzy-match requires a search root argument.")
    root = command[0]
    options: dict[str, Any] = {
        "query": "",
        "path_pattern": None,
        "type": None,
        "maxdepth": None,
        "mindepth": 0,
        "threshold": 0.35,
    }
    index = 1
    while index < len(command):
        option = command[index]
        if option in {"-print", "--print"}:
            index += 1
            continue
        if option in {"-name", "--name", "-iname", "--iname"}:
            value, index = _option_value(command, index, option)
            options["query"] = value
            continue
        if option in {"-path", "--path", "-ipath", "--ipath"}:
            value, index = _option_value(command, index, option)
            options["path_pattern"] = value
            continue
        if option in {"-type", "--type"}:
            value, index = _option_value(command, index, option)
            if value not in {"f", "d"}:
                raise ValueError("fuzzy-match -type must be 'f' or 'd'.")
            options["type"] = value
            continue
        if option in {"-maxdepth", "--maxdepth", "-mindepth", "--mindepth"}:
            value, index = _option_value(command, index, option)
            try:
                depth = int(value)
            except ValueError as error:
                raise ValueError(f"{option} must be an integer.") from error
            if depth < 0:
                raise ValueError(f"{option} must not be negative.")
            options["maxdepth" if "maxdepth" in option else "mindepth"] = depth
            continue
        if option in {"-threshold", "--threshold"}:
            value, index = _option_value(command, index, option)
            try:
                threshold = float(value)
            except ValueError as error:
                raise ValueError(f"{option} must be a number.") from error
            if not 0 <= threshold <= 1:
                raise ValueError(f"{option} must be between 0 and 1.")
            options["threshold"] = threshold
            continue
        raise ValueError(f"Unsupported fuzzy-match option: {option}")
    if not options["query"]:
        raise ValueError("fuzzy-match requires -name <query>.")
    return root, options


def _option_value(command: Sequence[str], index: int, option: str) -> tuple[str, int]:
    value_index = index + 1
    if value_index >= len(command) or not command[value_index]:
        raise ValueError(f"{option} requires a value.")
    return command[value_index], value_index + 1


def _resolve_root(root: str, worktree_root: Path) -> Path:
    root_path = Path(root)
    return root_path if root_path.is_absolute() else worktree_root / root_path


def _walk_paths(root: Path, mindepth: int, maxdepth: int | None) -> list[Path]:
    paths: list[Path] = []
    for current_root, directories, files in os.walk(root):
        current = Path(current_root)
        depth = len(current.relative_to(root).parts)
        if depth >= mindepth:
            paths.append(current)
        if maxdepth is not None and depth >= maxdepth:
            directories[:] = []
            continue
        paths.extend(sorted(current / filename for filename in files))
    return sorted(paths)


def _fuzzy_score(query: str, name: str, relative_path: str) -> float:
    query_tokens = _tokens(query)
    candidates = (name, Path(relative_path).stem, relative_path)
    scores = [
        difflib.SequenceMatcher(
            None, _normalized(query), _normalized(candidate)
        ).ratio()
        for candidate in candidates
    ]
    candidate_tokens = set(_tokens(name))
    if query_tokens and candidate_tokens:
        scores.append(
            len(set(query_tokens) & candidate_tokens) / len(set(query_tokens))
        )
    return max(scores)


def _normalized(value: str) -> str:
    return "".join(_tokens(value))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def fuzzy_match_json(command: str | Sequence[str], *, worktree_root: Path) -> str:
    """Return a stable JSON report for logging and LLM consumption."""
    return json.dumps(
        execute_fuzzy_match(command, worktree_root=worktree_root),
        indent=2,
        ensure_ascii=False,
    )
