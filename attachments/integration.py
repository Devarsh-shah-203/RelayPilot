"""Workspace reference planning, browser uploads, recovery and request assembly.

This is the only orchestration layer between file references and the browser chat
transport. Provider adapters stage files; ``main.py`` remains responsible for one
final ``chat.send_prompt`` call after this module has verified the delivery plan.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from attachments.limits import UploadLimitKind, classify_upload_limit
from attachments.manager import AttachmentManager
from attachments.models import AttachmentRecord, AttachmentStatus, UploadReport
from attachments.policy import (
    EXCLUDED_DIRECTORY_NAMES,
    FileKind,
    attachment_character_threshold,
    attachment_size_threshold_bytes,
    bulk_max_bytes,
    bulk_max_files,
    classify_file,
    estimate_tokens,
    exclusion_reason,
    inline_token_threshold,
    is_inline_fallback_eligible,
)
from browser.providers.factory import UnsupportedAttachmentProvider, create_attachment_provider
from files.references import FILE_REFERENCE_PATTERN, FileReferenceDetector


_REFERENCE_RE = re.compile(r"^\s*(\d+)\s*/\s*(.+?)\s*$")
_INLINE_OVERHEAD_CHARS = 1_000
_INLINE_PER_FILE_OVERHEAD_CHARS = 250


@dataclass(frozen=True)
class SourceReference:
    """One marker originally typed by the user, such as ``1/all``."""

    key: str
    display_reference: str
    workspace_id: int
    relative_path: str
    is_bulk: bool


@dataclass(frozen=True)
class ResolvedFileReference:
    """One concrete workspace file generated from an explicit or bulk marker."""

    raw_reference: str  # concrete path in <workspace_id/relative/path> form
    path: Path
    kind: FileKind
    source_reference: str = ""  # normalized marker originally typed by the user
    character_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())
        if not self.source_reference:
            object.__setattr__(self, "source_reference", self.raw_reference)


@dataclass
class AttachmentPlan:
    """Delivery plan created before any browser file selection occurs."""

    uses_attachments: bool = False
    references: list[ResolvedFileReference] = field(default_factory=list)
    # ``paths`` remains the complete, deduplicated selection for compatibility.
    paths: list[Path] = field(default_factory=list)
    reason: str = ""
    estimated_inline_characters: int = 0
    is_bulk_reference: bool = False
    attachment_paths: list[Path] = field(default_factory=list)
    inline_paths: list[Path] = field(default_factory=list)
    source_references: dict[str, SourceReference] = field(default_factory=dict)
    skipped_count: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    token_threshold: int = 12_000

    @property
    def inline_fallback_candidates(self) -> list[Path]:
        """Only TXT and source/programming-language files may use text fallback."""
        return [path for path in self.attachment_paths if is_inline_fallback_eligible(path)]


@dataclass
class UploadDecision:
    """Outcome of attachment delivery and any user-selected fallback."""

    proceed: bool
    attached_paths: list[Path] = field(default_factory=list)
    omitted_paths: list[Path] = field(default_factory=list)
    report: UploadReport | None = None
    reason: str = ""
    inline_paths: list[Path] = field(default_factory=list)


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _normalize_reference(workspace_id: int, relative_path: str) -> str:
    relative = relative_path.strip().replace("\\", "/").strip("/")
    return f"{int(workspace_id)}/{relative}"


def _split_reference_payload(payload: str) -> tuple[int, str]:
    match = _REFERENCE_RE.match(payload)
    if not match:
        raise ValueError(f"Invalid workspace file reference: <{payload}>.")
    workspace_id = int(match.group(1))
    relative = match.group(2).strip().replace("\\", "/").strip("/")
    if not relative:
        raise ValueError(f"File path in reference <{payload}> cannot be empty.")
    return workspace_id, relative


def _is_bulk_reference(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip().strip("/").casefold()
    return normalized == "all" or normalized.endswith("/all")


def _count_text_characters(path: Path, threshold: int | None = None) -> int:
    """Count UTF-8 characters, stopping just above a policy threshold when given."""
    count = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            while True:
                remaining = None if threshold is None else threshold + 1 - count
                if remaining is not None and remaining <= 0:
                    return threshold + 1
                chunk = stream.read(8192 if remaining is None else min(8192, remaining))
                if not chunk:
                    return count
                count += len(chunk)
                if threshold is not None and count > threshold:
                    return threshold + 1
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Could not safely read text file {path.name!r} as UTF-8: {error}") from error


def _safe_read_text(path: Path, *, max_characters: int | None = None) -> str | None:
    """Read a UTF-8 text file only when it fits the supplied character budget."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            pieces: list[str] = []
            total = 0
            while True:
                read_size = 8192 if max_characters is None else min(8192, max_characters + 1 - total)
                if read_size <= 0:
                    return None
                chunk = stream.read(read_size)
                if not chunk:
                    return "".join(pieces)
                pieces.append(chunk)
                total += len(chunk)
                if max_characters is not None and total > max_characters:
                    return None
    except (OSError, UnicodeError):
        return None


def _source_code_or_text_kind(path: Path) -> FileKind:
    return classify_file(path)


def _workspace_root(workspace_manager, workspace_id: int) -> Path:
    root = workspace_manager.resolve_path(f"{workspace_id}/.").expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Workspace #{workspace_id} is not an available directory.")
    return root


def _record_skip(skipped_reasons: dict[str, int], reason: str) -> None:
    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1


def _request_metadata_overhead(plan_paths: Sequence[Path], references: Sequence[ResolvedFileReference]) -> int:
    """Estimate request/manifest overhead for every selected file, including attachments.

    Attachment names still consume prompt tokens even when their contents are not
    inline. Inline references appear in both the manifest and PROJECT FILES section,
    so the estimate reserves extra room for paths and filenames instead of reducing
    overhead as files move from inline to attachments.
    """
    reference_by_path: dict[Path, str] = {}
    for reference in references:
        reference_by_path.setdefault(reference.path.resolve(), reference.raw_reference)
    overhead = _INLINE_OVERHEAD_CHARS
    for raw_path in plan_paths:
        path = Path(raw_path).resolve()
        reference = reference_by_path.get(path, path.name)
        overhead += _INLINE_PER_FILE_OVERHEAD_CHARS + 2 * len(reference) + len(path.name)
    return overhead


def _collect_bulk_files(
    workspace_manager,
    workspace_id: int,
    relative_directory: str,
    skipped_reasons: dict[str, int],
) -> list[tuple[Path, FileKind, str]]:
    """Collect a deterministic, bounded list of safe files under a workspace root."""
    workspace_root = _workspace_root(workspace_manager, workspace_id)
    scope_path = relative_directory.strip().replace("\\", "/").strip("/")
    collection_root = (
        workspace_manager.resolve_path(f"{workspace_id}/{scope_path or '.'}")
        .expanduser()
        .resolve()
    )
    try:
        collection_root.relative_to(workspace_root)
    except ValueError as error:
        raise ValueError("Bulk file reference resolves outside the configured workspace.") from error
    if not collection_root.exists() or not collection_root.is_dir():
        raise ValueError(f"Bulk file selection is not a directory: <{workspace_id}/{scope_path or 'all'}>.")

    # A user may explicitly aim at a generated directory; keep the exclusion policy
    # consistent with recursive traversal instead of turning that into a bypass.
    scope_parts = collection_root.relative_to(workspace_root).parts
    if any(part.casefold() in EXCLUDED_DIRECTORY_NAMES for part in scope_parts):
        raise ValueError("Bulk selection points inside a generated/private directory that is excluded by policy.")

    collected: list[tuple[Path, FileKind, str]] = []
    total_bytes = 0
    for current, directory_names, filenames in os.walk(collection_root, topdown=True, followlinks=False):
        current_path = Path(current)
        filtered_dirs: list[str] = []
        for name in sorted(directory_names, key=lambda item: (item.casefold(), item)):
            child = current_path / name
            if name.casefold() in EXCLUDED_DIRECTORY_NAMES:
                _record_skip(skipped_reasons, "generated/private directory")
                continue
            if child.is_symlink():
                _record_skip(skipped_reasons, "symbolic link")
                continue
            filtered_dirs.append(name)
        directory_names[:] = filtered_dirs

        for name in sorted(filenames, key=lambda item: (item.casefold(), item)):
            path = current_path / name
            try:
                if path.is_symlink() or not path.is_file():
                    _record_skip(skipped_reasons, "symbolic link/non-regular file")
                    continue
                resolved = path.resolve()
                resolved.relative_to(workspace_root)
            except (OSError, ValueError):
                _record_skip(skipped_reasons, "path outside workspace or inaccessible")
                continue

            reason = exclusion_reason(resolved, root=workspace_root)
            if reason:
                _record_skip(skipped_reasons, "safety/unsupported file")
                continue
            kind = _source_code_or_text_kind(resolved)
            if kind == FileKind.NOTEBOOK:
                _record_skip(skipped_reasons, "Jupyter notebook (use an explicit notebook reference)")
                continue
            if kind not in {FileKind.TEXT, FileKind.DOCUMENT, FileKind.IMAGE, FileKind.SPREADSHEET, FileKind.ARCHIVE}:
                _record_skip(skipped_reasons, "unsupported/binary file")
                continue

            try:
                size = resolved.stat().st_size
            except OSError:
                _record_skip(skipped_reasons, "inaccessible file")
                continue
            total_bytes += size
            collected.append((resolved, kind, resolved.relative_to(workspace_root).as_posix()))
            if len(collected) > bulk_max_files():
                raise ValueError(
                    f"Bulk selection exceeds RELAYPILOT_BULK_MAX_FILES={bulk_max_files()}. "
                    "Narrow the reference to a smaller folder. No files were uploaded."
                )
            if total_bytes > bulk_max_bytes():
                raise ValueError(
                    f"Bulk selection exceeds RELAYPILOT_BULK_MAX_BYTES={bulk_max_bytes()}. "
                    "Narrow the reference to a smaller folder. No files were uploaded."
                )

    collected.sort(key=lambda item: (item[2].casefold(), item[2]))
    return collected


def _character_count_for_policy(path: Path, kind: FileKind) -> int | None:
    if kind != FileKind.TEXT:
        return None
    threshold = attachment_character_threshold()
    try:
        size = path.stat().st_size
    except OSError:
        return threshold + 1
    # Byte count is a safe conservative trigger: UTF-8 character count cannot exceed
    # byte count for valid UTF-8 text, so there is no need to read a large file.
    if size > attachment_size_threshold_bytes():
        return threshold + 1
    try:
        return _count_text_characters(path, threshold=threshold)
    except ValueError:
        # Non-UTF-8 text remains eligible for browser attachment but cannot safely be
        # put inline by FileReader's UTF-8 contract.
        return threshold + 1


def plan_referenced_files(
    prompt: str,
    workspace_manager,
    *,
    memory_characters: int = 0,
    edit_mode: bool = False,
    force_attachments: bool | None = None,
) -> AttachmentPlan:
    """Resolve and vet references; upload-first even in structured edit mode.

    The legacy ``force_attachments`` parameter remains for call compatibility,
    but upload-first is now the default. Inline source delivery is only a user-
    approved recovery option after upload verification fails.
    """
    parsed = FileReferenceDetector().detect(prompt)
    threshold = inline_token_threshold()
    if not parsed:
        return AttachmentPlan(token_threshold=threshold, reason="No workspace file references found.")

    references: list[ResolvedFileReference] = []
    all_paths: list[Path] = []
    path_kinds: dict[Path, FileKind] = {}
    path_chars: dict[Path, int | None] = {}
    seen_paths: set[Path] = set()
    source_refs: dict[str, SourceReference] = {}
    skipped_reasons: dict[str, int] = {}
    has_bulk = False

    for detected in parsed:
        workspace_id = detected.workspace_id
        relative = detected.relative_path.strip().replace("\\", "/").strip("/")
        key = _normalize_reference(workspace_id, relative)
        is_bulk = _is_bulk_reference(relative)
        source = SourceReference(key, key, workspace_id, relative, is_bulk)
        source_refs[key] = source

        if is_bulk:
            if edit_mode:
                raise ValueError(
                    "Bulk references are disabled in EDIT MODE. Select files explicitly so each proposed edit can be reviewed safely."
                )
            has_bulk = True
            scope = re.sub(r"(?:^|/)all/?$", "", relative, flags=re.IGNORECASE).strip("/")
            expanded = _collect_bulk_files(workspace_manager, workspace_id, scope, skipped_reasons)
            if not expanded:
                raise ValueError(
                    f"No eligible files were found for <{key}>. Unsupported, sensitive, generated, and notebook files are excluded from bulk selection."
                )
            for path, kind, root_relative in expanded:
                concrete = _normalize_reference(workspace_id, root_relative)
                char_count = _character_count_for_policy(path, kind)
                references.append(ResolvedFileReference(concrete, path, kind, key, char_count))
                if path not in seen_paths:
                    seen_paths.add(path)
                    all_paths.append(path)
                    path_kinds[path] = kind
                    path_chars[path] = char_count
            continue

        path = workspace_manager.resolve_path(key).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Referenced file does not exist: <{key}>")
        if not path.is_file():
            raise ValueError(f"<{key}> is not a file. Use <workspace_id/folder/all> for a directory selection.")
        kind = classify_file(path)
        workspace_root = _workspace_root(workspace_manager, workspace_id)
        reason = exclusion_reason(path, root=workspace_root)
        if reason and not (kind == FileKind.NOTEBOOK and reason == "notebook files use the existing cell-aware context workflow"):
            raise ValueError(f"Cannot use <{key}>: {reason}.")
        if kind == FileKind.UNKNOWN:
            raise ValueError(f"Cannot use <{key}>: file type is binary or unsupported by the default policy.")
        if edit_mode and kind not in {FileKind.TEXT, FileKind.NOTEBOOK}:
            raise ValueError("EDIT MODE supports text/source files and cell-aware notebooks only; use a normal prompt for documents/images/archives.")

        char_count = _character_count_for_policy(path, kind)
        references.append(ResolvedFileReference(key, path, kind, key, char_count))
        if path not in seen_paths:
            seen_paths.add(path)
            all_paths.append(path)
            path_kinds[path] = kind
            path_chars[path] = char_count

    if not all_paths:
        raise ValueError("File references did not resolve to any usable files.")

    # Option B: every eligible explicitly referenced file is offered to the
    # browser uploader, in normal AND edit mode, regardless of size. A notebook
    # is also an explicit upload candidate; bulk notebook inclusion remains
    # disallowed by the safety policy above.
    #
    # Inline delivery is strictly opt-in recovery after upload failure. We still
    # account for the request's own prompt, memory and filename manifest here.
    metadata_overhead = _request_metadata_overhead(all_paths, references)
    if estimate_tokens(len(prompt) + max(0, int(memory_characters)) + metadata_overhead)[1] > threshold:
        raise ValueError(
            "The prompt, enabled memory and referenced-file manifest exceed the configured "
            "request budget. Narrow the request before uploading. No file was selected."
        )

    return AttachmentPlan(
        uses_attachments=True,
        references=references,
        paths=all_paths,
        reason="Explicit workspace references use upload-first delivery in all modes.",
        estimated_inline_characters=0,
        is_bulk_reference=has_bulk,
        attachment_paths=list(all_paths),
        inline_paths=[],
        source_references=source_refs,
        skipped_count=sum(skipped_reasons.values()),
        skipped_reasons=skipped_reasons,
        token_threshold=threshold,
    )



def inline_fallback_base_characters(prompt: str, plan: AttachmentPlan, memory_characters: int = 0) -> int:
    """Conservative size estimate including manifests for both attached and inline files."""
    return (
        len(prompt)
        + max(0, int(memory_characters))
        + max(0, int(plan.estimated_inline_characters))
        + _request_metadata_overhead(plan.paths, plan.references)
    )

def _report_record_map(report: UploadReport | None) -> dict[Path, AttachmentRecord]:
    return {
        Path(record.path).expanduser().resolve(): record
        for record in (report.records if report is not None else [])
    }


def _inline_candidate_text(path: Path, max_characters: int) -> str | None:
    if not is_inline_fallback_eligible(path):
        return None
    # This bounded reader refuses a text fallback that cannot fit in the current
    # inline budget. It does not truncate large source files silently.
    return _safe_read_text(path, max_characters=max_characters)


def _choose_inline_fallback(
    candidates: Sequence[Path],
    *,
    inline_base_characters: int,
    token_threshold: int,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    inline_payloads: dict[Path, str] | None = None,
    quota_text_paths: set[Path] | None = None,
) -> list[Path] | None:
    """Select complete, budgeted source or prepared notebook text (never truncated)."""
    max_total_chars = max(0, token_threshold * 3 - max(0, inline_base_characters))
    if max_total_chars <= 0:
        output_fn("The current prompt and inline context leave no room for fallback text under the configured token budget.")
        return None

    candidate_texts: dict[Path, str] = {}
    too_large: list[Path] = []
    for raw_path in candidates:
        path = Path(raw_path).expanduser().resolve()
        override = (inline_payloads or {}).get(path)
        regular_text = is_inline_fallback_eligible(path)
        quota_text = path in (quota_text_paths or set()) and classify_file(path) == FileKind.TEXT and exclusion_reason(path) is None
        if override is None and not (regular_text or quota_text):
            continue
        text = override if override is not None and len(override) <= max_total_chars else (
            _safe_read_text(path, max_characters=max_total_chars) if quota_text else
            _inline_candidate_text(path, max_total_chars) if override is None else None
        )
        if text is None:
            too_large.append(path)
        else:
            candidate_texts[path] = text

    if too_large:
        output_fn("Not eligible for inline fallback (unsupported encoding or too large for the remaining budget):")
        for path in too_large:
            output_fn(f"  - {path.name}")
    if not candidate_texts:
        output_fn("No failed TXT/source-code files can fit in the inline fallback budget.")
        return None

    ordered = list(candidate_texts)
    output_fn("\nEligible inline fallback files:")
    for index, path in enumerate(ordered, start=1):
        output_fn(f"  [{index}] {path.name} ({len(candidate_texts[path]):,} characters)")
    output_fn("Fallback is available only for complete, approved text/source content or selected notebook cell context. No content is truncated.")

    while True:
        try:
            answer = input_fn("Select file numbers separated by commas, or 0 to cancel the request: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer == "0" or answer.casefold() in {"cancel", "c"}:
            return None
        try:
            indexes = [int(piece.strip()) for piece in answer.split(",") if piece.strip()]
        except ValueError:
            output_fn("Enter comma-separated numbers from the list, or 0 to cancel.")
            continue
        if not indexes or len(indexes) != len(set(indexes)) or any(index < 1 or index > len(ordered) for index in indexes):
            output_fn("Selection is empty, duplicated, or outside the listed range. Try again.")
            continue
        selected = [ordered[index - 1] for index in indexes]
        selected_chars = sum(len(candidate_texts[path]) for path in selected)
        estimated_total = inline_base_characters + selected_chars + _INLINE_PER_FILE_OVERHEAD_CHARS * len(selected)
        if estimate_tokens(estimated_total)[1] > token_threshold:
            output_fn("That selection exceeds the configured inline token budget. Select fewer/smaller files or enter 0 to cancel.")
            continue
        return selected


def _settle_upload_failure(
    report: UploadReport,
    manager: AttachmentManager,
    *,
    fallback_candidates: Sequence[str | Path],
    inline_base_characters: int,
    token_threshold: int,
    allow_retry: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    inline_payloads: dict[Path, str] | None = None,
    allow_notebook_reselect: bool = False,
) -> UploadDecision:
    """Never submit a missing/unknown attachment without an explicit decision.

    UNKNOWN files may already be staged. They cannot be skipped or inlined
    safely until the user has cleared any staged card in the actual browser.
    """
    attached = [Path(r.path).resolve() for r in report.attached]
    unresolved = [r for r in report.records if r.status != AttachmentStatus.ATTACHED]
    paths = [Path(r.path).resolve() for r in unresolved]
    uncertain = [r for r in unresolved if r.status == AttachmentStatus.UNKNOWN]
    has_limit = any(r.status == AttachmentStatus.LIMIT_EXCEEDED for r in unresolved)
    quota_text_paths = {
        Path(record.path).resolve() for record in unresolved
        if record.status == AttachmentStatus.LIMIT_EXCEEDED
        and classify_file(record.path) == FileKind.TEXT
        and exclusion_reason(record.path) is None
    }
    allowed_for_inline = {
        Path(path).resolve() for path in fallback_candidates
        if is_inline_fallback_eligible(path) or Path(path).resolve() in (inline_payloads or {})
        or Path(path).resolve() in quota_text_paths
    }
    fallback_paths = [
        Path(r.path).resolve() for r in unresolved
        if Path(r.path).resolve() in allowed_for_inline
    ]

    output_fn("Attachment upload needs your decision (the prompt has NOT been sent):")
    for record in unresolved:
        output_fn(f"  - {record.path.name}: {record.status.value} — {record.message}")
    if has_limit:
        messages = [record.message for record in unresolved if record.status == AttachmentStatus.LIMIT_EXCEEDED]
        if any(classify_upload_limit(message) == UploadLimitKind.PROVIDER_QUOTA for message in messages):
            output_fn("UPLOAD QUOTA REACHED: ChatGPT reports exhausted uploads. Choose an eligible inline fallback; do not retry until the quota resets.")
        else:
            output_fn("UPLOAD LIMIT DETECTED: a file-size or batch limit was reported. This is not necessarily an account upload quota.")
    if uncertain:
        output_fn(
            "UNKNOWN means a file may already be present in the composer. "
            "Check Chrome and remove any unresolved attachment cards before retrying. "
            "RelayPilot will not silently skip such a file and send it anyway."
        )

    retry_paths = [r for r in unresolved if r.status in {AttachmentStatus.FAILED, AttachmentStatus.UNKNOWN}]
    choices: list[tuple[str, str]] = []
    if allow_retry and retry_paths and not has_limit:
        choices.append(("retry", "Retry upload once (after checking the browser for any UNKNOWN files)"))
    if fallback_paths:
        choices.append(("inline", "Append eligible source/TXT or selected notebook contents to the prompt"))
    if allow_notebook_reselect and (has_limit or any(r.status == AttachmentStatus.UNSUPPORTED for r in unresolved)) and not uncertain:
        choices.append(("reselect", "Choose fewer notebook cells/outputs (retry selection)"))
    choices.append(("skip", "Skip the unresolved files and send the prompt without them"))
    choices.append(("cancel", "Cancel this request"))

    output_fn("Choose how to proceed:")
    for index, (_, label) in enumerate(choices, 1):
        output_fn(f"  [{index}] {label}")
    try:
        answer = input_fn("Choice: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    # A Textual choice with a checked cleanup acknowledgement can authorize the
    # retry in one panel. Terminal-only callers can still select 1 and type
    # RETRY (case-insensitively) as a conservative compatibility fallback.
    from attachments.recovery_ui_protocol import decode_recovery_choice
    choice_number, cleanup_checked = decode_recovery_choice(answer)
    selected = next(
        (key for n, (key, _) in enumerate(choices, 1)
         if choice_number == str(n) or choice_number.casefold() == key),
        "cancel",
    )

    if selected in {"retry", "inline", "skip"} and uncertain and not cleanup_checked:
        # A UI checkbox is enough. In plain CLI mode, the separate text
        # confirmation remains available as a conservative fallback.
        confirmation = input_fn(
            "Remove staged copies of unresolved files in Chrome, then type CLEAN to confirm: "
        ).strip()
        if confirmation.casefold() not in {"clean", "retry"}:
            return UploadDecision(False, attached, paths, report, "Uncertain browser staging was not cleared.")
        cleanup_checked = True

    if selected == "retry":
        retry_report = manager.upload(
            [Path(r.path).resolve() for r in retry_paths],
            retry_failed=False,
            preflight=True,
        )
        retry_map = {Path(item.path).resolve(): item for item in retry_report.records}
        for record in report.records:
            retry = retry_map.get(Path(record.path).resolve())
            if retry is not None:
                record.attempts += retry.attempts
                record.status = retry.status
                record.message = retry.message
                record.verification_method = retry.verification_method
        if report.all_required_attached:
            return UploadDecision(
                True, [Path(r.path).resolve() for r in report.attached], [], report,
                "All files verified after user-authorized retry."
            )
        return _settle_upload_failure(
            report, manager, fallback_candidates=fallback_candidates,
            inline_base_characters=inline_base_characters,
            token_threshold=token_threshold, allow_retry=False,
            input_fn=input_fn, output_fn=output_fn,
            inline_payloads=inline_payloads,
            allow_notebook_reselect=allow_notebook_reselect,
        )

    if selected == "inline":
        selected_inline = _choose_inline_fallback(
            fallback_paths,
            inline_base_characters=inline_base_characters,
            token_threshold=token_threshold,
            input_fn=input_fn, output_fn=output_fn,
            inline_payloads=inline_payloads,
            quota_text_paths=quota_text_paths,
        )
        if selected_inline:
            chosen = {Path(p).resolve() for p in selected_inline}
            return UploadDecision(
                True, attached, [p for p in paths if p not in chosen], report,
                "User authorized eligible source/TXT inline fallback.", selected_inline,
            )

    if selected == "reselect":
        return UploadDecision(False, attached, paths, report, "RESELECT_NOTEBOOK")

    if selected == "skip":
        # Explicitly permits sending a no-file prompt when every selected upload
        # failed. The delivery manifest identifies every omitted reference.
        return UploadDecision(
            True, attached, paths, report,
            "User explicitly omitted unresolved files; no content was inlined.",
        )

    output_fn("Request cancelled. No prompt was submitted.")
    return UploadDecision(False, attached, paths, report, "User cancelled attachment delivery.")


def upload_and_confirm(
    paths: Sequence[str | Path],
    page,
    *,
    fallback_candidates: Sequence[str | Path] = (),
    inline_base_characters: int = 0,
    token_threshold: int | None = None,
    provider=None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    inline_payloads: dict[Path, str] | None = None,
    allow_notebook_reselect: bool = False,
) -> UploadDecision:
    """Upload, verify per file, and invoke only the recovery decisions needed."""
    normalized = list(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))
    if not normalized:
        return UploadDecision(False, reason="No files were selected for upload.")
    active_threshold = inline_token_threshold() if token_threshold is None else max(1, int(token_threshold))
    try:
        active_provider = provider or create_attachment_provider(page)
    except UnsupportedAttachmentProvider as error:
        output_fn(f"\nCannot upload attachments: {error}")
        return UploadDecision(False, omitted_paths=normalized, reason=str(error))

    manager = AttachmentManager(active_provider, max_retries=1)
    report = manager.upload(normalized, retry_failed=False)

    unresolved = [record for record in report.records if record.status != AttachmentStatus.ATTACHED]
    if not unresolved:
        return UploadDecision(
            True,
            attached_paths=[Path(record.path).resolve() for record in report.attached],
            report=report,
        )

    return _settle_upload_failure(
        report,
        manager,
        fallback_candidates=[Path(path).expanduser().resolve() for path in fallback_candidates],
        inline_base_characters=max(0, int(inline_base_characters)),
        token_threshold=active_threshold,
        allow_retry=True,
        input_fn=input_fn,
        output_fn=output_fn,
        inline_payloads={Path(p).resolve(): text for p, text in (inline_payloads or {}).items()},
        allow_notebook_reselect=allow_notebook_reselect,
    )


def _rewrite_prompt_references(
    original_prompt: str,
    plan: AttachmentPlan,
    attached_paths: Sequence[str | Path],
    inline_paths: Sequence[str | Path],
    omitted_paths: Sequence[str | Path],
    report: UploadReport | None,
) -> str:
    attached = {Path(path).expanduser().resolve() for path in attached_paths}
    inline = {Path(path).expanduser().resolve() for path in inline_paths}
    omitted = {Path(path).expanduser().resolve() for path in omitted_paths}
    refs_by_source: dict[str, list[ResolvedFileReference]] = {}
    for reference in plan.references:
        refs_by_source.setdefault(reference.source_reference, []).append(reference)

    def replacement(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        try:
            workspace_id, relative = _split_reference_payload(raw)
        except ValueError:
            return match.group(0)
        key = _normalize_reference(workspace_id, relative)
        source = plan.source_references.get(key)
        refs = refs_by_source.get(key, [])
        if source and source.is_bulk:
            return f"the eligible files selected from workspace reference {source.display_reference}"
        if not refs:
            return f"[file reference {key} was not resolved and is unavailable]"
        reference = refs[0]
        path = reference.path
        if path in attached:
            return f'attached file "{path.name}"'
        if path in inline:
            return f'inline file "{path.name}" (contents included below)'
        if path in omitted or path not in attached | inline:
            return f'[file "{path.name}" was not delivered and is unavailable to this request]'
        return match.group(0)

    return FILE_REFERENCE_PATTERN.sub(replacement, original_prompt).strip()


def build_delivery_prompt(
    original_prompt: str,
    plan: AttachmentPlan,
    *,
    attached_paths: Sequence[str | Path] = (),
    inline_paths: Sequence[str | Path] = (),
    omitted_paths: Sequence[str | Path] = (),
    report: UploadReport | None = None,
    display_filenames: dict[Path, str] | None = None,
) -> str:
    """Rewrite reference markers and accurately describe delivered file context."""
    attached = list(dict.fromkeys(Path(path).expanduser().resolve() for path in attached_paths))
    inline = list(dict.fromkeys(Path(path).expanduser().resolve() for path in inline_paths))
    omitted = list(dict.fromkeys(Path(path).expanduser().resolve() for path in omitted_paths))
    rewritten = _rewrite_prompt_references(original_prompt, plan, attached, inline, omitted, report)

    sections: list[str] = []
    records_by_path = _report_record_map(report)
    reference_by_path: dict[Path, str] = {}
    for reference in plan.references:
        reference_by_path.setdefault(reference.path.resolve(), reference.raw_reference)

    if attached:
        attached_lines: list[str] = []
        for path in attached:
            record = records_by_path.get(path)
            method = record.verification_method if record else "provider UI verification"
            if method == "user_confirmed_manual_upload":
                # Kept for compatibility with older reports; current recovery flow
                # does not silently convert uncertain files into successes.
                evidence = "manually confirmed by the user"
            else:
                evidence = "verified in the provider UI"
            display_ref = reference_by_path.get(path, path.name)
            displayed = (display_filenames or {}).get(path, path.name)
            attached_lines.append(f"- `{display_ref}` ({evidence}; filename: `{displayed}`)")
        sections.append(
            "FILES ATTACHED TO THIS CHAT\n"
            "============================\n"
            "Use only these actual browser attachments as attached-file sources. Do not try to access the user's local filesystem.\n"
            + "\n".join(attached_lines)
        )
    if inline:
        sections.append(
            "FILES INCLUDED INLINE\n"
            "=====================\n"
            "The complete selected text/source contents for these files are included in the PROJECT FILES section below:\n"
            + "\n".join(
                f"- `{reference_by_path.get(path, path.name)}` (filename: `{path.name}`)"
                for path in inline
            )
        )
    if omitted:
        sections.append(
            "FILES NOT DELIVERED\n"
            "===================\n"
            "These selected files are unavailable to this request. Do not infer or claim to have read them:\n"
            + "\n".join(
                f"- `{reference_by_path.get(path, path.name)}` (filename: `{path.name}`)"
                for path in omitted
            )
        )
    if plan.is_bulk_reference:
        sections.append(
            "BULK SELECTION POLICY\n"
            "=====================\n"
            "Bulk references expand only to eligible files within the configured workspace. Sensitive, generated, unsupported, binary, and notebook files are not implicitly included."
        )
    if sections:
        rewritten = f"{rewritten}\n\n" + "\n\n".join(sections)
    return rewritten.strip()


def build_attachment_prompt(
    original_prompt: str,
    references: Sequence[ResolvedFileReference],
    attached_paths: Sequence[str | Path],
    report: UploadReport | None = None,
) -> str:
    """Backward-compatible helper used by existing focused tests/callers."""
    path_set = {Path(path).expanduser().resolve() for path in attached_paths}
    all_paths = list(dict.fromkeys(Path(reference.path).resolve() for reference in references))
    omitted = [path for path in all_paths if path not in path_set]
    source_refs: dict[str, SourceReference] = {}
    for reference in references:
        key = reference.source_reference or reference.raw_reference
        workspace_id, relative = _split_reference_payload(key)
        source_refs.setdefault(key, SourceReference(key, key, workspace_id, relative, _is_bulk_reference(relative)))
    plan = AttachmentPlan(
        references=list(references), paths=all_paths,
        source_references=source_refs, is_bulk_reference=any(source.is_bulk for source in source_refs.values()),
    )
    return build_delivery_prompt(
        original_prompt, plan, attached_paths=list(path_set), omitted_paths=omitted, report=report
    )


def build_inline_context(file_context_builder, plan: AttachmentPlan, paths: Sequence[str | Path]):
    """Build standard file contexts for chosen inline files without re-expanding all markers."""
    selected = {Path(path).expanduser().resolve() for path in paths}
    by_path: dict[Path, ResolvedFileReference] = {}
    for reference in plan.references:
        if reference.path in selected:
            by_path.setdefault(reference.path, reference)
    if not by_path:
        return []
    synthetic_prompt = "\n".join(
        f"<{reference.raw_reference}>" for reference in sorted(
            by_path.values(), key=lambda item: (str(item.path).casefold(), str(item.path))
        )
    )
    return file_context_builder.build(synthetic_prompt)
