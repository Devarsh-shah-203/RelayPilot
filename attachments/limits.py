"""Classify provider-reported upload-limit messages for recovery decisions."""

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
    "only upload up to", "select fewer files",
)
_SIZE_TERMS = (
    "file too large", "file is too large", "maximum file size", "max file size",
    "file size limit", "exceeds the maximum file size", "exceeds max file size",
    "file size exceeds", "file is too big", "maximum size for a file",
)
_QUOTA_TERMS = (
    "you've reached your file upload limit", "you have reached your file upload limit",
    "you've reached your upload limit", "you have reached your upload limit",
    "upload quota", "daily upload limit", "file uploads are currently unavailable",
    "no more uploads", "upload limit reached", "upload limit has been reached",
    "upload limit exceeded", "uploads are temporarily unavailable",
    "try again later",
)


def classify_upload_limit(message: str) -> UploadLimitKind:
    """Classify an observed provider message; UNKNOWN is deliberately conservative."""
    text = (message or "").casefold()
    if not text:
        return UploadLimitKind.UNKNOWN
    if any(term in text for term in _SIZE_TERMS):
        return UploadLimitKind.FILE_SIZE
    if any(term in text for term in _BATCH_TERMS):
        return UploadLimitKind.BATCH_COUNT
    if any(term in text for term in _QUOTA_TERMS):
        return UploadLimitKind.PROVIDER_QUOTA
    if "upload" in text and any(word in text for word in ("limit", "quota", "maximum", "reached")):
        return UploadLimitKind.PROVIDER_QUOTA
    return UploadLimitKind.UNKNOWN
