from __future__ import annotations

from queue import Queue
from typing import TextIO, cast

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

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


class WorkflowChatApp(App[None]):
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
        height: 3;
        padding: 1;
    }
    #response {
        height: 3;
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
        self._response: Input | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ListView(id="steps")
        yield Static("Status: starting...", id="status")
        yield Static("", id="message")
        yield Input(placeholder="Press Return to submit", id="response")
        yield Footer()

    def on_mount(self) -> None:
        self._response = self.query_one("#response", Input)
        self._response.focus()
        self.run_worker(self._run_workflow, thread=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._answers.put(event.value)
        event.input.value = ""
        event.input.disabled = True
        self.query_one("#status", Static).update("Status: thinking...")

    def _run_workflow(self) -> None:
        stdout = _TextualOutput(self, channel="stdout")
        stderr = _TextualOutput(self, channel="stderr")
        try:
            self._exit_code = run_workflow_chat(
                self._config,
                input_func=self._next_answer,
                stdout=cast(TextIO, stdout),
                stderr=cast(TextIO, stderr),
                progress_callback=self._progress_update,
            )
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.call_from_thread(self._set_failure, str(exc))
            self._exit_code = 1
        self.call_from_thread(self._finish)

    def _next_answer(self) -> str:
        return self._answers.get()

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
        self.query_one("#message", Static).update(prompt)
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
        self.query_one("#message", Static).update(message)

    def _set_failure(self, message: str) -> None:
        self.query_one("#status", Static).update(f"Status: failed — {message}")

    def _finish(self) -> None:
        status = "workflow complete" if self._exit_code == 0 else "workflow stopped"
        self.query_one("#status", Static).update(f"Status: {status}")
        self.exit()

    def on_unmount(self) -> None:
        self._answers.put("")


def run_workflow_chat_tui(config: WorkflowChatConfig) -> int:
    app = WorkflowChatApp(config)
    app.run()
    return app._exit_code
