"""Gemini browser attachment adapter.

Gemini may create its file inputs only after its Upload and tools menu is opened.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Sequence

from .base import BrowserAttachmentProvider


class GeminiAttachmentProvider(BrowserAttachmentProvider):
    name = "gemini"
    domains = ("gemini.google.com",)

    def prepare_for_upload(self, paths: Sequence[Path]) -> None:
        # Gemini can expose an image-only input before its document upload flow
        # has been opened. Check compatibility with the requested paths, not just
        # whether any file input exists.
        if self._has_compatible_inputs(paths):
            return

        # The observed Gemini flow exposes input[type=file] after opening the
        # Upload and tools menu. Try the accessible name first; avoid hard-coding
        # an index because the page can add/remove inputs between releases.
        try:
            opened = self._click_named_upload_control((
                r"^upload and tools$",
            ), roles=("button",))
            if not opened:
                labelled = self.page.locator('[aria-label*="upload and tools" i]:visible')
                if labelled.count():
                    labelled.first.click(timeout=4000)
        except Exception:
            # Continue to the wait below: the menu might already have opened or
            # its control may expose a different role on a future UI version.
            pass

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if self._has_compatible_inputs(paths):
                return
            time.sleep(0.2)

        # If a document input has appeared but still does not accept the selected
        # files, select the menu's Upload files entry if it exists.
        if not self._has_compatible_inputs(paths):
            self._click_named_upload_control((r"^upload files$",), roles=("menuitem", "button"))
            self._wait_for_compatible_inputs(paths, timeout=8.0)
