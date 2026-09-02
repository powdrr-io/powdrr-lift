"""Progressive-discovery help for workflow builtin tools."""

from __future__ import annotations

from typing import Any

BUILTIN_TOOL_NAMES = (
    "shell",
    "internal",
    "git",
    "gh",
    "fuzzy-match",
    "basedpyright-symbol",
    "basedpyright-structure",
    "validate_edit",
    "apply_edit",
)

_HELP: dict[str, dict[str, Any]] = {
    "shell": {
        "summary": "Run a shell command in the current worktree.",
        "when_to_use": (
            "Use for repository inspection, tests, formatting, linting, builds, "
            "and other commands that are not covered by an intrinsic tool. "
            "Keep commands scoped to the current worktree."
        ),
        "parameters": {
            "command": "A command string or argv array.",
            "cwd": "Optional directory inside the worktree.",
            "env": "Optional string-to-string environment overrides.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"command": ["rg", "TODO", "src"]},
            {"command": ["uv", "run", "pytest", "-q"]},
        ],
    },
    "internal": {
        "summary": "Run one powdrr-lift CLI command in the current worktree.",
        "when_to_use": (
            "Use for repository-specific operations exposed by the powdrr-lift "
            "CLI, such as generating or validating specifications. It may not "
            "invoke arbitrary executables."
        ),
        "parameters": {
            "command": "An argv array beginning with powdrr-lift.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"command": ["powdrr-lift", "repository-state"]},
            {"command": ["powdrr-lift", "validate", "--help"]},
        ],
    },
    "git": {
        "summary": "Perform an allow-listed Git operation in the current worktree.",
        "when_to_use": (
            "Use instead of shell for status, staging approved paths, or moving "
            "files. Structured operations keep repository mutations explicit."
        ),
        "parameters": {
            "operation": "One of status, add, or move.",
            "paths": "Relative paths for add.",
            "source": "Relative source path for move.",
            "destination": "Relative destination path for move.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"operation": "status"},
            {"operation": "add", "paths": ["src/example.py", "tests/test_example.py"]},
            {"operation": "move", "source": "old.yaml", "destination": "new.yaml"},
        ],
    },
    "gh": {
        "summary": "Perform an allow-listed GitHub pull-request operation.",
        "when_to_use": (
            "Use for pull-request inspection, creation, editing, checks, comments, "
            "and inline review comments. Use the GitHub intrinsic rather than "
            "shelling out to arbitrary network commands."
        ),
        "parameters": {
            "operation": (
                "One of pr_view, pr_diff, pr_checks, pr_create, pr_edit, "
                "pr_comments, or pr_review_comment."
            ),
            "pr_reference": (
                "Pull-request number or branch for inspection operations only; "
                "the runtime selects the target for pr_edit."
            ),
            "title": "Pull-request title for pr_create or pr_edit.",
            "body": "Pull-request body for pr_create or pr_edit.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"operation": "pr_view", "pr_reference": "394"},
            {"operation": "pr_checks", "pr_reference": "394"},
            {
                "operation": "pr_create",
                "title": "Fix validation",
                "body": "Summary and tests.",
            },
            {
                "operation": "pr_edit",
                "title": "Fix validation",
                "body": "Updated summary and tests.",
            },
        ],
    },
    "fuzzy-match": {
        "summary": "Find worktree paths by fuzzy name and find-like filters.",
        "when_to_use": (
            "Use when the exact path is unknown and a focused filename or directory "
            "search is needed. Prefer this over broad shell searches for path "
            "discovery."
        ),
        "parameters": {
            "command": (
                "An argv array beginning with fuzzy-match and a root, followed by "
                "-name and a query."
            ),
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"command": ["fuzzy-match", ".", "-name", "workflow"]},
            {"command": ["fuzzy-match", "src", "-name", "agent", "-type", "f"]},
        ],
    },
    "basedpyright-symbol": {
        "summary": "Find Python symbols by name across the worktree.",
        "when_to_use": (
            "Use when you know a class, function, or method name but not its file. "
            "Use basedpyright-structure instead when you already know the file."
        ),
        "parameters": {
            "query": "A symbol-name search string.",
            "limit": "Optional result limit from 1 through 200.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"query": "WorkflowAction"},
            {"query": "build_server", "limit": 10},
        ],
    },
    "basedpyright-structure": {
        "summary": "List symbols declared in one Python file.",
        "when_to_use": (
            "Use when you need a file's classes, functions, methods, and variables "
            "before deciding which lines to inspect or edit."
        ),
        "parameters": {
            "path": "A relative path to a Python file in the worktree.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {"path": "src/powdrr_lift/workflow_chat_agent.py"},
            {"path": "tests/test_workflow_chat_agent.py"},
        ],
    },
    "validate_edit": {
        "summary": "Validate a deferred edit without changing files.",
        "when_to_use": (
            "Use before applying an edit stored in a workflow handoff output."
        ),
        "parameters": {
            "edit": "The complete canonical edit action object.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {
                "edit": {
                    "action": "edit",
                    "file_path": "src/example.py",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "end_line": 1,
                            "text": "replacement",
                        }
                    ],
                }
            }
        ],
    },
    "apply_edit": {
        "summary": "Apply a previously validated deferred edit.",
        "when_to_use": "Use only after validate_edit returns returncode 0.",
        "parameters": {
            "edit": "The complete canonical edit action object.",
            "help": "Set true to request the tool's conventional --help guidance.",
        },
        "examples": [
            {
                "edit": {
                    "action": "edit",
                    "file_path": "src/example.py",
                    "edits": [
                        {
                            "kind": "replace",
                            "start_line": 1,
                            "end_line": 1,
                            "text": "replacement",
                        }
                    ],
                }
            }
        ],
    },
}


def builtin_tool_help(tool: str) -> dict[str, Any]:
    """Return structured progressive-discovery guidance for one builtin tool."""
    try:
        guidance = _HELP[tool]
    except KeyError as error:
        raise ValueError(f"Unknown builtin tool: {tool!r}") from error
    return {"tool": tool, "help": True, **guidance}
