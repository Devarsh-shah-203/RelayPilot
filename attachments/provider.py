"""Provider contract for browser-based attachment transports."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from .models import AttachmentRecord


class AttachmentProvider(Protocol):
    """Minimum interface required by AttachmentManager."""

    @property
    def name(self) -> str: ...

    def upload_files(self, paths: Sequence[Path]) -> list[AttachmentRecord]:
        """Select files in the provider UI and return verified per-file results."""
        ...
