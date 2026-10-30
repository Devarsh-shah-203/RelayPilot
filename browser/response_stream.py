"""Convert cumulative browser DOM snapshots into append/revision updates.

The browser UI exposes the assistant response as a cumulative DOM snapshot. This
small tracker emits only the newly appended suffix in the normal case and emits
a full revision only when the provider changes content that was already shown.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResponseChange:
    kind: str  # "delta" or "revision"
    sequence: int
    text: str
    snapshot: str


class ResponseStreamTracker:
    def __init__(self) -> None:
        self._latest = ""
        self._sequence = 0

    @property
    def latest(self) -> str:
        return self._latest

    @property
    def sequence(self) -> int:
        return self._sequence

    def update(self, snapshot: str) -> ResponseChange | None:
        if not isinstance(snapshot, str):
            raise TypeError("response snapshot must be a string")
        if snapshot == self._latest:
            return None

        if snapshot.startswith(self._latest):
            change = ResponseChange(
                kind="delta",
                sequence=self._sequence + 1,
                text=snapshot[len(self._latest):],
                snapshot=snapshot,
            )
        else:
            change = ResponseChange(
                kind="revision",
                sequence=self._sequence + 1,
                text=snapshot,
                snapshot=snapshot,
            )

        self._latest = snapshot
        self._sequence += 1
        return change
