"""Integration helpers for staging referenced workspace files as chat attachments.

The normal CLI remains responsible for assembling memory and dispatching the final
prompt. This module resolves file references, chooses inline vs attachment delivery,
and makes the attachment decision explicit before a prompt is submitted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from attachments.manager import AttachmentManager
from attachments.models import AttachmentStatus, DeliveryMode, UploadReport
from attachments.policy import FileKind, classify_file, choose_delivery_mode
from attachments.recovery import RecoveryAction, recover_upload_limit
from browser.providers.factory import UnsupportedAttachmentProvider, create_attachment_provider
from files.references import FileReferenceDetector


@dataclass(frozen=True)
class ResolvedFileReference:
    """One angle-bracket file reference and its validated local path."""

    raw_reference: str
    path: Path
    kind: FileKind


@dataclass
class AttachmentPlan:
    """Decision made before reading text content or touching the browser UI."""

    uses_attachments: bool = False
    references: list[ResolvedFileReference] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    reason: str = ""
    estimated_inline_characters: int = 0
    is_bulk_reference: bool = False


@dataclass
class UploadDecision:
    """Result of uploading attachments and resolving uncertain outcomes."""

    proceed: bool
    attached_paths: list[Path] = field(default_factory=list)
    omitted_paths: list[Path] = field(default_factory=list)
    report: UploadReport | None = None
    reason: str = ""


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _is_bulk_reference(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip().rstrip("/").casefold()
    return normalized == "all" or normalized.endswith("/all")


def plan_referenced_files(
    prompt: str,
    workspace_manager,
    *,
    memory_characters: int = 0,
    edit_mode: bool = False,
    force_attachments: bool | None = None,
) -> AttachmentPlan:
    """Resolve all normal file references and determine whether to upload them.

    Small text requests continue to use inline context by default. Documents,
    images and spreadsheets use attachments; larger text requests use the existing
    size-first policy. Set ``AI_GATEWAY_FORCE_ATTACHMENTS=1`` to exercise upload
    handling with a small text file during testing.

    Bulk ``<id/all>`` references remain on the existing inline collection path;
    this first integration handles explicit individual file references only.
    """

    parsed = FileReferenceDetector().detect(prompt)
    if not parsed:
        return AttachmentPlan(reason="No workspace file references found.")

    has_bulk = any(_is_bulk_reference(item.relative_path) for item in parsed)
    resolved: list[ResolvedFileReference] = []
    unique_paths: list[Path] = []
    seen: set[Path] = set()

    for reference in parsed:
        if _is_bulk_reference(reference.relative_path):
            continue
        raw = f"{reference.workspace_id}/{reference.relative_path}"
        path = workspace_manager.resolve_path(raw).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Referenced file does not exist: <{raw}>")
        if not path.is_file():
            raise ValueError(
                f"<{raw}> is not a file. Use a file reference, or the existing "
                "<workspace_id/folder/all> syntax for inline folder context."
            )
        kind = classify_file(path)
        if kind == FileKind.ARCHIVE:
            raise ValueError(
                f"Archive attachment is disabled by default: <{raw}>. "
                "Reference the files inside the archive individually instead."
            )
        resolved.append(ResolvedFileReference(raw, path, kind))
        if path not in seen:
            seen.add(path)
            unique_paths.append(path)

    # Leave folder-wide references on the pre-existing inline path. They are not
    # represented by one local file path, so attempting to upload "all" would be wrong.
    if has_bulk:
        return AttachmentPlan(
            uses_attachments=False,
            references=resolved,
            paths=unique_paths,
            reason="Bulk references use the existing inline file-context workflow.",
            is_bulk_reference=True,
        )

    if edit_mode:
        return AttachmentPlan(
            uses_attachments=False,
            references=resolved,
            paths=unique_paths,
            reason="Edit mode retains its current inline structured-edit workflow.",
        )

    forced = (
        _truthy_env(os.environ.get("AI_GATEWAY_FORCE_ATTACHMENTS"))
        if force_attachments is None
        else bool(force_attachments)
    )
    has_notebook = any(item.kind == FileKind.NOTEBOOK for item in resolved)
    has_non_notebook = any(item.kind != FileKind.NOTEBOOK for item in resolved)
    contains_non_text = any(
        item.kind not in {FileKind.TEXT, FileKind.NOTEBOOK}
        for item in resolved
    )
    inline_characters = sum(
        item.path.stat().st_size
        for item in resolved
        if item.kind in {FileKind.TEXT, FileKind.NOTEBOOK}
    )
    decision = choose_delivery_mode(
        prompt_chars=len(prompt),
        inline_file_chars=inline_characters,
        memory_chars=max(0, int(memory_characters)),
        file_count=len(unique_paths),
        contains_non_text_files=contains_non_text,
    )
    use_attachments = forced or decision.mode == DeliveryMode.ATTACHMENTS

    if has_notebook and use_attachments:
        if has_non_notebook:
            raise ValueError(
                "This request mixes a Jupyter notebook with files that need browser "
                "attachments. Submit the notebook and other files in separate requests; "
                "notebooks must use the existing cell-aware context workflow."
            )
        # Notebook files have their own cell-selection workflow and must never be
        # sent through the generic attachment adapter by this integration.
        use_attachments = False
        reason = "Jupyter notebooks stay on the existing cell-aware inline workflow."
    else:
        reason = "Forced attachment test mode is enabled." if forced else decision.reason

    return AttachmentPlan(
        uses_attachments=use_attachments,
        references=resolved,
        paths=unique_paths,
        reason=reason,
        estimated_inline_characters=inline_characters,
    )


def _resolve_non_limit_failures(
    report: UploadReport,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> UploadDecision:
    attached = list(report.attached)
    unresolved = [record for record in report.records if record.status != AttachmentStatus.ATTACHED]
    if not unresolved:
        return UploadDecision(True, [record.path for record in attached], [], report)

    output_fn("")
    output_fn("FILE UPLOAD NEEDS ATTENTION")
    output_fn("=" * 56)
    output_fn(f"Provider: {report.provider_name}")
    for record in unresolved:
        detail = record.message.strip() or record.status.value
        output_fn(f"  - {record.path.name}: {record.status.value} — {detail}")

    manual_candidates = [
        record for record in unresolved
        if record.status in {
            AttachmentStatus.FAILED,
            AttachmentStatus.UNKNOWN,
            AttachmentStatus.LIMIT_EXCEEDED,
        }
    ]
    if manual_candidates:
        output_fn("\nChoose an action:")
        output_fn("  [1] Handle eligible files manually in the browser, then confirm")
        if attached:
            output_fn("  [2] Continue with successfully verified attachments only")
            output_fn("  [3] Cancel the request")
        else:
            output_fn("  [2] Cancel the request")
        try:
            choice = input_fn("Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        manual_choice = "1"
        verified_choice = "2" if attached else None
        cancel_choice = "3" if attached else "2"

        if choice == manual_choice:
            output_fn("\nCheck each eligible file in the browser and wait for upload to finish:")
            for record in manual_candidates:
                output_fn(f"  - {record.path.name}")
            output_fn("Do not submit the chat message yourself.")
            try:
                confirmed = input_fn(
                    "Type CONFIRMED only when those files are attached and ready: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                confirmed = ""
            if confirmed != "CONFIRMED":
                output_fn("Manual upload was not confirmed. The request will not be submitted.")
                return UploadDecision(False, [r.path for r in attached], [r.path for r in unresolved], report,
                                      "Manual upload was not confirmed.")
            for record in manual_candidates:
                record.status = AttachmentStatus.ATTACHED
                record.verification_method = "user_confirmed_manual_upload"
                record.message = "User confirmed the file is attached and ready in the provider UI."
            confirmed_paths = [record.path for record in report.attached]
            omitted = [record.path for record in report.records if record.status != AttachmentStatus.ATTACHED]
            return UploadDecision(bool(confirmed_paths), confirmed_paths, omitted, report,
                                  "Eligible manual uploads were confirmed by the user.")

        if verified_choice and choice == verified_choice:
            return UploadDecision(True, [r.path for r in attached], [r.path for r in unresolved], report,
                                  "User chose to continue with verified attachments only.")
        if choice == cancel_choice:
            return UploadDecision(False, [r.path for r in attached], [r.path for r in unresolved], report,
                                  "User cancelled after an upload failure.")

        output_fn("No valid recovery choice was selected. The request will not be submitted.")
        return UploadDecision(False, [r.path for r in attached], [r.path for r in unresolved], report,
                              "No valid recovery choice was selected.")

    if attached:
        output_fn("\nSome referenced files are unsupported or blocked by the safety policy.")
        output_fn("Those files will not be marked as attached.")
        output_fn("  [1] Continue with successfully verified attachments only")
        output_fn("  [2] Cancel the request")
        try:
            choice = input_fn("Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice == "1":
            return UploadDecision(True, [r.path for r in attached], [r.path for r in unresolved], report,
                                  "User chose verified attachments only.")
    else:
        output_fn("No files are verified as attached. The request will not be submitted.")
    return UploadDecision(False, [], [r.path for r in unresolved], report,
                          "No verified attachment set was accepted.")


def upload_and_confirm(
    paths: Sequence[str | Path],
    page,
    *,
    provider=None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> UploadDecision:
    """Upload files, verify the UI state, and ask before proceeding on uncertainty."""

    normalized = list(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))
    if not normalized:
        return UploadDecision(False, reason="No files were selected for upload.")
    try:
        active_provider = provider or create_attachment_provider(page)
    except UnsupportedAttachmentProvider as error:
        output_fn(f"\nCannot upload attachments: {error}")
        return UploadDecision(False, omitted_paths=normalized, reason=str(error))

    manager = AttachmentManager(active_provider)
    report = manager.upload(normalized)

    if report.limited:
        recovery = recover_upload_limit(
            report,
            manager,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if recovery.action == RecoveryAction.CANCELLED:
            return UploadDecision(False, recovery.submit_paths, recovery.omitted_paths, report, recovery.message)
        if recovery.action == RecoveryAction.NEEDS_DECISION:
            # If a one-by-one retry did not resolve every item, let the user choose
            # manual handling or a verified-only subset. Never send unknown files.
            return _resolve_non_limit_failures(
                report, input_fn=input_fn, output_fn=output_fn
            )
        return UploadDecision(
            bool(recovery.submit_paths),
            list(dict.fromkeys(Path(path).resolve() for path in recovery.submit_paths)),
            list(dict.fromkeys(Path(path).resolve() for path in recovery.omitted_paths)),
            report,
            recovery.message,
        )

    return _resolve_non_limit_failures(report, input_fn=input_fn, output_fn=output_fn)


def build_attachment_prompt(
    original_prompt: str,
    references: Sequence[ResolvedFileReference],
    attached_paths: Sequence[str | Path],
    report: UploadReport | None = None,
) -> str:
    """Tell the model exactly which selected workspace files are attached.

    Absolute local paths are intentionally not included in the prompt. A reference
    that was not uploaded is explicitly marked as omitted rather than implied to be
    available in the current chat.
    """

    attached = {Path(path).expanduser().resolve() for path in attached_paths}
    rewritten = original_prompt
    listed: list[str] = []
    seen: set[Path] = set()
    records_by_path = {
        Path(record.path).expanduser().resolve(): record
        for record in (report.records if report is not None else [])
    }
    for item in references:
        marker = f"<{item.raw_reference}>"
        if item.path in attached:
            replacement = f'attached file "{item.path.name}"'
            if item.path not in seen:
                record = records_by_path.get(item.path)
                if record and record.verification_method == "user_confirmed_manual_upload":
                    evidence = "manually confirmed by the user"
                else:
                    evidence = "verified in the provider UI"
                listed.append(
                    f"- `{item.path.name}` (reference: `<{item.raw_reference}>`; {evidence})"
                )
                seen.add(item.path)
        else:
            replacement = f'[file `{item.path.name}` was not attached and is unavailable to this request]'
        rewritten = rewritten.replace(marker, replacement)

    if not listed:
        raise ValueError("No attached files remain after upload recovery; refusing to submit the request.")

    manifest = (
        "FILES ATTACHED TO THIS CHAT\n"
        "============================\n"
        "The following files are attached to this chat. Their state was checked in "
        "the provider UI or explicitly confirmed by the user during manual recovery. "
        "Use the actual attached files as the source for the user's task. Do not attempt "
        "to access the user's local filesystem.\n"
        + "\n".join(listed)
    )
    return f"{rewritten.strip()}\n\n{manifest}"
