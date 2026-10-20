"""Provider-independent models for browser-based file attachments."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AttachmentStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    ATTACHED = "ATTACHED"
    FAILED = "FAILED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"
    BLOCKED = "BLOCKED"


class DeliveryMode(str, Enum):
    INLINE = "INLINE"
    ATTACHMENTS = "ATTACHMENTS"
    MANUAL = "MANUAL"


@dataclass
class AttachmentRecord:
    path: Path
    status: AttachmentStatus = AttachmentStatus.PENDING
    attempts: int = 0
    message: str = ""
    verification_method: str = ""

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser().resolve()


@dataclass
class UploadReport:
    provider_name: str
    records: list[AttachmentRecord] = field(default_factory=list)

    @property
    def attached(self) -> list[AttachmentRecord]:
        return [item for item in self.records if item.status == AttachmentStatus.ATTACHED]

    @property
    def failed(self) -> list[AttachmentRecord]:
        return [item for item in self.records if item.status == AttachmentStatus.FAILED]

    @property
    def limited(self) -> list[AttachmentRecord]:
        return [item for item in self.records if item.status == AttachmentStatus.LIMIT_EXCEEDED]

    @property
    def unknown(self) -> list[AttachmentRecord]:
        return [item for item in self.records if item.status == AttachmentStatus.UNKNOWN]

    @property
    def unsupported(self) -> list[AttachmentRecord]:
        return [
            item for item in self.records
            if item.status in {AttachmentStatus.UNSUPPORTED, AttachmentStatus.BLOCKED}
        ]

    @property
    def all_required_attached(self) -> bool:
        return bool(self.records) and all(
            item.status == AttachmentStatus.ATTACHED for item in self.records
        )

    @property
    def needs_user_decision(self) -> bool:
        return any(
            item.status in {
                AttachmentStatus.FAILED,
                AttachmentStatus.LIMIT_EXCEEDED,
                AttachmentStatus.UNKNOWN,
                AttachmentStatus.UNSUPPORTED,
                AttachmentStatus.BLOCKED,
            }
            for item in self.records
        )
