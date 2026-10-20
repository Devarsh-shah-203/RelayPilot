"""File classification and provider-neutral inline-versus-attachment policy.

The policy is deliberately conservative. Provider file limits and accepted formats
are dynamic; the active provider adapter remains responsible for checking the live
browser input's `accept` attribute and interpreting upload errors.
"""

from __future__ import annotations

import math
import os
import re
from enum import Enum
from dataclasses import dataclass
from pathlib import Path

from .models import DeliveryMode


class FileKind(str, Enum):
    TEXT = "text"
    DOCUMENT = "document"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"
    NOTEBOOK = "notebook"
    ARCHIVE = "archive"
    BINARY = "binary"
    UNKNOWN = "unknown"


TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".json5",
    ".xml", ".html", ".htm", ".css", ".scss", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".java", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".kts", ".sh", ".ps1",
    ".sql", ".graphql", ".vue", ".svelte", ".ipynb", ".gitignore", ".dockerfile",
    ".makefile", ".bat", ".cmd", ".env.example", ".properties", ".r", ".jl",
}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf", ".odt", ".ppt", ".pptx", ".epub"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".ods"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2"}

# Never automatically attach likely credentials or private key material.
SENSITIVE_EXACT_NAMES = {
    ".env", ".env.local", ".env.development", ".env.production",
    "id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "secrets.json",
    "service-account.json", "authorized_keys", "known_hosts",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
SENSITIVE_NAME_PATTERN = re.compile(r"(^|[._-])(secret|secrets|credential|credentials|access_token|token|api_key|apikey|private_key)([._-]|$)", re.IGNORECASE)
EXCLUDED_DIRECTORY_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "chrome-data",
    "chrome-data-default", ".ai-gateway", ".ai", "dist", "build", "target",
}


@dataclass(frozen=True)
class DeliveryDecision:
    mode: DeliveryMode
    estimated_tokens_lower: int
    estimated_tokens_upper: int
    file_count: int
    reason: str


def classify_file(path: str | Path) -> FileKind:
    path = Path(path)
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in {"dockerfile", "makefile", ".gitignore", ".env.example"}:
        return FileKind.TEXT
    if suffix in TEXT_EXTENSIONS:
        return FileKind.NOTEBOOK if suffix == ".ipynb" else FileKind.TEXT
    if suffix in DOCUMENT_EXTENSIONS:
        return FileKind.DOCUMENT
    if suffix in IMAGE_EXTENSIONS:
        return FileKind.IMAGE
    if suffix in SPREADSHEET_EXTENSIONS:
        return FileKind.SPREADSHEET
    if suffix in ARCHIVE_EXTENSIONS:
        return FileKind.ARCHIVE
    return FileKind.UNKNOWN


def exclusion_reason(path: str | Path) -> str | None:
    """Return why a path should not be auto-attached, or None if eligible."""
    path = Path(path)
    lower_name = path.name.lower()
    if lower_name in SENSITIVE_EXACT_NAMES:
        return "sensitive filename is excluded by default"
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "private-key or certificate material is excluded by default"
    if SENSITIVE_NAME_PATTERN.search(lower_name):
        return "filename resembles credentials or secret material"
    if lower_name.startswith(".env") and lower_name != ".env.example":
        return "environment files are excluded by default"
    if any(part.lower() in EXCLUDED_DIRECTORY_NAMES for part in path.parts):
        return "file is inside a generated, private, or local-state directory"
    if classify_file(path) == FileKind.ARCHIVE:
        return "archives are not auto-attached; select supported files directly"
    if classify_file(path) in {FileKind.UNKNOWN, FileKind.NOTEBOOK}:
        reason = "notebook files use the existing cell-aware context workflow" if classify_file(path) == FileKind.NOTEBOOK else "file type is not in the default attachment allowlist"
        return reason
    return None


def estimate_tokens(characters: int) -> tuple[int, int]:
    """Return a rough token range; never treat this as a provider limit."""
    characters = max(0, int(characters))
    return math.ceil(characters / 5), math.ceil(characters / 3)


def choose_delivery_mode(
    *,
    prompt_chars: int,
    inline_file_chars: int,
    memory_chars: int = 0,
    edit_instruction_chars: int = 0,
    file_count: int = 0,
    contains_non_text_files: bool = False,
    token_threshold: int | None = None,
    many_file_threshold: int = 5,
    many_file_char_threshold: int = 12_000,
) -> DeliveryDecision:
    """Choose inline vs attachment delivery using size first, count second.

    Environment variable AI_GATEWAY_INLINE_TOKEN_THRESHOLD overrides the default
    threshold of 12,000 estimated upper-bound tokens.
    """
    if token_threshold is None:
        raw = os.environ.get("AI_GATEWAY_INLINE_TOKEN_THRESHOLD", "12000")
        try:
            token_threshold = max(1, int(raw))
        except ValueError:
            token_threshold = 12_000

    all_inline_chars = prompt_chars + inline_file_chars + memory_chars + edit_instruction_chars
    lower, upper = estimate_tokens(all_inline_chars)
    mode = DeliveryMode.INLINE
    reason = "estimated inline request is below configured threshold"

    if contains_non_text_files:
        mode = DeliveryMode.ATTACHMENTS
        reason = "one or more selected files require attachment handling"
    elif upper > token_threshold:
        mode = DeliveryMode.ATTACHMENTS
        reason = f"estimated upper-bound token count exceeds configured threshold ({token_threshold})"
    elif file_count >= many_file_threshold and inline_file_chars >= many_file_char_threshold:
        mode = DeliveryMode.ATTACHMENTS
        reason = "many files with a substantial combined inline payload"

    return DeliveryDecision(mode, lower, upper, file_count, reason)
