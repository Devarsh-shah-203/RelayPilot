"""Classify provider-reported attachment limits conservatively."""

from __future__ import annotations

from enum import Enum


class UploadLimitKind(str, Enum):
    NONE = "NONE"
    BATCH_COUNT = "BATCH_COUNT"
    FILE_SIZE = "FILE_SIZE"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    UNKNOWN = "UNKNOWN"


_BATCH_TERMS = (
    "too many files", "too many attachments", "maximum number of files",
    "max number of files", "maximum files per message", "maximum files at once",
    "maximum number of attachments", "limit on the number of files",
    "only upload up to", "select fewer files", "files per message",
)
_SIZE_TERMS = (
    "file too large", "file is too large", "maximum file size", "max file size",
    "file size limit", "exceeds the maximum file size", "exceeds max file size",
    "file size exceeds", "file is too big", "maximum size for a file",
)
# Only phrases strongly suggesting an account/session quota belong here. Generic
# "upload limit reached" is ambiguous and must not be promoted to an account quota.
_QUOTA_TERMS = (
    "you've reached your file upload limit", "you have reached your file upload limit",
    "you've reached your upload limit", "you have reached your upload limit",
    "you have used all available uploads", "you've used all available uploads",
    "upload quota", "daily upload limit", "no more uploads",
    "file uploads are currently unavailable", "uploads are temporarily unavailable",
    "you can upload more files after", "upload allowance has been exhausted",
)


def classify_upload_limit(message: str) -> UploadLimitKind:
    """Classify a *specific observed error message*; avoid guessing on ambiguity."""
    text = (message or "").casefold()
    if not text:
        return UploadLimitKind.UNKNOWN
    if any(term in text for term in _SIZE_TERMS):
        return UploadLimitKind.FILE_SIZE
    if any(term in text for term in _BATCH_TERMS):
        return UploadLimitKind.BATCH_COUNT
    if any(term in text for term in _QUOTA_TERMS):
        return UploadLimitKind.PROVIDER_QUOTA
    return UploadLimitKind.UNKNOWN
