"""Terminal interaction and diagnostic output for workflow chat runtimes."""

from __future__ import annotations

import json
import select
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

try:
    import readline
    import termios
    import tty
except ImportError:  # pragma: no cover - only used on non-POSIX platforms
    readline = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


def _verbose_print(stderr: TextIO, verbose: bool, message: str) -> None:
    if verbose:
        print(f"[verbose] {message}", file=stderr)


def _write_agent_error(repo_root: Path, message: str) -> None:
    """Persist the latest failure context without hiding the original error."""
    try:
        (repo_root / "agent_error.txt").write_text(
            message.rstrip() + "\n", encoding="utf-8"
        )
    except OSError:
        return


def _verbose_json(
    stderr: TextIO,
    verbose: bool,
    label: str,
    value: object,
) -> None:
    if not verbose:
        return
    serialized = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    for line_number, line in enumerate(serialized.splitlines()):
        prefix = f"{label}: " if line_number == 0 else "  "
        _verbose_print(stderr, verbose, f"{prefix}{line}")


def _prompt_user(
    prompt: str,
    *,
    input_func: Callable[[], str],
    stdout: TextIO,
    status_stream: TextIO | None = None,
) -> str:
    if input_func is input and _supports_readline_input(stdout):
        answer = input(prompt).strip()
    else:
        stdout.write(prompt)
        stdout.flush()
        if input_func is input and _supports_interactive_line_editing(stdout):
            answer = _read_interactive_line(prompt, stdout=stdout).strip()
        else:
            answer = input_func().strip()
        if input_func is not input:
            stdout.write("\n")
            stdout.flush()
    if status_stream is not None:
        print("[workflow] calling LLM...", file=status_stream, flush=True)
    return answer


def _supports_readline_input(stdout: TextIO) -> bool:
    """Return whether native readline can safely own this terminal prompt."""
    return sys.stdin.isatty() and stdout.isatty() and readline is not None


def _supports_interactive_line_editing(stdout: TextIO) -> bool:
    """Return whether the process has a terminal we can safely edit in place."""
    return sys.stdin.isatty() and stdout.isatty() and hasattr(termios, "tcgetattr")


def _read_interactive_line(prompt: str, *, stdout: TextIO) -> str:
    """Read a line with cursor movement support when readline is unavailable."""
    stdin = sys.stdin
    chars: list[str] = []
    cursor = 0
    original_attributes = termios.tcgetattr(stdin.fileno())

    def redraw() -> None:
        # Repaint the line and position the cursor after the edited prefix.
        stdout.write("\r" + prompt + "".join(chars) + "\x1b[K")
        distance_from_end = len(chars) - cursor
        if distance_from_end:
            stdout.write(f"\x1b[{distance_from_end}D")
        stdout.flush()

    try:
        tty.setraw(stdin.fileno())
        while True:
            character = stdin.read(1)
            if character in {"\r", "\n"}:
                stdout.write("\r\n")
                stdout.flush()
                return "".join(chars)
            if character == "\x03":
                raise KeyboardInterrupt
            if character == "\x04":
                if not chars:
                    raise EOFError
                continue
            if character in {"\x7f", "\b"}:
                if cursor:
                    del chars[cursor - 1]
                    cursor -= 1
                    redraw()
                continue
            if character != "\x1b":
                chars[cursor:cursor] = [character]
                cursor += 1
                stdout.write(character)
                stdout.flush()
                continue

            # Arrow and editing keys arrive as ANSI escape sequences. Read the
            # rest only when it is immediately available so a lone Escape can
            # still be entered as a normal control key without hanging.
            sequence = ""
            while select.select([stdin], [], [], 0.01)[0]:
                sequence += stdin.read(1)
                if sequence[-1:] in "~ABCDEFGH":
                    break
            if sequence in {"[D", "OD"}:
                cursor = max(0, cursor - 1)
            elif sequence in {"[C", "OC"}:
                cursor = min(len(chars), cursor + 1)
            elif sequence in {"[H", "OH", "[1~"}:
                cursor = 0
            elif sequence in {"[F", "OF", "[4~"}:
                cursor = len(chars)
            elif sequence == "[3~" and cursor < len(chars):
                del chars[cursor]
            if sequence:
                redraw()
    finally:
        termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, original_attributes)
