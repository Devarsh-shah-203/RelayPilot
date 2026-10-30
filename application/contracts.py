"""Typed messages shared by the RelayPilot UI, application and runtime layers.

These contracts intentionally contain no Textual or Playwright imports. The
terminal and browser implementations can therefore be replaced independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias
from uuid import uuid4


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_request_id() -> str:
    """Create an opaque identifier for one submitted prompt/request."""
    return _new_id("req")


class RequestStatus(str, Enum):
    """Lifecycle state of one request (not the overall application state)."""

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    WAITING_FOR_RESPONSE = "WAITING_FOR_RESPONSE"
    STREAMING = "STREAMING"
    CANCELLING = "CANCELLING"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# UI -> Application / Runtime commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class UICommand:
    """Base envelope for a command originating from the terminal UI."""

    command_id: str = field(default_factory=lambda: _new_id("cmd"))
    submitted_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class SubmitPrompt(UICommand):
    request_id: str
    prompt: str

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id cannot be empty")
        if not self.prompt.strip():
            raise ValueError("prompt cannot be empty")


@dataclass(frozen=True, slots=True)
class ExecuteCommand(UICommand):
    """Execute an existing slash command through the canonical command router."""

    command_text: str
    request_id: str = field(default_factory=new_request_id)

    def __post_init__(self) -> None:
        if not self.command_text.strip().startswith("/"):
            raise ValueError("command_text must be a slash command")
        if not self.request_id.strip():
            raise ValueError("request_id cannot be empty")


@dataclass(frozen=True, slots=True)
class CancelRequest(UICommand):
    """Request cancellation of one request; this is not the /cancel command."""

    request_id: str

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id cannot be empty")


@dataclass(frozen=True, slots=True)
class ResolveEditApproval(UICommand):
    approval_id: str
    approved: bool

    def __post_init__(self) -> None:
        if not self.approval_id.strip():
            raise ValueError("approval_id cannot be empty")


@dataclass(frozen=True, slots=True)
class ResolveUserInput(UICommand):
    input_id: str
    value: str

    def __post_init__(self) -> None:
        if not self.input_id.strip():
            raise ValueError("input_id cannot be empty")


@dataclass(frozen=True, slots=True)
class ReconnectBrowser(UICommand):
    """Retry the CDP connection without restarting the terminal application."""


@dataclass(frozen=True, slots=True)
class ShutdownRuntime(UICommand):
    reason: str = "application_exit"


RuntimeCommand: TypeAlias = (
    SubmitPrompt
    | ExecuteCommand
    | CancelRequest
    | ResolveEditApproval
    | ResolveUserInput
    | ReconnectBrowser
    | ShutdownRuntime
)


# ---------------------------------------------------------------------------
# Runtime -> Application / UI events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeEvent:
    """Base envelope for events produced by the application/runtime."""

    event_id: str = field(default_factory=lambda: _new_id("evt"))
    occurred_at: datetime = field(default_factory=utc_now)
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeReady(RuntimeEvent):
    provider: str
    connection_label: str = "Connected"
    supported: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeStopped(RuntimeEvent):
    reason: str = "shutdown"


@dataclass(frozen=True, slots=True)
class RuntimeConnectionFailed(RuntimeEvent):
    message: str


@dataclass(frozen=True, slots=True)
class AppNotice(RuntimeEvent):
    title: str
    message: str
    severity: str = "information"


@dataclass(frozen=True, slots=True)
class HelpRequested(RuntimeEvent):
    """Open the full command reference in a scrollable terminal screen."""

    text: str


@dataclass(frozen=True, slots=True)
class RequestStateChanged(RuntimeEvent):
    previous_status: RequestStatus | None
    status: RequestStatus
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ResponseStarted(RuntimeEvent):
    """The user-visible final answer has begun (not a separate thinking panel)."""


@dataclass(frozen=True, slots=True)
class ResponseDelta(RuntimeEvent):
    """Append-only text update for the active response."""

    sequence: int
    text: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("response sequence must be >= 1")
        if not self.text:
            raise ValueError("response delta cannot be empty")


@dataclass(frozen=True, slots=True)
class ResponseRevised(RuntimeEvent):
    """Full response correction when the source DOM revises earlier text."""

    sequence: int
    full_text: str
    reason: str = "source_content_changed"

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("response sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class RequestCompleted(RuntimeEvent):
    """Completion marker. Raw text remains in the application ResponseStore."""


@dataclass(frozen=True, slots=True)
class RequestCancelled(RuntimeEvent):
    partial_response_preserved: bool = True


@dataclass(frozen=True, slots=True)
class RequestFailed(RuntimeEvent):
    error_code: str
    message: str
    retryable: bool = False
    submission_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class UserActionRequired(RuntimeEvent):
    action: str
    message: str


@dataclass(frozen=True, slots=True)
class MemoryStatusChanged(RuntimeEvent):
    mode: str
    snapshot: str


@dataclass(frozen=True, slots=True)
class AttachmentStatusChanged(RuntimeEvent):
    attachment_id: str
    file_name: str
    status: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class EditModeChanged(RuntimeEvent):
    enabled: bool


@dataclass(frozen=True, slots=True)
class EditProposalStarted(RuntimeEvent):
    """The browser request carries a structured edit proposal, not chat prose."""


@dataclass(frozen=True, slots=True)
class EditReviewResult(RuntimeEvent):
    """Persistent review result for a structured edit request.

    This is separate from the original raw model response, which remains in
    ResponseStore for parsing, validation, and diagnostics.
    """

    successful: bool
    message: str


@dataclass(frozen=True, slots=True)
class EditApprovalRequired(RuntimeEvent):
    approval_id: str
    file_name: str
    reason: str
    diff: str


@dataclass(frozen=True, slots=True)
class UserInputRequested(RuntimeEvent):
    input_id: str
    prompt: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceStatusChanged(RuntimeEvent):
    directories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandCompleted(RuntimeEvent):
    command_id: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class CommandFailed(RuntimeEvent):
    command_id: str
    error_code: str
    message: str


RuntimeEventType: TypeAlias = (
    RuntimeReady
    | RuntimeStopped
    | RuntimeConnectionFailed
    | AppNotice
    | HelpRequested
    | RequestStateChanged
    | ResponseStarted
    | ResponseDelta
    | ResponseRevised
    | RequestCompleted
    | RequestCancelled
    | RequestFailed
    | UserActionRequired
    | MemoryStatusChanged
    | AttachmentStatusChanged
    | EditModeChanged
    | EditProposalStarted
    | EditReviewResult
    | EditApprovalRequired
    | UserInputRequested
    | WorkspaceStatusChanged
    | CommandCompleted
    | CommandFailed
)
