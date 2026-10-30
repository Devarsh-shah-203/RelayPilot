"""Searchable command palette backed by the existing command registry."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from application.command_registry import COMMANDS


class _PaletteFilter(Input):
    BINDINGS = [Binding("down", "focus_options", "Results", show=False)]

    def action_focus_options(self) -> None:
        self.app.screen.query_one("#palette-options", OptionList).focus()


class CommandPaletteScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close_palette", "Close", show=False, priority=True)]

    DEFAULT_CSS = """
    CommandPaletteScreen {
        align: center middle;
        background: #000000 68%;
    }
    CommandPaletteScreen > Vertical {
        width: 78;
        height: 68%;
        padding: 1 2;
        border: round #424a54;
        background: #171b20;
    }
    CommandPaletteScreen #palette-title {
        height: 1;
        color: #dfe1e3;
        text-style: bold;
        margin-bottom: 1;
    }
    CommandPaletteScreen #palette-filter {
        margin-bottom: 1;
        background: #20252b;
        color: #dfe1e3;
        border: round #343b44;
    }
    CommandPaletteScreen #palette-options {
        height: 1fr;
        border: none;
        background: #171b20;
        color: #c4c9cf;
    }
    CommandPaletteScreen #palette-options > .option-list--option {
        background: #171b20;
        color: #c4c9cf;
    }
    CommandPaletteScreen #palette-options > .option-list--option-highlighted {
        background: #343b44;
        color: #f0f1f2;
    }
    CommandPaletteScreen #palette-options > .option-list--option-hover {
        background: #252b32;
    }
    CommandPaletteScreen #palette-hint {
        height: 1;
        color: #858b94;
        margin-top: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._filtered = list(COMMANDS)
        self._command_by_id: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("RELAYPILOT COMMANDS", id="palette-title", markup=False)
            yield _PaletteFilter(placeholder="Filter commands…", id="palette-filter")
            yield OptionList(*self._build_options(self._filtered), id="palette-options")
            yield Static("Enter: insert command  ·  Esc: close", id="palette-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#palette-filter", Input).focus()

    def action_close_palette(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        self._filtered = [
            item for item in COMMANDS
            if query in item.usage.lower()
            or query in item.description.lower()
            or query in item.category.lower()
        ]
        self.query_one("#palette-options", OptionList).set_options(
            self._build_options(self._filtered)
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the search field picks the first match; the option list can
        # also be focused for explicit Up/Down navigation and mouse selection.
        if self._filtered:
            self.dismiss(self._filtered[0].usage)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        usage = self._command_by_id.get(event.option_id or "")
        self.dismiss(usage)

    def _build_options(self, commands) -> list[Option]:
        options: list[Option] = []
        self._command_by_id.clear()
        for index, command in enumerate(commands):
            option_id = f"command-{index}"
            label = f"{command.usage}  —  {command.description}"
            options.append(Option(Text(label), id=option_id))
            self._command_by_id[option_id] = command.usage
        return options

