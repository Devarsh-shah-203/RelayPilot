"""Scrollable conversation region and empty-state welcome panel."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll, Vertical
from textual.widgets import Static

from terminal.widgets.message import ConversationMessage
from terminal.widgets.edit_review import InlineEditReview


class ConversationView(VerticalScroll):
    """Persistent scrollable history; only the active message is updated."""

    def __init__(self, *, id: str = "conversation") -> None:
        super().__init__(id=id)

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-panel"):
            yield Static("RELAYPILOT", id="welcome-title", markup=False)
            yield Static(
                "A focused terminal for your AI workflows.",
                id="welcome-subtitle",
                markup=False,
            )
            yield Static(
                "Enter sends  ·  Shift+Enter / Alt+N adds a new line  ·  Alt+P commands",
                id="welcome-hint",
                markup=False,
            )

    async def add_message(
        self,
        role: str,
        content: str = "",
        *,
        message_id: str | None = None,
    ) -> ConversationMessage:
        try:
            welcome = self.query_one("#welcome-panel", Vertical)
            await welcome.remove()
        except Exception:
            # The welcome panel is removed after the first message.
            pass

        message = ConversationMessage(role, content, message_id=message_id)
        await self.mount(message)
        self.scroll_end(animate=False)
        return message

    async def add_edit_review(
        self, approval_id: str, file_name: str, reason: str, diff: str
    ) -> InlineEditReview:
        """Append a proposed edit after the prompt; preserve earlier reviews."""
        review = InlineEditReview(approval_id, file_name, reason, diff)
        await self.mount(review)
        self.scroll_end(animate=False)
        return review
