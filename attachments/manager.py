"""Coordinate provider-independent validation, attachment upload and safe retry."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .models import AttachmentRecord, AttachmentStatus, UploadReport
from .policy import classify_file, exclusion_reason, FileKind
from .provider import AttachmentProvider


class AttachmentManager:
    """Orchestrate uploads without knowing provider-specific browser details.

    No upload failures are retried without permission, including confirmed
    failures. UNKNOWN statuses require extra care because a file may be staged.
    Known account/session quota warnings are checked before any file selection.
    """

    def __init__(self, provider: AttachmentProvider, *, max_retries: int = 1) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.provider = provider
        self.max_retries = min(max_retries, 1)

    def upload(
        self,
        paths: Sequence[str | Path],
        *,
        retry_failed: bool = False,
        preflight: bool = True,
    ) -> UploadReport:
        records: dict[Path, AttachmentRecord] = {}
        normalized: list[Path] = []
        valid_paths: list[Path] = []
        seen: set[Path] = set()

        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            normalized.append(path)
            record = AttachmentRecord(path=path)
            records[path] = record

            if not path.exists() or not path.is_file():
                record.status = AttachmentStatus.FAILED
                record.message = "File does not exist or is not a regular file."
                continue

            # Explicit notebook references may be offered to the provider as
            # attachments. EditValidator still enforces cell-aware notebook edits.
            kind = classify_file(path)

            reason = exclusion_reason(path)
            if reason:
                record.status = (
                    AttachmentStatus.BLOCKED
                    if any(token in reason.casefold() for token in (
                        "excluded", "secret", "credential", "environment", "private-key",
                    ))
                    else AttachmentStatus.UNSUPPORTED
                )
                record.message = reason
                continue

            if kind == FileKind.UNKNOWN:
                record.status = AttachmentStatus.UNSUPPORTED
                record.message = "File type is not supported by the default attachment policy."
                continue

            valid_paths.append(path)

        if not valid_paths:
            return UploadReport(self.provider.name, list(records.values()))

        # Providers can only preflight limits that are explicitly visible in the
        # active UI. An invisible account quota cannot be reliably queried through
        # browser automation, so a clear preflight is not proof of available quota.
        if preflight:
            checker = getattr(self.provider, "preflight_upload_limit", None)
            if callable(checker):
                try:
                    warning = checker(valid_paths)
                except Exception:
                    # A failed preflight probe must not falsely claim quota or block
                    # the upload. Normal verification remains mandatory afterwards.
                    warning = None
                if warning:
                    message = f"Preflight detected a provider upload quota warning: {str(warning).strip()}"
                    for path in valid_paths:
                        records[path].status = AttachmentStatus.LIMIT_EXCEEDED
                        records[path].message = message
                    return UploadReport(self.provider.name, list(records.values()))

        self._run_provider_upload(valid_paths, records)

        retry_paths = [
            path for path in valid_paths
            if retry_failed
            and records[path].status == AttachmentStatus.FAILED
            and records[path].attempts < 1 + self.max_retries
        ]
        if retry_paths and self.max_retries:
            self._run_provider_upload(retry_paths, records, is_retry=True)

        # Omitted/pending provider records are ambiguous, never successes.
        for path in valid_paths:
            if records[path].status in {AttachmentStatus.PENDING, AttachmentStatus.UPLOADING}:
                records[path].status = AttachmentStatus.UNKNOWN
                records[path].message = "Provider did not return a final per-file status."

        return UploadReport(self.provider.name, list(records.values()))

    def _run_provider_upload(
        self,
        paths: list[Path],
        records: dict[Path, AttachmentRecord],
        *,
        is_retry: bool = False,
    ) -> None:
        try:
            results = self.provider.upload_files(paths)
        except Exception as error:
            # We cannot know whether the UI accepted files before an exception.
            for path in paths:
                records[path].attempts += 1
                records[path].status = AttachmentStatus.UNKNOWN
                records[path].message = (
                    "Provider upload raised an exception; status is uncertain "
                    f"({type(error).__name__})."
                )
            return

        result_map = {Path(item.path).expanduser().resolve(): item for item in results}
        for path in paths:
            current = records[path]
            current.attempts += 1
            result = result_map.get(path)
            if result is None:
                current.status = AttachmentStatus.UNKNOWN
                current.message = "Provider did not report this file; upload status is uncertain."
                continue
            current.status = result.status
            current.message = result.message
            current.verification_method = result.verification_method
            if is_retry and current.status == AttachmentStatus.FAILED:
                current.message = f"Retry failed: {current.message}".strip()
