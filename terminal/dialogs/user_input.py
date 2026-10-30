"""Non-blocking terminal replacement for legacy upload-recovery input()."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class UserInputScreen(ModalScreen[str]):
    BINDINGS = []

    DEFAULT_CSS = """
    UserInputScreen { align: center middle; background: #000000 65%; }
    UserInputScreen > Vertical {
        width: 84%; height: 78%; max-height: 90%; padding: 1 2;
        border: round #424a54; background: #171b20;
    }
    UserInputScreen #input-details {
        height: 1fr; min-height: 5; margin-bottom: 1;
        background: #171b20; scrollbar-color: #424a54;
    }
    UserInputScreen #input-prompt { height: auto; color: #dfe1e3; padding: 0 1; }
    UserInputScreen #user-input { margin-bottom: 1; }
    UserInputScreen Horizontal { height: 3; align-horizontal: right; }
    UserInputScreen Button { margin-left: 1; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="input-details"):
                yield Static(self.prompt, id="input-prompt", markup=False)
            yield Input(placeholder="Enter a choice…", id="user-input")
            with Horizontal():
                yield Button("Cancel", id="cancel-input")
                yield Button("Continue", id="submit-input", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or "0")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit-input":
            value = self.query_one(Input).value.strip()
            self.dismiss(value or "0")
        elif event.button.id == "cancel-input":
            self.dismiss("0")

