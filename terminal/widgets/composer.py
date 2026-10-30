"""Compact multiline prompt composer with explicit Enter-key semantics."""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, TextArea


class PromptInput(TextArea):
    """Enter submits; modified Enter or Alt+N inserts a newline."""

    BINDINGS = [
        Binding("enter", "submit_prompt", "Send", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "New line", show=False, priority=True),
        Binding("alt+enter", "insert_newline", "New line", show=False, priority=True),
        Binding("alt+n", "insert_newline", "New line", show=False, priority=True),
    ]

    def action_submit_prompt(self) -> None:
        submit = getattr(self.app, "action_submit_prompt", None)
        if callable(submit):
            submit()

    def action_insert_newline(self) -> None:
        self.insert("\n")


class PromptComposer(Vertical):
    """Low-profile composer with a contextual Stop control."""

    DEFAULT_CSS = """
    PromptComposer {
        height: 7;
        min-height: 6;
        max-height: 10;
        width: 100%;
        padding: 0 1;
        background: #15181d;
        border-top: solid #2a2e34;
    }
    PromptComposer #composer-caption {
        height: 1;
        margin-top: 1;
        padding: 0 1;
        color: #858b94;
    }
    PromptComposer #prompt-input {
        height: 3;
        min-height: 2;
        max-height: 5;
        width: 100%;
        border: none;
        padding: 0 1;
        background: #15181d;
        color: #e0e2e4;
    }
    PromptComposer #composer-footer {
        height: 1;
        width: 100%;
        padding: 0 1;
        layout: horizontal;
    }
    PromptComposer #composer-hint {
        height: 1;
        width: 1fr;
        color: #747b84;
    }
    PromptComposer #newline-button {
        width: auto;
        min-width: 10;
        height: 1;
        min-height: 1;
        padding: 0 1;
        margin: 0;
        border: none;
        background: transparent;
        color: #8d95a0;
    }
    PromptComposer #newline-button:hover {
        background: #292f36;
        color: #d0d4d8;
    }
    PromptComposer #stop-button {
        display: none;
        width: auto;
        min-width: 8;
        height: 1;
        min-height: 1;
        padding: 0 1;
        margin: 0;
        border: none;
        background: transparent;
        color: #c39a71;
        text-style: bold;
    }
    PromptComposer #stop-button:hover {
        background: #29251f;
        color: #e1bd8e;
    }
    """

    def compose(self):
        yield Static(
            "MESSAGE  ·  Enter: send  ·  Shift+Enter: new line",
            id="composer-caption",
            markup=False,
        )
        yield PromptInput(
            "",
            id="prompt-input",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            placeholder="Ask RelayPilot…",
        )
        with Horizontal(id="composer-footer"):
            yield Static("Ready", id="composer-hint", markup=False)
            yield Button("New line", id="newline-button")
            yield Button("Stop · Esc", id="stop-button", variant="warning")

    def set_active(self, active: bool) -> None:
        hint = self.query_one("#composer-hint", Static)
        stop = self.query_one("#stop-button", Button)
        stop.display = active
        if active:
            hint.update("Request active · Escape to cancel")
        else:
            hint.update("Enter: send  ·  Shift+Enter: newline  ·  Alt+N: fallback")
