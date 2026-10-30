"""Validated request lifecycle transitions for RelayPilot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

from application.contracts import RequestStatus


class UnknownRequestError(KeyError):
    """Raised when a request ID is not registered."""


class InvalidRequestTransition(ValueError):
    """Raised when a request attempts an invalid lifecycle transition."""


_TERMINAL_STATES = {
    RequestStatus.SUBMISSION_UNKNOWN,
    RequestStatus.COMPLETED,
    RequestStatus.CANCELLED,
    RequestStatus.FAILED,
}

_ACTIVE_STATES = {
    RequestStatus.QUEUED,
    RequestStatus.PREPARING,
    RequestStatus.SUBMITTING,
    RequestStatus.SUBMITTED,
    RequestStatus.WAITING_FOR_RESPONSE,
    RequestStatus.STREAMING,
    RequestStatus.CANCELLING,
}

_ALLOWED_TRANSITIONS: dict[RequestStatus, set[RequestStatus]] = {
    RequestStatus.QUEUED: {
        RequestStatus.PREPARING,
        RequestStatus.CANCELLED,
        RequestStatus.FAILED,
    },
    RequestStatus.PREPARING: {
        RequestStatus.SUBMITTING,
        RequestStatus.CANCELLING,
        RequestStatus.NEEDS_USER_ACTION,
        RequestStatus.CANCELLED,
        RequestStatus.FAILED,
    },
    RequestStatus.SUBMITTING: {
        RequestStatus.SUBMITTED,
        RequestStatus.CANCELLING,
        RequestStatus.NEEDS_USER_ACTION,
        RequestStatus.SUBMISSION_UNKNOWN,
        RequestStatus.CANCELLED,
        RequestStatus.FAILED,
    },
    RequestStatus.SUBMITTED: {
        RequestStatus.WAITING_FOR_RESPONSE,
        RequestStatus.CANCELLING,
        RequestStatus.NEEDS_USER_ACTION,
        RequestStatus.FAILED,
    },
    RequestStatus.WAITING_FOR_RESPONSE: {
        RequestStatus.STREAMING,
        RequestStatus.CANCELLING,
        RequestStatus.NEEDS_USER_ACTION,
        RequestStatus.COMPLETED,
        RequestStatus.FAILED,
    },
    RequestStatus.STREAMING: {
        RequestStatus.CANCELLING,
        RequestStatus.NEEDS_USER_ACTION,
        RequestStatus.COMPLETED,
        RequestStatus.FAILED,
    },
    RequestStatus.CANCELLING: {
        # Completion may win the race with a cancellation request.
        RequestStatus.COMPLETED,
        RequestStatus.CANCELLED,
        RequestStatus.SUBMISSION_UNKNOWN,
        RequestStatus.FAILED,
    },
    RequestStatus.NEEDS_USER_ACTION: {
        RequestStatus.CANCELLING,
        RequestStatus.CANCELLED,
        RequestStatus.FAILED,
    },
    RequestStatus.SUBMISSION_UNKNOWN: set(),
    RequestStatus.COMPLETED: set(),
    RequestStatus.CANCELLED: set(),
    RequestStatus.FAILED: set(),
}


@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    request_id: str
    status: RequestStatus
    created_at: datetime
    updated_at: datetime
    message: str | None = None
    resume_status: RequestStatus | None = None


@dataclass(slots=True)
class _RequestRecord:
    request_id: str
    status: RequestStatus
    created_at: datetime
    updated_at: datetime
    message: str | None = None
    resume_status: RequestStatus | None = None

    def snapshot(self) -> RequestSnapshot:
        return RequestSnapshot(
            request_id=self.request_id,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            message=self.message,
            resume_status=self.resume_status,
        )


class RequestStateMachine:
    """Track and validate request states; one instance belongs to the app layer."""

    def __init__(self) -> None:
        self._records: dict[str, _RequestRecord] = {}
        self._lock = RLock()

    def create(self, request_id: str) -> RequestSnapshot:
        if not request_id or not request_id.strip():
            raise ValueError("request_id cannot be empty")

        now = datetime.now(timezone.utc)
        with self._lock:
            if request_id in self._records:
                raise ValueError(f"request already exists: {request_id}")
            record = _RequestRecord(
                request_id=request_id,
                status=RequestStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
            self._records[request_id] = record
            return record.snapshot()

    def get(self, request_id: str) -> RequestSnapshot:
        with self._lock:
            return self._require(request_id).snapshot()

    def transition(
        self,
        request_id: str,
        new_status: RequestStatus,
        *,
        message: str | None = None,
    ) -> RequestSnapshot:
        with self._lock:
            record = self._require(request_id)
            allowed = _ALLOWED_TRANSITIONS[record.status]
            if new_status not in allowed:
                raise InvalidRequestTransition(
                    f"Cannot transition request {request_id} "
                    f"from {record.status.value} to {new_status.value}."
                )
            record.status = new_status
            record.updated_at = datetime.now(timezone.utc)
            record.message = message
            if new_status != RequestStatus.NEEDS_USER_ACTION:
                record.resume_status = None
            return record.snapshot()

    def pause_for_user_action(
        self,
        request_id: str,
        *,
        message: str,
    ) -> RequestSnapshot:
        """Pause an active request while Chrome needs user intervention."""
        with self._lock:
            record = self._require(request_id)
            if record.status not in _ACTIVE_STATES:
                raise InvalidRequestTransition(
                    f"Cannot pause request in state {record.status.value}."
                )
            record.resume_status = record.status
            record.status = RequestStatus.NEEDS_USER_ACTION
            record.message = message
            record.updated_at = datetime.now(timezone.utc)
            return record.snapshot()

    def resume_after_user_action(self, request_id: str) -> RequestSnapshot:
        """Restore the state that was active before the user-action pause."""
        with self._lock:
            record = self._require(request_id)
            if record.status != RequestStatus.NEEDS_USER_ACTION:
                raise InvalidRequestTransition(
                    "Request is not waiting for user action."
                )
            if record.resume_status is None:
                raise InvalidRequestTransition(
                    "Request has no saved state to resume."
                )
            record.status = record.resume_status
            record.resume_status = None
            record.message = None
            record.updated_at = datetime.now(timezone.utc)
            return record.snapshot()

    def is_terminal(self, request_id: str) -> bool:
        with self._lock:
            return self._require(request_id).status in _TERMINAL_STATES

    def _require(self, request_id: str) -> _RequestRecord:
        try:
            return self._records[request_id]
        except KeyError as error:
            raise UnknownRequestError(request_id) from error
