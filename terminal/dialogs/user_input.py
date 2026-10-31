"""Non-blocking terminal input and single-screen attachment recovery choices.

Notebook/other prompts keep an ordinary text field. The first upload failure
menu uses a scrollable set of choices with an explicit UNKNOWN cleanup check.
"""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, OptionList, Static
from textual.widgets.option_list import Option

from terminal.dialogs.recovery_choices import (
    RecoveryPrompt,
    encode_recovery_choice,
    parse_recovery_prompt,
)


class UserInputScreen(ModalScreen[str]):
    """One accessible compact dialog; never sends an upload by itself."""

    DEFAULT_CSS = """
    UserInputScreen {
        align: center middle;
        background: #000000 68%;
    }
    UserInputScreen > Vertical {
        width: 78%;
        height: 72%;
        max-height: 90%;
        padding: 1 2;
        border: round #424a54;
        background: #171b20;
    }
    UserInputScreen.recovery-dialog > Vertical {
        height: 26;
        max-height: 88%;
    }
    UserInputScreen #input-details {
        width: 100%;
        height: 1fr;
        min-height: 4;
        margin-bottom: 1;
        background: #171b20;
        scrollbar-color: #424a54;
    }
    UserInputScreen #input-prompt {
        height: auto;
        width: 100%;
        color: #cbd2da;
        padding: 0 1;
    }
    UserInputScreen #recovery-choices {
        width: 100%;
        height: 5;
        min-height: 3;
        border: none;
        background: #171b20;
        color: #d6dbe0;
        margin-bottom: 1;
    }
    UserInputScreen #recovery-choices > .option-list--option-highlighted {
        color: #ecf1f4;
        background: #343d47;
    }
    UserInputScreen #cleanup-check {
        height: 3;
        width: 100%;
        padding: 0 1;
        color: #e2c89d;
        background: #171b20;
    }
    UserInputScreen #user-input {
        width: 100%;
        min-width: 0;
        height: 3;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round #424a54;
        background: #20252b;
        color: #e1e4e8;
    }
    UserInputScreen #dialog-actions {
        width: 100%;
        height: 3;
        align-horizontal: right;
    }
    UserInputScreen #dialog-actions Button {
        height: 3;
        width: 16;
        min-width: 12;
        max-width: 18;
        margin: 0 0 0 1;
        padding: 0 1;
        border: none;
        background: #2b3139;
        color: #d9dfe5;
    }
    UserInputScreen #dialog-actions #submit-input {
        background: #344d49;
        color: #e2eee9;
    }
    UserInputScreen #dialog-actions Button:hover {
        background: #49535e;
        color: #ffffff;
    }
    UserInputScreen #dialog-actions #submit-input:disabled {
        background: #252b31;
        color: #77818b;
    }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt
        self.recovery: RecoveryPrompt | None = parse_recovery_prompt(prompt)
        self._choice_number: str | None = None
        if self.recovery is not None:
            self.add_class("recovery-dialog")
            if self.recovery.choices:
                # Default to Cancel, not Retry: no accidental duplicate uploads.
                self._choice_number = self.recovery.choices[-1].number

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="input-details"):
                yield Static(
                    self.recovery.explanation if self.recovery else self.prompt,
                    id="input-prompt",
                    markup=False,
                )
            if self.recovery is not None:
                yield OptionList(
                    *[
                        Option(Text(f"{choice.number}. {choice.label}"), id=f"option-{choice.number}")
                        for choice in self.recovery.choices
                    ],
                    id="recovery-choices",
                )
                if self.recovery.has_uncertain_upload and any(c.needs_cleanup for c in self.recovery.choices):
                    yield Checkbox(
                        "I checked Chrome and removed staged copies of the unresolved files",
                        id="cleanup-check",
                    )
            else:
                yield Input(placeholder="Enter a choice…", id="user-input")
            with Horizontal(id="dialog-actions"):
                yield Button("Cancel", id="cancel-input")
                yield Button("Continue", id="submit-input")

    def on_mount(self) -> None:
        if self.recovery is None:
            self.query_one("#user-input", Input).focus()
        else:
            options = self.query_one("#recovery-choices", OptionList)
            options.highlighted = len(self.recovery.choices) - 1
            options.focus()
            self._sync_buttons()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.recovery is not None and event.option_id:
            self._choice_number = event.option_id.removeprefix("option-")
            self._sync_buttons()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.recovery is not None and event.option_id:
            self._choice_number = event.option_id.removeprefix("option-")
            self._sync_buttons()
            # Enter selects a menu item. Continue remains an explicit decision.

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "cleanup-check":
            self._sync_buttons()

    def _cleanup_checkbox(self) -> Checkbox | None:
        try:
            return self.query_one("#cleanup-check", Checkbox)
        except NoMatches:
            return None

    def _sync_buttons(self) -> None:
        if self.recovery is None or not self.is_mounted:
            return
        selected = next((c for c in self.recovery.choices if c.number == self._choice_number), None)
        needs_cleanup = bool(selected and selected.needs_cleanup and self.recovery.has_uncertain_upload)
        checkbox = self._cleanup_checkbox()
        if checkbox is not None:
            checkbox.display = needs_cleanup
        self.query_one("#submit-input", Button).disabled = bool(
            needs_cleanup and not (checkbox is not None and checkbox.value)
        )

    def _continue(self) -> None:
        if self.recovery is None:
            answer = self.query_one("#user-input", Input).value.strip()
            self.dismiss(answer or "0")
            return
        try:
            checkbox = self._cleanup_checkbox()
            response = encode_recovery_choice(
                self.recovery,
                self._choice_number or "",
                cleanup_checked=bool(checkbox and checkbox.value),
            )
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        self.dismiss(response)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._continue()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit-input":
            self._continue()
        elif event.button.id == "cancel-input":
            self.dismiss("0")
