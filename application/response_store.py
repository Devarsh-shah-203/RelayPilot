"""Canonical raw-response storage, independent of terminal presentation."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


class UnknownResponseError(KeyError):
    """Raised when no response record exists for a request ID."""


class ResponseSequenceGap(ValueError):
    """Raised when a response update arrives out of order or is missing."""


@dataclass(frozen=True, slots=True)
class ResponseSnapshot:
    request_id: str
    raw_content: str
    last_sequence: int
    revision_count: int
    completed: bool


@dataclass(slots=True)
class _ResponseRecord:
    request_id: str
    raw_content: str = ""
    last_sequence: int = 0
    revision_count: int = 0
    completed: bool = False

    def snapshot(self) -> ResponseSnapshot:
        return ResponseSnapshot(
            request_id=self.request_id,
            raw_content=self.raw_content,
            last_sequence=self.last_sequence,
            revision_count=self.revision_count,
            completed=self.completed,
        )


class ResponseStore:
    """Accumulate raw response text before any Rich/Textual formatting.

    The application controller should feed every runtime update into this store
    before forwarding a coalesced presentation update to the UI. That way the
    terminal can refresh less often without losing response text.
    """

    def __init__(self) -> None:
        self._records: dict[str, _ResponseRecord] = {}
        self._lock = RLock()

    def start(self, request_id: str) -> ResponseSnapshot:
        if not request_id or not request_id.strip():
            raise ValueError("request_id cannot be empty")
        with self._lock:
            if request_id in self._records:
                raise ValueError(f"response already exists: {request_id}")
            record = _ResponseRecord(request_id=request_id)
            self._records[request_id] = record
            return record.snapshot()

    def append_delta(
        self,
        request_id: str,
        *,
        sequence: int,
        text: str,
    ) -> ResponseSnapshot:
        if sequence < 1:
            raise ValueError("response sequence must be >= 1")
        if not isinstance(text, str) or not text:
            raise ValueError("response delta must be a non-empty string")

        with self._lock:
            record = self._require(request_id)
            if sequence <= record.last_sequence:
                # Duplicate or stale event: idempotently ignore it.
                return record.snapshot()
            expected = record.last_sequence + 1
            if sequence != expected:
                raise ResponseSequenceGap(
                    f"Expected response sequence {expected}, received {sequence}."
                )
            if record.completed:
                raise ValueError("cannot append to a completed response")
            record.raw_content += text
            record.last_sequence = sequence
            return record.snapshot()

    def revise(
        self,
        request_id: str,
        *,
        sequence: int,
        full_text: str,
    ) -> ResponseSnapshot:
        """Replace the current raw snapshot after a genuine source correction."""
        if sequence < 1:
            raise ValueError("response sequence must be >= 1")
        if not isinstance(full_text, str):
            raise TypeError("full_text must be a string")

        with self._lock:
            record = self._require(request_id)
            if sequence <= record.last_sequence:
                return record.snapshot()
            expected = record.last_sequence + 1
            if sequence != expected:
                raise ResponseSequenceGap(
                    f"Expected response sequence {expected}, received {sequence}."
                )
            if record.completed:
                raise ValueError("cannot revise a completed response")
            record.raw_content = full_text
            record.last_sequence = sequence
            record.revision_count += 1
            return record.snapshot()

    def mark_completed(self, request_id: str) -> ResponseSnapshot:
        with self._lock:
            record = self._require(request_id)
            record.completed = True
            return record.snapshot()

    def get(self, request_id: str) -> ResponseSnapshot:
        with self._lock:
            return self._require(request_id).snapshot()

    def _require(self, request_id: str) -> _ResponseRecord:
        try:
            return self._records[request_id]
        except KeyError as error:
            raise UnknownResponseError(request_id) from error
