from __future__ import annotations

from collections.abc import Callable
from math import ceil
from queue import Queue
from threading import Event, Thread
from typing import Any, TextIO, cast

from textual.app import App, ComposeResult
from textual.events import Key
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static, TextArea

from powdrr_lift.workflow_chat_agent import (
    SkillCatalogEntry,
    WorkflowChatConfig,
    run_workflow_chat,
)


class _TextualOutput:
    """Small TextIO adapter that turns workflow output into screen updates."""

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
            self._app._output_line(self._channel, line.rstrip("\r"))
        if self._buffer and self._channel == "stdout":
            self._app._output_prompt(self._buffer)
        return len(text)

    def flush(self) -> None:
        return

    def isatty(self) -> bool:
        return False


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
        await super()._on_key(event)


class WorkflowChatApp(App[None]):
    BINDINGS = [("ctrl+q", "quit_workflow", "Quit")]

    CSS = """
    Screen {
        layout: vertical;
    }
    #steps {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }
    #status {
        height: 3;
        border: round $success;
        padding: 1;
    }
    #message {
        height: auto;
        min-height: 3;
        max-height: 12;
        overflow-y: auto;
        padding: 1;
    }
    #response {
        height: 3;
        min-height: 3;
        max-height: 12;
        margin: 0 1;
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
        self._response: TextArea | None = None
        self._stop_requested = Event()
        self._request_submitted = Event()
        self._workflow_active = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ListView(id="steps")
        yield Static("Status: waiting for your request...", id="status")
        yield Static("Enter your request below.", id="message")
        yield _WorkflowResponseTextArea(
            placeholder="Press Return to submit; multiline text is supported",
            id="response",
            submit_callback=self._submit_response,
        )
        yield Footer()

    def on_mount(self) -> None:
        self._response = self.query_one("#response", TextArea)
        self._response.focus()
        Thread(target=self._run_workflow, daemon=True).start()

    def action_quit_workflow(self) -> None:
        self._stop_requested.set()
        self._request_submitted.set()
        self._answers.put("")
        self.exit()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "response":
            return
        line_count = event.text_area.text.count("\n") + 1
        event.text_area.styles.height = min(max(3, line_count + 2), 12)

    def _submit_response(self) -> None:
        if self._response is None or self._response.disabled:
            return
        self._answers.put(self._response.text.strip())
        self._request_submitted.set()
        self._response.text = ""
        self._response.disabled = True
        self.query_one("#status", Static).update("Status: thinking...")

    def _run_workflow(self) -> None:
        first_workflow = True
        while not self._stop_requested.is_set():
            if not first_workflow:
                self._request_submitted.wait()
                self._request_submitted.clear()
                if self._stop_requested.is_set():
                    return
            stdout = _TextualOutput(self, channel="stdout")
            stderr = _TextualOutput(self, channel="stderr")
            self._failure_message = None
            self._exit_code = 1
            self._workflow_active = True
            self.call_from_thread(
                self._set_status,
                "waiting for your request..."
                if first_workflow
                else "waiting on LLM response...",
            )
            try:
                self._exit_code = run_workflow_chat(
                    self._config,
                    input_func=self._next_answer,
                    stdout=cast(TextIO, stdout),
                    stderr=cast(TextIO, stderr),
                    progress_callback=self._progress_update,
                )
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self._failure_message = str(exc)
                self.call_from_thread(self._set_failure, str(exc))
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
    ) -> None:
        self.call_from_thread(
            self._apply_progress,
            skill,
            current_step_index,
            status,
        )

    def _apply_progress(
        self,
        skill: SkillCatalogEntry,
        current_step_index: int,
        status: str,
    ) -> None:
        steps = self.query_one("#steps", ListView)
        items = list(steps.query(ListItem))
        if len(steps.children) != len(skill.skill.steps):
            steps.clear()
            items = [
                ListItem(Label(f"{step_index + 1}. {step.description}"))
                for step_index, step in enumerate(skill.skill.steps)
            ]
            steps.mount(*items)
        for step_index, item in enumerate(items):
            item.remove_class("completed", "current")
            if step_index < current_step_index:
                item.add_class("completed")
            elif step_index == current_step_index:
                item.add_class("current")
        self.query_one("#status", Static).update(f"Status: {status}")
        if self._response is not None:
            self._response.disabled = False
            self._response.focus()

    def _output_prompt(self, prompt: str) -> None:
        self.call_from_thread(self._show_prompt, prompt)

    def _show_prompt(self, prompt: str) -> None:
        if not self._workflow_active:
            return
        self._update_message(prompt)
        if self._response is not None:
            self._response.disabled = False
            self._response.focus()

    def _output_line(self, channel: str, line: str) -> None:
        if channel == "stderr":
            if line.startswith("[workflow] "):
                self.call_from_thread(
                    self._set_status,
                    line.removeprefix("[workflow] "),
                )
            elif any(word in line.lower() for word in ("error", "failed", "stopping")):
                self.call_from_thread(self._set_message, line)
        elif line:
            self.call_from_thread(self._set_message, line)

    def _set_status(self, status: str) -> None:
        self.query_one("#status", Static).update(f"Status: {status}")

    def _set_message(self, message: str) -> None:
        self._update_message(message)

    def _update_message(self, message: str) -> None:
        message_widget = self.query_one("#message", Static)
        message_widget.update(message)
        available_width = message_widget.size.width - 4
        if available_width <= 0:
            available_width = 80
        line_count = sum(
            max(1, ceil(len(line) / available_width))
            for line in (message.splitlines() or [""])
        )
        message_widget.styles.height = min(max(3, line_count + 2), 12)

    def _set_failure(self, message: str) -> None:
        self.query_one("#status", Static).update(f"Status: failed — {message}")

    def _finish(self) -> None:
        status = "workflow complete" if self._exit_code == 0 else "workflow stopped"
        self.query_one("#status", Static).update(f"Status: {status}")
        if self._exit_code == 0:
            self._update_message("Workflow complete. What would you like to do next?")
            if self._response is not None:
                self._response.disabled = False
                self._response.focus()
        else:
            self._update_message(
                "Workflow error: "
                f"{self._failure_message or 'unknown error'}. Press Ctrl+C to exit."
            )

    def on_unmount(self) -> None:
        self._stop_requested.set()
        self._request_submitted.set()
        self._answers.put("")


def run_workflow_chat_tui(config: WorkflowChatConfig) -> int:
    app = WorkflowChatApp(config)
    app.run()
    return app._exit_code
