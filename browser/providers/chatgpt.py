"""ChatGPT browser attachment adapter.

ChatGPT may expose only photo/video inputs until the composer attachment menu
is opened. This adapter first reuses any already-compatible input, then opens
the attachment menu and selects Upload files only when needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .base import BrowserAttachmentProvider


class ChatGPTAttachmentProvider(BrowserAttachmentProvider):
    name = "chatgpt"
    domains = ("chatgpt.com", "chat.openai.com")

    def prepare_for_upload(self, paths: Sequence[Path]) -> None:
        # Reuse a compatible hidden input if ChatGPT already exposes one. This
        # preserves the direct-upload path that works in some ChatGPT sessions.
        if self._has_compatible_inputs(paths):
            return

        # Otherwise, open the composer attachment menu. Accessible labels change
        # occasionally, so try a short list of exact semantic names rather than
        # relying on brittle DOM positions.
        opened = self._click_named_upload_control((
            r"^add files and tools$",
            r"^attach files$",
            r"^add photos (?:and|&) files$",
            r"^add files$",
        ), roles=("button",))

        if opened:
            # Some versions create the general file input immediately. Others
            # show an Upload files menu item that must be selected first.
            self._wait_for_compatible_inputs(paths, timeout=2.0)

        if not self._has_compatible_inputs(paths):
            self._click_named_upload_control((
                r"^upload files$",
                r"^attach files$",
            ), roles=("menuitem", "button"))

        # If the interface stays ambiguous, the base adapter returns UNKNOWN
        # rather than misclassifying the file format as unsupported or retrying
        # the selection blindly.
        self._wait_for_compatible_inputs(paths, timeout=8.0)
