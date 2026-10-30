"""Keyboard and command help modal."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from application.command_registry import COMMANDS


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close_help", "Close", show=False, priority=True)]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
        background: #000000 65%;
    }
    HelpScreen > Vertical {
        width: 82;
        height: 88%;
        padding: 1 2;
        border: round #424a54;
        background: #171b20;
    }
    HelpScreen .help-title {
        height: 1;
        color: #dfe1e3;
        text-style: bold;
        margin-bottom: 1;
    }
    HelpScreen .help-copy {
        height: auto;
        margin-bottom: 1;
        color: #b6bbc2;
    }
    HelpScreen #help-commands {
        height: 1fr;
        min-height: 8;
        color: #aeb5bd;
        padding: 0 1;
    }
    """

    def __init__(self, help_text: str | None = None) -> None:
        super().__init__()
        self.help_text = help_text

    def action_close_help(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        command_lines = [
            f"{item.usage:<36} {item.description}"
            for item in COMMANDS
        ]
        with Vertical():
            yield Static("RELAYPILOT · HELP", classes="help-title", markup=False)
            yield Static(
                "Enter          Send prompt\n"
                "Shift+Enter    Newline (VS Code may need keybinding setup)\n"
                "Alt+N          Newline fallback (or New line button)\n"
                "Escape         Close Help/Palette; otherwise stop request\n"
                "Alt+P          Commands (also Ctrl+P, /commands)\n"
                "Ctrl+R         Reconnect browser\n"
                "Alt+H          Help (also F1, /help)\n"
                "Alt+Q          Quit (also Ctrl+Q, /exit)",
                classes="help-copy",
                markup=False,
            )
            yield Static("COMMAND REFERENCE" if self.help_text else "COMMANDS", classes="help-title", markup=False)
            with VerticalScroll(id="help-commands"):
                yield Static(
                    self.help_text if self.help_text is not None else "\n\n".join(command_lines),
                    markup=False,
                )
            yield Static("Press Esc to close", classes="help-copy", markup=False)
