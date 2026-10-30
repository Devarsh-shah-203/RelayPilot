"""Single-line, low-contrast application status bar."""

from textual.containers import Horizontal
from textual.widgets import Static


class StatusBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="statusbar")

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        width: 100%;
        padding: 0 1;
        background: #1a1d22;
        color: #858b94;
    }
    StatusBar Static {
        height: 1;
        width: auto;
        margin-right: 3;
    }
    StatusBar #request-status {
        width: 1fr;
        margin-right: 1;
        content-align: right middle;
    }
    """

    def compose(self):
        yield Static("Provider —", id="provider-status", markup=False)
        yield Static("Memory OFF", id="memory-status", markup=False)
        yield Static("Workspace —", id="workspace-status", markup=False)
        yield Static("Idle", id="request-status", markup=False)

    def set_request_status(self, value: str) -> None:
        self.query_one("#request-status", Static).update(value)

    def set_memory_status(self, value: str) -> None:
        self.query_one("#memory-status", Static).update(f"Memory {value}")

    def set_workspace_status(self, value: str) -> None:
        self.query_one("#workspace-status", Static).update(f"Workspace {value}")

    def set_provider_status(self, value: str) -> None:
        self.query_one("#provider-status", Static).update(f"Provider: {value}")
