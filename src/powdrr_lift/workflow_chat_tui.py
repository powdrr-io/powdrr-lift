from __future__ import annotations

import subprocess
import sys
import traceback
from collections.abc import Callable
from math import ceil
from queue import Queue
from threading import Event, Thread
from typing import Any, TextIO, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.events import Key, Resize
from textual.widgets import Label, ListItem, ListView, TextArea

from powdrr_lift.workflow_chat_agent import (
    SkillCatalogEntry,
    WorkflowChatConfig,
    run_workflow_chat,
)

_EMPTY_HUMAN_INPUT_WARNING = "WARNING: received empty response but need human input"
_USER_RESPONSE_SEPARATOR = "-" * 40
_POWDRR_AGENT_BANNER = "Powdrr Agent v0.0.1"
_MAX_STATUS_HISTORY_ENTRIES = 80
_MAX_STATUS_HISTORY_CHARS = 24_000
_MAX_STATUS_MESSAGE_CHARS = 8_000


class _TextualOutput:
    """TextIO adapter that turns line-oriented output into screen updates."""

    def __init__(self, app: WorkflowChatApp, *, channel: str) -> None:
        self._app = app
        self._channel = channel
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            self._app._output_line(self._channel, line)
        return len(text)

    def flush(self) -> None:
        return

    def isatty(self) -> bool:
        return False


class _TextualStdoutOutput(_TextualOutput):
    """Stdout adapter that preserves presentation boundaries for the TUI."""

    def __init__(self, app: WorkflowChatApp) -> None:
        super().__init__(app, channel="stdout")
        self._pending_lines: list[str] = []
        self._initial_prompt_pending = True

    def _flush_pending(self, *, question: bool) -> None:
        if not self._pending_lines:
            return
        presentation = "\n".join(self._pending_lines)
        self._pending_lines = []
        if question:
            self._app._output_question(presentation)
        else:
            self._app._output_line("stdout", presentation)

    def write(self, text: str) -> int:
        if not text:
            return 0
        if not self._buffer:
            if text.strip() == ">":
                self._flush_pending(question=True)
            elif self._pending_lines:
                self._flush_pending(question=False)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            # _prompt_user writes a newline after the submitted answer when
            # driven by the TUI's queue. It is transport echo, not an empty
            # LLM question; retaining it makes the next `> ` delimiter look
            # like a real empty question.
            if line or self._pending_lines:
                self._pending_lines.append(line)
        if self._buffer:
            prompt = self._buffer
            if prompt.strip() == ">":
                self._flush_pending(question=True)
                # `> ` is only the input delimiter. A question, including an
                # intentionally empty one, is delivered by _flush_pending.
                # Do not interpret a bare delimiter as an empty LLM response.
                self._buffer = ""
            elif self._initial_prompt_pending:
                self._initial_prompt_pending = False
                self._app._output_initial_prompt(prompt)
                self._buffer = ""
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            prompt = self._buffer
            self._buffer = ""
            self._app._output_question(prompt)
        self._flush_pending(question=True)


class _WorkflowResponseTextArea(TextArea):
    def __init__(
        self,
        *,
        submit_callback: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._submit_callback = submit_callback

    async def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self._submit_callback()
            return
        if event.key in {"ctrl+c", "meta+c", "super+c"}:
            event.stop()
            event.prevent_default()
            cast(WorkflowChatApp, self.app).action_copy_selection()
            return
        if event.key in {"ctrl+x", "meta+x", "super+x"}:
            event.stop()
            event.prevent_default()
            cast(WorkflowChatApp, self.app).action_cut_selection()
            return
        if event.key in {"ctrl+v", "meta+v", "super+v"}:
            event.stop()
            event.prevent_default()
            cast(WorkflowChatApp, self.app).action_paste_selection()
            return
        await super()._on_key(event)

    def paste_text(self, text: str) -> None:
        """Replace the selection with text while preserving TextArea editing."""
        edit_result = self._replace_via_keyboard(text, *self.selection)
        if edit_result is not None:
            self.move_cursor(edit_result.end_location)


class WorkflowChatApp(App[None]):
    BINDINGS = [
        ("ctrl+q", "quit_workflow", "Quit"),
        ("ctrl+c", "copy_selection", "Copy"),
        ("super+c", "copy_selection", "Copy"),
        ("ctrl+x", "cut_selection", "Cut"),
        ("super+x", "cut_selection", "Cut"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #steps {
        width: 100%;
        height: auto;
        min-height: 0;
        display: none;
        border: round $warning;
        padding: 0 1;
    }
    #steps.has-content {
        display: block;
    }
    #status-container {
        width: 100%;
        height: 1fr;
        min-height: 0;
        border: round $success;
        padding: 0 1;
        overflow-y: scroll;
    }
    #status {
        width: 100%;
    }
    #response {
        width: 100%;
        height: auto;
        min-height: 3;
        border: round $primary;
        margin: 0;
    }
    .completed {
        color: $success;
    }
    .current {
        color: $accent;
        text-style: bold;
    }
    """

    def __init__(self, config: WorkflowChatConfig) -> None:
        super().__init__()
        self._config = config
        self._answers: Queue[str] = Queue()
        self._exit_code = 1
        self._failure_message: str | None = None
        self._failure_traceback: str | None = None
        self._recent_output: list[str] = []
        self._response: TextArea | None = None
        self._stop_requested = Event()
        self._request_submitted = Event()
        self._workflow_active = False
        self._message_history: list[str] = []
        self._message_history_chars = 0
        self._current_status = _POWDRR_AGENT_BANNER
        self._initial_prompt_visible = False
        self._active_skill_path: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        yield ScrollableContainer(
            Label(self._status_text(_POWDRR_AGENT_BANNER), markup=False, id="status"),
            id="status-container",
        )
        yield ListView(id="steps")
        yield _WorkflowResponseTextArea(
            placeholder="Press Return to submit; multiline text is supported",
            id="response",
            submit_callback=self._submit_response,
        )

    def on_mount(self) -> None:
        self._response = self.query_one("#response", TextArea)
        self.query_one("#status-container", ScrollableContainer).can_focus = True
        self.query_one("#steps", ListView).can_focus = True
        # Paint the initial state before starting any repository or LLM work.
        # The worker can block during setup, so this must not be the first
        # operation that establishes visible state.
        self._set_status(_POWDRR_AGENT_BANNER)
        self._response.focus()
        Thread(target=self._run_workflow, daemon=True).start()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy through Textual and the native macOS clipboard when available.

        Textual's terminal driver uses OSC 52. macOS Terminal does not consume
        OSC 52, which made copy appear to work in tests (the in-process buffer
        was populated) while nothing reached the user's clipboard.
        """
        super().copy_to_clipboard(text)
        if sys.platform != "darwin":
            return
        try:
            subprocess.run(
                ["pbcopy"],
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            # OSC 52 / Textual's in-process clipboard remains available as a
            # fallback if pbcopy is unavailable.
            return

    def action_quit_workflow(self) -> None:
        self._stop_requested.set()
        self._request_submitted.set()
        self._answers.put("")
        self.exit()

    def action_copy_selection(self) -> None:
        text_area = self._selected_text_area()
        if text_area is not None:
            text_area.action_copy()
            return
        if text := self._focused_box_text():
            self.copy_to_clipboard(text)

    def action_cut_selection(self) -> None:
        text_area = self._selected_text_area()
        if text_area is not None:
            text_area.action_cut()
            return
        # The green and orange panels are read-only, so cut has the useful
        # clipboard portion of the operation without deleting workflow output.
        self.action_copy_selection()

    def action_paste_selection(self) -> None:
        if isinstance(self.focused, _WorkflowResponseTextArea):
            self.focused.paste_text(self._read_clipboard())

    async def on_key(self, event: Key) -> None:
        if event.key in {"ctrl+c", "meta+c", "super+c"}:
            event.stop()
            event.prevent_default()
            self.action_copy_selection()
        elif event.key in {"ctrl+x", "meta+x", "super+x"}:
            event.stop()
            event.prevent_default()
            self.action_cut_selection()
        elif event.key in {"ctrl+v", "meta+v", "super+v"}:
            event.stop()
            event.prevent_default()
            self.action_paste_selection()

    def _focused_box_text(self) -> str | None:
        status_container = self.query_one("#status-container", ScrollableContainer)
        if status_container.has_focus_within:
            return self._status_content()
        steps = self.query_one("#steps", ListView)
        if steps.has_focus_within:
            labels = [item.query_one(Label) for item in steps.query(ListItem)]
            return "\n".join(str(label.render()) for label in labels)
        return None

    def _read_clipboard(self) -> str:
        if sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["pbpaste"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            else:
                return result.stdout
        return self.clipboard

    def _selected_text_area(self) -> TextArea | None:
        candidates = [self.focused, self._response]
        for candidate in candidates:
            if isinstance(candidate, TextArea) and candidate.selected_text:
                return candidate
        return None

    @on(TextArea.Changed, "#response")
    def _on_response_changed(self, event: TextArea.Changed) -> None:
        self._resize_text_area(event.text_area)

    @on(Resize)
    def _on_response_resized(self, event: Resize) -> None:
        _ = event
        if self._response is not None:
            self._resize_text_area(self._response)

    @staticmethod
    def _resize_text_area(text_area: TextArea) -> None:
        """Keep entered text visible, including wrapped long lines."""
        width = max(text_area.size.width - 4, 20)
        line_count = sum(
            max(1, ceil(len(line) / width))
            for line in (text_area.text.splitlines() or [""])
        )
        text_area.styles.height = max(3, line_count + 2)

    def _submit_response(self) -> None:
        if self._response is None or self._response.disabled:
            return
        answer = self._response.text.strip()
        if not answer:
            return
        if self._initial_prompt_visible:
            self._initial_prompt_visible = False
        self._set_message(self._format_user_response(answer))
        self._response.text = ""
        self._response.disabled = True
        # Let Textual paint the echoed response before changing the status or
        # waking the worker. This makes Return visibly acknowledge the input
        # even when the next workflow operation is slow to start.
        self.call_after_refresh(self._begin_submitted_response, answer)

    def _begin_submitted_response(self, answer: str) -> None:
        self._set_status("calling LLM...")
        self._release_submitted_response(answer)

    def _release_submitted_response(self, answer: str) -> None:
        if self._stop_requested.is_set():
            return
        self._answers.put(answer)
        self._request_submitted.set()

    @staticmethod
    def _format_user_response(answer: str) -> str:
        response = answer or "<empty response>"
        lines = response.splitlines() or [""]
        echoed_response = "\n".join(f"> {line}" for line in lines)
        return (
            f"{_USER_RESPONSE_SEPARATOR}\n{echoed_response}\n{_USER_RESPONSE_SEPARATOR}"
        )

    def _run_workflow(self) -> None:
        first_workflow = True
        while not self._stop_requested.is_set():
            if not first_workflow:
                self._request_submitted.wait()
                self._request_submitted.clear()
                if self._stop_requested.is_set():
                    return
            stdout = _TextualStdoutOutput(self)
            stderr = _TextualOutput(self, channel="stderr")
            self._failure_message = None
            self._failure_traceback = None
            self._recent_output.clear()
            self._exit_code = 1
            self._workflow_active = True
            try:
                self._exit_code = run_workflow_chat(
                    self._config,
                    input_func=self._next_answer,
                    stdout=cast(TextIO, stdout),
                    stderr=cast(TextIO, stderr),
                    progress_callback=self._progress_update,
                )
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                message = str(exc).strip() or "<exception had no message>"
                self._failure_message = f"{type(exc).__name__}: {message}"
                self._failure_traceback = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).strip()
                self._record_output("exception", self._failure_message)
                self.call_from_thread(self._set_failure, self._failure_message)
                self._exit_code = 1
            self._workflow_active = False
            if self._stop_requested.is_set():
                return
            self.call_from_thread(self._finish)
            if self._exit_code != 0:
                return
            first_workflow = False

    def _next_answer(self) -> str:
        answer = self._answers.get()
        self._request_submitted.clear()
        return answer

    def _progress_update(
        self,
        skill: SkillCatalogEntry,
        current_step_index: int,
        status: str,
        parent_skill: SkillCatalogEntry | None = None,
        parent_step_index: int | None = None,
    ) -> None:
        self._record_output("progress", status)
        self.call_from_thread(
            self._apply_progress,
            skill,
            current_step_index,
            status,
            parent_skill,
            parent_step_index,
        )

    def _apply_progress(
        self,
        skill: SkillCatalogEntry,
        current_step_index: int,
        status: str,
        parent_skill: SkillCatalogEntry | None = None,
        parent_step_index: int | None = None,
    ) -> None:
        steps = self.query_one("#steps", ListView)
        if current_step_index >= len(skill.skill.steps):
            steps.clear()
            steps.set_class(False, "has-content")
            steps.border_title = ""
            self._active_skill_path = ()
            self._set_status(status)
            if self._response is not None:
                self._response.disabled = False
                self._response.focus()
            return
        nested_parent_skill = parent_skill
        nested_parent_step_index = parent_step_index
        nested = (
            nested_parent_skill is not None and nested_parent_step_index is not None
        )
        skill_path = self._resolve_skill_path(skill, parent_skill)
        steps.border_title = " > ".join(skill_path)
        expected_item_count = len(skill.skill.steps) + (2 if nested else 0)
        items = list(steps.query(ListItem))
        if len(steps.children) != expected_item_count:
            steps.clear()
            if nested:
                assert nested_parent_skill is not None
                assert nested_parent_step_index is not None
                parent_step = nested_parent_skill.skill.steps[nested_parent_step_index]
                steps.mount(
                    ListItem(
                        Label(
                            f"{nested_parent_step_index + 1}. {parent_step.description}"
                        )
                    ),
                    ListItem(Label("-------")),
                )
            items = [
                ListItem(Label(f"{step_index + 1}. {step.description}"))
                for step_index, step in enumerate(skill.skill.steps)
            ]
            steps.mount(*items)
        items = list(steps.query(ListItem))
        if nested:
            items = items[2:]
        steps.set_class(bool(items), "has-content")
        for step_index, item in enumerate(items):
            item.remove_class("completed", "current")
            if step_index < current_step_index:
                item.add_class("completed")
            elif step_index == current_step_index:
                item.add_class("current")
        self._set_status(status)
        if self._response is not None:
            self._response.disabled = False
            self._response.focus()

    def _resolve_skill_path(
        self,
        skill: SkillCatalogEntry,
        parent_skill: SkillCatalogEntry | None,
    ) -> tuple[str, ...]:
        skill_name = skill.skill.name
        if skill_name in self._active_skill_path:
            path = self._active_skill_path[
                : self._active_skill_path.index(skill_name) + 1
            ]
        elif parent_skill is not None:
            parent_name = parent_skill.skill.name
            if parent_name in self._active_skill_path:
                path = self._active_skill_path[
                    : self._active_skill_path.index(parent_name) + 1
                ] + (skill_name,)
            else:
                path = (parent_name, skill_name)
        else:
            path = (skill_name,)
        self._active_skill_path = path
        return path

    def _output_prompt(self, prompt: str) -> None:
        self.call_from_thread(self._show_prompt, prompt)

    def _output_initial_prompt(self, prompt: str) -> None:
        self.call_from_thread(self._show_initial_prompt, prompt)

    def _output_question(self, question: str) -> None:
        self.call_from_thread(self._show_prompt, question)

    def _show_prompt(self, prompt: str) -> None:
        if not self._workflow_active:
            return
        prompt = prompt.strip()
        if prompt == ">":
            # The marker is only a delimiter. Empty-question detection happens
            # when _output_question receives an empty presentation.
            pass
        else:
            self._set_message(prompt or _EMPTY_HUMAN_INPUT_WARNING)
        if self._response is not None:
            self._response.disabled = False
            self._response.focus()

    def _show_initial_prompt(self, prompt: str) -> None:
        if not self._workflow_active:
            return
        self._initial_prompt_visible = True
        self._set_message(prompt.strip())
        if self._response is not None:
            self._response.disabled = False
            self._response.focus()

    def _output_line(self, channel: str, line: str) -> None:
        self._record_output(channel, line)
        if channel == "stderr":
            if line.startswith("[workflow] "):
                self.call_from_thread(
                    self._set_status,
                    line.removeprefix("[workflow] "),
                )
            elif line.startswith("waiting for ") and line.endswith(" LLM response..."):
                # Model waits are emitted directly by the provider loop during
                # selection and repair, before execution progress is updated.
                # Surface them so the UI cannot remain on a stale "thinking..."
                # label after a local tool such as git add completes.
                self.call_from_thread(self._set_status, line)
            elif line.startswith("WARNING:"):
                self.call_from_thread(self._set_status, line)
            elif any(
                word in line.lower()
                for word in ("error", "failed", "stopping", "yaml", "parser")
            ):
                self.call_from_thread(self._set_message, line)
        elif channel == "stdout":
            self.call_from_thread(self._set_message, line)

    def _record_output(self, channel: str, line: str) -> None:
        normalized_line = line.strip()
        if not normalized_line:
            return
        self._recent_output.append(f"{channel}: {normalized_line}")
        del self._recent_output[:-40]

    def _set_status(self, status: str) -> None:
        # Status updates are history, too.  Keeping the latest value in a
        # separate transient slot caused diagnostics (including the full
        # empty-response prompt) to disappear as soon as the next status was
        # emitted.
        self._set_message(status)

    def _render_status(self) -> None:
        status_container = self.query_one("#status-container", ScrollableContainer)
        status_widget = self.query_one("#status", Label)
        content = self._status_content()
        status_widget.update(self._status_text(content))
        status_container.scroll_end(animate=False)
        status_container.call_after_refresh(status_container.scroll_end, animate=False)
        status_widget.refresh(repaint=True)

    def _status_content(self) -> str:
        if self._message_history:
            content = "\n\n".join(self._message_history)
            if self._message_history[-1] != self._current_status:
                content += f"\n\n{self._current_status}"
        else:
            content = self._current_status
        return content

    @staticmethod
    def _status_text(content: str) -> Text:
        return Text(content, no_wrap=False, overflow="fold")

    def _set_message(self, message: str) -> None:
        message = message.strip()
        if not message:
            return
        if message == self._current_status and self._message_history:
            return
        if len(message) > _MAX_STATUS_MESSAGE_CHARS:
            half_limit = _MAX_STATUS_MESSAGE_CHARS // 2
            message = (
                message[:half_limit]
                + "\n... [status message truncated] ...\n"
                + message[-half_limit:]
            )
        if not self._message_history or self._message_history[-1] != message:
            self._message_history.append(message)
            self._message_history_chars += len(message)
        while (
            len(self._message_history) > _MAX_STATUS_HISTORY_ENTRIES
            or self._message_history_chars > _MAX_STATUS_HISTORY_CHARS
        ):
            removed_message = self._message_history.pop(0)
            self._message_history_chars -= len(removed_message)
        self._current_status = message
        self._render_status()

    def _set_failure(self, message: str) -> None:
        self._set_status(f"failed — {message}")

    def _finish(self) -> None:
        if self._exit_code == 0:
            # The agent emits the skill-specific completion status through the
            # progress callback; preserve that wording in the final UI state.
            self._set_status(self._current_status)
            if self._response is not None:
                self._response.disabled = False
                self._response.focus()
        else:
            self._set_status("workflow stopped")
            failure_message = self._failure_message
            if failure_message is None:
                failure_message = (
                    f"workflow exited with status {self._exit_code} without a "
                    "reported exception"
                )
            failure_details = [failure_message]
            if self._failure_traceback:
                traceback_lines = self._failure_traceback.splitlines()
                failure_details.append(
                    "Traceback (most recent call last):\n"
                    + "\n".join(traceback_lines[-8:])
                )
            if self._recent_output:
                failure_details.append(
                    "Recent workflow context:\n" + "\n".join(self._recent_output[-12:])
                )
            self._set_message(
                "Workflow error:\n"
                + "\n".join(failure_details)
                + "\nPress Ctrl+C to exit."
            )

    def on_unmount(self) -> None:
        self._stop_requested.set()
        self._request_submitted.set()
        self._answers.put("")


def run_workflow_chat_tui(config: WorkflowChatConfig) -> int:
    app = WorkflowChatApp(config)
    app.run()
    return app._exit_code
