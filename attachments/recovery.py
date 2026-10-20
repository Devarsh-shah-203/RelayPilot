"""User-facing recovery choices after browser file-upload limits/errors.

This module is intentionally separate from main.py. It can be integrated into the
request flow once the attachment subsystem itself has been validated live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .limits import UploadLimitKind, classify_upload_limit
from .manager import AttachmentManager
from .models import AttachmentRecord, AttachmentStatus, UploadReport


class RecoveryAction(str, Enum):
    RETRIED_INDIVIDUALLY = "RETRIED_INDIVIDUALLY"
    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"
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
    """Notify only when upload needs intervention, then honor the user's choice.

    Successful uploads remain silent. LIMIT_EXCEEDED states are not retried by
    AttachmentManager automatically. Batch-count errors may be retried one file
    at a time only after the user opts in. File-size and account/quota limits are
    not blindly retried because an individual retry usually cannot fix them.
    """
    limited = [record for record in report.records if record.status == AttachmentStatus.LIMIT_EXCEEDED]
    if not limited:
        attached = [record.path for record in report.records if record.status == AttachmentStatus.ATTACHED]
        omitted = [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED]
        if not omitted:
            return UploadRecoveryResult(RecoveryAction.CONTINUE_VERIFIED_ONLY, report, attached)
        return UploadRecoveryResult(
            RecoveryAction.NEEDS_DECISION, report, attached, omitted,
            "The report has unresolved files but no recognized upload-limit state.",
        )

    kinds = {record.path: classify_upload_limit(record.message) for record in limited}
    has_batch = any(kind == UploadLimitKind.BATCH_COUNT for kind in kinds.values())
    has_size = any(kind == UploadLimitKind.FILE_SIZE for kind in kinds.values())
    has_quota = any(kind == UploadLimitKind.PROVIDER_QUOTA for kind in kinds.values())

    output_fn("")
    output_fn("FILE UPLOAD LIMIT REACHED")
    output_fn("=" * 56)
    output_fn(f"Provider: {report.provider_name}")
    for record in limited:
        detail = record.message.strip() or "Provider reported an upload limit."
        output_fn(f"  - {record.path.name}: {detail}")

    if has_quota:
        output_fn("")
        output_fn("The provider may have reached an account/session upload quota.")
        output_fn("Retrying one by one may not help; the provider may need time or manual action.")
        options = {
            "1": "manual",
            "2": "verified",
            "3": "cancel",
        }
        output_fn("")
        output_fn("Choose an action:")
        output_fn("  [1] Handle uploads manually in the browser, then confirm")
        output_fn("  [2] Continue with successfully verified attachments only")
        output_fn("  [3] Cancel the request")
    elif has_size and not has_batch:
        output_fn("")
        output_fn("Uploading this file individually will not normally bypass a per-file size limit.")
        output_fn("Choose an action:")
        output_fn("  [1] Replace the oversized file with a supported version and attach it manually")
        output_fn("  [2] Continue with successfully verified attachments only")
        output_fn("  [3] Cancel the request")
        options = {"1": "manual", "2": "verified", "3": "cancel"}
    else:
        output_fn("")
        output_fn("Choose an action:")
        output_fn("  [1] Retry limit-affected files one by one")
        output_fn("  [2] Handle uploads manually in the browser, then confirm")
        output_fn("  [3] Continue with successfully verified attachments only")
        output_fn("  [4] Cancel the request")
        options = {"1": "individual", "2": "manual", "3": "verified", "4": "cancel"}

    try:
        choice = input_fn("Choice: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""

    action = options.get(choice)
    if action is None:
        output_fn("No valid choice selected. The request will not be submitted.")
        return UploadRecoveryResult(
            RecoveryAction.NEEDS_DECISION, report,
            [record.path for record in report.attached],
            [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED],
            "No valid recovery choice was selected.",
        )

    if action == "cancel":
        return UploadRecoveryResult(
            RecoveryAction.CANCELLED, report, [],
            [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED],
            "User cancelled after an upload limit.",
        )

    if action == "verified":
        verified = [record.path for record in report.attached]
        omitted = [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED]
        if not verified:
            output_fn("No files are verified as attached; continuing would send no selected file context.")
            output_fn("Choose manual handling or cancel instead.")
            return UploadRecoveryResult(
                RecoveryAction.NEEDS_DECISION, report, [], omitted,
                "No verified attachments are available to continue with.",
            )
        return UploadRecoveryResult(
            RecoveryAction.CONTINUE_VERIFIED_ONLY, report, verified, omitted,
            "User explicitly chose to continue with verified attachments only.",
        )

    if action == "manual":
        output_fn("")
        manual_candidates = [
            record for record in report.records
            if record.status in {
                AttachmentStatus.FAILED,
                AttachmentStatus.LIMIT_EXCEEDED,
                AttachmentStatus.UNKNOWN,
            }
        ]
        output_fn("Handle the listed files in the browser. Check each file card and wait")
        output_fn("until uploads are complete. Do not submit the chat message yourself.")
        if has_size and not has_batch:
            output_fn("For a replacement, keep the original filename so it matches the workspace reference.")
            output_fn("If you split one file into several new files, cancel this request and reference those files in a new prompt.")
        for record in manual_candidates:
            output_fn(f"  - {record.path.name}")
        try:
            confirmed = input_fn(
                "Type CONFIRMED only when every listed file is attached and ready: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            confirmed = ""
        if confirmed != "CONFIRMED":
            return UploadRecoveryResult(
                RecoveryAction.NEEDS_DECISION, report,
                [record.path for record in report.attached],
                [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED],
                "Manual attachment completion was not confirmed.",
            )
        # Explicit human confirmation is recorded separately from automated evidence.
        for record in manual_candidates:
            record.status = AttachmentStatus.ATTACHED
            record.verification_method = "user_confirmed_manual_upload"
            record.message = "User confirmed the file is attached and ready in the provider UI."
        omitted = [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED]
        if omitted:
            return UploadRecoveryResult(
                RecoveryAction.NEEDS_DECISION, report,
                [record.path for record in report.attached], omitted,
                "Some files remain unsupported or blocked and were not marked as attached.",
            )
        return UploadRecoveryResult(
            RecoveryAction.MANUAL_CONFIRMED,
            report,
            [record.path for record in report.records if record.status == AttachmentStatus.ATTACHED],
            [],
            "User confirmed manual attachment completion.",
        )

    # Only a batch-count limit is a plausible candidate for per-file retry.
    individually_retryable = [
        record for record in limited if kinds[record.path] == UploadLimitKind.BATCH_COUNT
    ]
    non_retryable = [record for record in limited if record not in individually_retryable]
    if not individually_retryable:
        output_fn("No limit-affected files can be safely retried individually based on the reported error.")
        return UploadRecoveryResult(
            RecoveryAction.NEEDS_DECISION, report,
            [record.path for record in report.attached],
            [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED],
            "The limit does not look like a per-batch file-count limit.",
        )

    # Warn before possible re-selection: the provider may have staged some cards
    # before displaying the batch limit error. The user explicitly opted in.
    output_fn("")
    output_fn("The provider may have staged some files before reporting the limit.")
    output_fn("Inspect/remove duplicate or failed cards in the browser if necessary.")
    for record in individually_retryable:
        one_report = manager.upload([record.path])
        retry = one_report.records[0] if one_report.records else None
        record.attempts += retry.attempts if retry else 1
        if retry is not None:
            record.status = retry.status
            record.message = retry.message
            record.verification_method = retry.verification_method
        else:
            record.status = AttachmentStatus.UNKNOWN
            record.message = "Retry returned no per-file result."
        if record.status == AttachmentStatus.LIMIT_EXCEEDED:
            # The one-file attempt shows this isn't fixed by reducing the batch.
            break

    # Do not convert unresolved limit records into successes.
    verified = [record.path for record in report.attached]
    omitted = [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED]
    if omitted:
        return UploadRecoveryResult(
            RecoveryAction.NEEDS_DECISION,
            report,
            verified,
            omitted,
            "Per-file retries finished, but unresolved files still require a user decision.",
        )
    return UploadRecoveryResult(
        RecoveryAction.RETRIED_INDIVIDUALLY,
        report,
        verified,
        [],
        "All files were attached successfully after per-file retry.",
    )
