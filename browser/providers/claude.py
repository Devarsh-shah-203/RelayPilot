"""Claude browser attachment adapter."""

from .base import BrowserAttachmentProvider


class ClaudeAttachmentProvider(BrowserAttachmentProvider):
    name = "claude"
    domains = ("claude.ai",)
