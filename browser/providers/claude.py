"""Claude browser attachment adapter.

Reuse already-exposed compatible inputs. If the composer hides its file input
behind an attachment menu, open it using accessible control names rather than
assuming a DOM index.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

from .base import BrowserAttachmentProvider


class ClaudeAttachmentProvider(BrowserAttachmentProvider):
    name = "claude"
    domains = ("claude.ai",)

    def prepare_for_upload(self, paths: Sequence[Path]) -> None:
        if self._has_compatible_inputs(paths):
            return

        opened = self._click_named_upload_control(
            (r"^attach files$", r"^add files$", r"^add content$", r"^attach$"),
            roles=("button",),
        )
        if opened:
            self._wait_for_compatible_inputs(paths, timeout=2.0)

        if not self._has_compatible_inputs(paths):
            self._click_named_upload_control(
                (r"^upload files$", r"^add files$", r"^upload from computer$"),
                roles=("menuitem", "button"),
            )

        self._wait_for_compatible_inputs(paths, timeout=8.0)
