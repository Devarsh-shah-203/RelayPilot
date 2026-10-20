"""Coordinate provider-independent validation, attachment upload and safe retry."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .models import AttachmentRecord, AttachmentStatus, UploadReport
from .policy import classify_file, exclusion_reason, FileKind
from .provider import AttachmentProvider


class AttachmentManager:
    """Orchestrate attachments without knowing provider-specific browser details.

    Successes are silent. This class returns a structured report so the caller can
    decide whether to continue, ask the user, or cancel. Only explicitly FAILED
    files are retried once; UNKNOWN results are never blindly retried.
    """

    def __init__(self, provider: AttachmentProvider, *, max_retries: int = 1) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.provider = provider
        self.max_retries = min(max_retries, 1)  # safety policy: at most one retry

    def upload(self, paths: Sequence[str | Path]) -> UploadReport:
        normalized: list[Path] = []
        seen: set[Path] = set()
        records: dict[Path, AttachmentRecord] = {}

        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            record = AttachmentRecord(path=path)
            records[path] = record

            if not path.exists() or not path.is_file():
                record.status = AttachmentStatus.FAILED
                record.message = "file does not exist or is not a regular file"
                continue

            reason = exclusion_reason(path)
            if reason:
                # Documents/images and text are allowed; special-case excluded
                # unknown/private formats as policy blocks rather than upload errors.
                record.status = AttachmentStatus.BLOCKED if "excluded" in reason or "secret" in reason or "credential" in reason or "environment" in reason or "private-key" in reason else AttachmentStatus.UNSUPPORTED
                record.message = reason
                continue

            if classify_file(path) == FileKind.ARCHIVE:
                record.status = AttachmentStatus.UNSUPPORTED
                record.message = "archive uploads are opt-in and disabled by default"
                continue
            normalized.append(path)

        if normalized:
            self._run_provider_upload(normalized, records)

        retry_paths = [
            path for path in normalized
            if records[path].status == AttachmentStatus.FAILED
            and records[path].attempts < 1 + self.max_retries
        ]
        if retry_paths and self.max_retries:
            self._run_provider_upload(retry_paths, records, is_retry=True)

        # Provider omissions are ambiguous, never successes.
        for path in normalized:
            if records[path].status in {AttachmentStatus.PENDING, AttachmentStatus.UPLOADING}:
                records[path].status = AttachmentStatus.UNKNOWN
                records[path].message = "provider did not return a final per-file status"

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
                records[path].message = f"provider upload raised an exception; status is uncertain ({type(error).__name__})"
            return

        result_map = {Path(item.path).resolve(): item for item in results}
        for path in paths:
            current = records[path]
            current.attempts += 1
            result = result_map.get(path)
            if result is None:
                current.status = AttachmentStatus.UNKNOWN
                current.message = "provider did not report this file; upload status is uncertain"
                continue
            current.status = result.status
            current.message = result.message
            current.verification_method = result.verification_method
            if is_retry and current.status == AttachmentStatus.FAILED:
                current.message = f"retry failed: {current.message}".strip()
