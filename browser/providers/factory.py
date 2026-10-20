"""Choose a browser attachment provider from the currently selected page URL."""

from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page

from .base import BrowserAttachmentProvider
from .chatgpt import ChatGPTAttachmentProvider
from .claude import ClaudeAttachmentProvider
from .gemini import GeminiAttachmentProvider


class UnsupportedAttachmentProvider(ValueError):
    """Raised when the current browser tab has no known attachment adapter."""


def create_attachment_provider(page: Page) -> BrowserAttachmentProvider:
    host = (urlparse(page.url).hostname or "").lower()
    adapters = (
        ChatGPTAttachmentProvider,
        ClaudeAttachmentProvider,
        GeminiAttachmentProvider,
    )
    for adapter_type in adapters:
        if any(host == domain or host.endswith("." + domain) for domain in adapter_type.domains):
            return adapter_type(page)
    raise UnsupportedAttachmentProvider(
        f"No attachment adapter is registered for browser host {host or '<unknown>'!r}."
    )
