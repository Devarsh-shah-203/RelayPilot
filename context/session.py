"""One-shot project memory inclusion, independent of persistent edit mode."""

from __future__ import annotations

import os
from pathlib import Path


class MemoryContextSession:
    OFF = "OFF"
    NEXT = "NEXT"
    # Compatibility constant only; enable() now arms one request.
    ON = NEXT

    def __init__(self, project_id=None):
        self.project_id = self._key(project_id)
        self.refresh_pending = False
        self.snapshot_loaded = False

    @staticmethod
    def _key(project_id):
        if project_id is None:
            return None
        return os.path.normcase(str(Path(project_id).expanduser().resolve()))

    @property
    def mode(self):
        return self.NEXT if self.refresh_pending else self.OFF

    def switch_project(self, project_id):
        """Changing projects clears pending and sent state; nothing leaks."""
        project_id = self._key(project_id)
        if project_id != self.project_id:
            self.project_id = project_id
            self.refresh_pending = False
            self.snapshot_loaded = False
            return True
        return False

    def refresh(self):
        self.refresh_pending = True
        self.snapshot_loaded = False

    def should_include_memory(self):
        return self.refresh_pending

    def mark_memory_sent(self, project_id=None):
        """Call only after provider-confirmed submission, never preparation."""
        if project_id is not None and self._key(project_id) != self.project_id:
            return False
        if not self.refresh_pending:
            return False
        self.refresh_pending = False
        self.snapshot_loaded = True
        return True

    def use_once(self):
        """Compatibility alias; the user-facing use command is retired."""
        self.refresh()

    def enable(self):
        """Compatibility alias with no persistent injection behavior."""
        self.refresh()

    def disable(self):
        self.refresh_pending = False
        self.snapshot_loaded = False

    def get_status(self):
        return {
            "mode": self.mode,
            "snapshot": (
                "REFRESH PENDING" if self.refresh_pending
                else "LOADED" if self.snapshot_loaded else "NOT LOADED"
            ),
        }
