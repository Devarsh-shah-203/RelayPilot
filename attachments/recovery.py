"""Legacy recovery result types.

The live request workflow is implemented by ``attachments.integration.upload_and_confirm``
because inline fallback must know the original prompt, selected files, and remaining
inline-token budget. This module remains only for compatibility with older internal
imports; it intentionally does not mark manual/uncertain uploads as verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .manager import AttachmentManager
from .models import AttachmentStatus, UploadReport


class RecoveryAction(str, Enum):
    RETRIED_INDIVIDUALLY = "RETRIED_INDIVIDUALLY"
    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"  # legacy enum value; no longer produced
    CONTINUE_VERIFIED_ONLY = "CONTINUE_VERIFIED_ONLY"
    CANCELLED = "CANCELLED"
    NEEDS_DECISION = "NEEDS_DECISION"


@dataclass
class UploadRecoveryResult:
    action: RecoveryAction
    report: UploadReport
    submit_paths: list[Path] = field(default_factory=list)
    omitted_paths: list[Path] = field(default_factory=list)
    message: str = ""


def recover_upload_limit(
    report: UploadReport,
    manager: AttachmentManager,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> UploadRecoveryResult:
    """Return a conservative compatibility result without bypassing new recovery UI.

    Use ``upload_and_confirm`` for the production flow. This legacy function cannot
    safely offer inline fallback because it has no original prompt/context budget.
    """
    attached = [Path(record.path).resolve() for record in report.attached]
    omitted = [Path(record.path).resolve() for record in report.records if record.status != AttachmentStatus.ATTACHED]
    if not omitted:
        return UploadRecoveryResult(RecoveryAction.CONTINUE_VERIFIED_ONLY, report, attached, [])
    output_fn(
        "Upload recovery requires the request context and inline budget. "
        "Use attachments.integration.upload_and_confirm; this legacy helper will not "
        "mark uncertain/manual files as attached."
    )
    return UploadRecoveryResult(
        RecoveryAction.NEEDS_DECISION,
        report,
        attached,
        omitted,
        "Use the integrated upload workflow to choose retry, eligible inline fallback, or cancellation.",
    )
