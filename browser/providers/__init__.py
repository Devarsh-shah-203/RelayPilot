"""Provider-specific browser attachment adapters."""

from .factory import UnsupportedAttachmentProvider, create_attachment_provider
from .chatgpt import ChatGPTAttachmentProvider
from .claude import ClaudeAttachmentProvider
from .gemini import GeminiAttachmentProvider

__all__ = [
    "UnsupportedAttachmentProvider", "create_attachment_provider",
    "ChatGPTAttachmentProvider", "ClaudeAttachmentProvider", "GeminiAttachmentProvider",
]
