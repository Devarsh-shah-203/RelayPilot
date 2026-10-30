"""Diff review modal for the existing validated edit workflow."""

from __future__ import annotations

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class EditApprovalScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("a", "approve", "Approve", show=True, priority=True),
        Binding("r", "reject", "Reject", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    EditApprovalScreen { align: center middle; background: #000000 70%; }
    EditApprovalScreen > Vertical {
        width: 90%; height: 90%; padding: 1 2;
        border: round #424a54; background: #171b20;
    }
    EditApprovalScreen .approval-title { height: 1; color: #e0e3e6; text-style: bold; }
    EditApprovalScreen #approval-reason { height: auto; max-height: 4; margin: 1 0; color: #aeb5bd; }
    EditApprovalScreen #approval-diff { height: 1fr; border: round #343b44; padding: 0 1; }
    EditApprovalScreen Horizontal { height: 3; align-horizontal: right; margin-top: 1; }
    EditApprovalScreen Button { margin-left: 1; }
    """

    def __init__(self, file_name: str, reason: str, diff: str) -> None:
        super().__init__()
        self.file_name = file_name
        self.reason = reason
        self.diff = diff

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"REVIEW EDIT · {self.file_name}", classes="approval-title", markup=False)
            yield Static(self.reason or "No reason supplied.", id="approval-reason", markup=False)
            with VerticalScroll(id="approval-diff"):
                yield Static(Syntax(self.diff, "diff", theme="ansi_dark", line_numbers=False), markup=False)
            with Horizontal():
                yield Button("Reject · R", id="reject-edit")
                yield Button("Approve · A", id="approve-edit", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve-edit":
            self.dismiss(True)
        elif event.button.id == "reject-edit":
            self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)

