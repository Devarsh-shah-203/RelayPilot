"""Conversation message widgets with append-only streaming and final Markdown."""

from __future__ import annotations

from rich.markdown import Markdown
from textual.containers import Vertical
from textual.widgets import Static, TextArea


class ConversationMessage(Vertical):
    """One mounted conversation message.

    While an assistant response streams, deltas are appended to a read-only
    TextArea. On completion (or interruption), that one active message is
    converted to a Rich Markdown renderable. Older messages are not rebuilt.
    """

    DEFAULT_CSS = """
    ConversationMessage {
        height: auto;
        width: 100%;
        margin: 1 0 2 0;
        padding: 0 1;
    }
    ConversationMessage .message-label {
        height: 1;
        width: 100%;
        margin-bottom: 1;
        color: #858b94;
        text-style: bold;
    }
    ConversationMessage #message-body {
        height: auto;
        min-height: 1;
        width: 100%;
        border: none;
        padding: 0;
        background: transparent;
        color: #dfe1e3;
    }
    ConversationMessage.user .message-label {
        color: #a5afbd;
    }
    ConversationMessage.assistant .message-label {
        color: #b5c2b3;
    }
    ConversationMessage.system .message-label {
        color: #858b94;
    }
    ConversationMessage .message-state {
        height: 1;
        margin-top: 1;
        color: #b49a72;
    }
    ConversationMessage.user {
        padding-left: 1;
    }
    ConversationMessage .request-progress {
        display: none;
        height: 1;
        margin-top: 1;
        color: #8d9ba9;
        text-style: italic;
    }
    """

    def __init__(self, role: str, content: str = "", *, message_id: str | None = None) -> None:
        self.role = role.lower()
        self.raw_content = content
        self.message_id = message_id
        self._markdown_finalized = False
        self._state_label: Static | None = None
        super().__init__(classes=f"message {self.role}")

    def compose(self):
        label = {
            "user": "YOU",
            "assistant": "RELAYPILOT",
            "system": "INFO",
        }.get(self.role, self.role.upper())
        yield Static(label, classes="message-label", markup=False)
        if self.role == "assistant":
            yield TextArea(
                self.raw_content,
                id="message-body",
                read_only=True,
                show_cursor=False,
                show_line_numbers=False,
                soft_wrap=True,
            )
        else:
            yield Static(self.raw_content, id="message-body", markup=False)
        if self.role == "user":
            yield Static("", classes="request-progress", markup=False)

    def set_request_progress(self, label: str | None) -> None:
        """Show short-lived request state directly beneath this user prompt."""
        if self.role != "user":
            return
        progress = self.query_one(".request-progress", Static)
        progress.update(label or "")
        progress.display = bool(label)

    def append_text(self, delta: str) -> None:
        """Append a response delta without replacing previously received text."""
        if not delta or self._markdown_finalized:
            return
        self.raw_content += delta
        body = self.query_one("#message-body", TextArea)
        body.insert(delta, location=body.document.end)

    async def replace_text(self, full_text: str) -> None:
        """Apply a rare source revision to the currently active response."""
        self.raw_content = full_text
        if self._markdown_finalized:
            self._markdown_finalized = False
            body = self.query_one("#message-body", Static)
            await body.remove()
            await self.mount(
                TextArea(
                    self.raw_content,
                    id="message-body",
                    read_only=True,
                    show_cursor=False,
                    show_line_numbers=False,
                    soft_wrap=True,
                )
            )
            return
        self.query_one("#message-body", TextArea).load_text(full_text)

    async def finalize_markdown(self) -> None:
        """Render the active response as Markdown once generation has ended."""
        if self.role != "assistant" or self._markdown_finalized:
            return
        body = self.query_one("#message-body", TextArea)
        await body.remove()
        await self.mount(
            Static(
                Markdown(self.raw_content or " ", code_theme="native"),
                id="message-body",
                classes="rendered-markdown",
                markup=False,
            )
        )
        self._markdown_finalized = True

    async def mark_interrupted(self, label: str = "Generation stopped · partial response kept") -> None:
        """Finalize the partial response and attach a separate status label."""
        await self.finalize_markdown()
        if self._state_label is None:
            self._state_label = Static(label, classes="message-state", markup=False)
            await self.mount(self._state_label)
        else:
            self._state_label.update(label)
