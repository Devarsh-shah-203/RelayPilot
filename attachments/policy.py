"""Provider-neutral file classification and delivery policy for RelayPilot.

Thresholds are heuristics for choosing a delivery method. They are not claims about
any provider's account, upload, or model-context limits; the active provider adapter
must still validate upload compatibility and completion in the live browser UI.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import DeliveryMode


DEFAULT_INLINE_TOKEN_THRESHOLD = 12_000
DEFAULT_ATTACHMENT_CHAR_THRESHOLD = 20_000
DEFAULT_ATTACHMENT_SIZE_BYTES = 1 * 1024 * 1024
DEFAULT_BULK_MAX_FILES = 200
DEFAULT_BULK_MAX_BYTES = 100 * 1024 * 1024


class FileKind(str, Enum):
    TEXT = "text"
    DOCUMENT = "document"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"
    NOTEBOOK = "notebook"
    ARCHIVE = "archive"
    BINARY = "binary"
    UNKNOWN = "unknown"


# Broad language/source-code set. Unknown extensions are also content-sniffed as
# UTF-8 text, so a newly encountered programming language need not be listed here
# merely to be attached. This set is specifically used for inline upload fallback.
SOURCE_CODE_EXTENSIONS = {
    ".py", ".pyi", ".pyx", ".pxd", ".ipynb.py",
    ".c", ".h", ".cc", ".hh", ".cpp", ".cxx", ".hpp", ".hxx",
    ".m", ".mm", ".cs", ".fs", ".fsi", ".fsx", ".vb", ".vbs",
    ".java", ".kt", ".kts", ".scala", ".sc", ".groovy", ".gradle",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".rb", ".php", ".swift", ".dart", ".lua", ".pl", ".pm",
    ".r", ".jl", ".ex", ".exs", ".hs", ".lhs", ".clj", ".cljs", ".cljc",
    ".erl", ".hrl", ".elm", ".nim", ".zig", ".d", ".cr", ".v", ".sv",
    ".sol", ".move", ".sql", ".graphql", ".gql", ".sh", ".bash", ".zsh",
    ".ps1", ".psm1", ".bat", ".cmd", ".f", ".for", ".f90", ".f95",
    ".cob", ".cbl", ".adb", ".ads", ".pas", ".pp", ".pro", ".vhd", ".vhdl",
    ".asm", ".s", ".plsql", ".prg", ".tcl", ".awk", ".sed", ".fish",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".xml", ".xsl", ".xslt",
}

TEXT_EXTENSIONS = {
    ".txt", ".text", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".json5",
    ".xml", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env.example", ".properties", ".editorconfig",
    ".gitignore", ".dockerignore", ".npmrc", ".dockerfile", ".makefile", ".lock",
    ".tf", ".hcl", ".gradle", ".cmake", ".mk", ".patch", ".diff", ".tex", ".bib",
} | SOURCE_CODE_EXTENSIONS

DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf", ".odt", ".ppt", ".pptx", ".epub",
}
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".ico", ".svg",
}
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".ods", ".xlsm", ".xlsb"}
ARCHIVE_EXTENSIONS = {
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz", ".tbz2",
    ".txz", ".zst",
}

# Inline fallback is intentionally narrower than the general set of text files:
# only .txt and source/programming-language files may be inlined after upload failure.
INLINE_FALLBACK_EXTENSIONS = SOURCE_CODE_EXTENSIONS | {".txt"}
INLINE_FALLBACK_FILENAMES = {"dockerfile", "makefile", "gnumakefile"}

# Never automatically upload or inline likely credentials/private key material.
SENSITIVE_EXACT_NAMES = {
    ".env", ".env.local", ".env.development", ".env.production", ".env.test",
    "id_rsa", "id_ed25519", "id_ecdsa", "credentials.json", "secrets.json",
    "service-account.json", "authorized_keys", "known_hosts",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
SENSITIVE_NAME_PATTERN = re.compile(
    r"(^|[._-])(secret|secrets|credential|credentials|access[._-]?tokens?|tokens?|api[._-]?keys?|private[._-]?keys?)([._-]|$)",
    re.IGNORECASE,
)
EXCLUDED_DIRECTORY_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "chrome-data", "chrome-data-default",
    ".relaypilot", ".ai-gateway", ".ai", "dist", "build", "target", "coverage", "site-packages",
    "secrets", ".secrets", "credentials", ".credentials", "credential",
    "private", "private_keys", "private-keys", "keys", ".ssh", ".aws", ".azure",
}


def _setting(name: str, default: str = "") -> str:
    """Read RelayPilot config, falling back to the original env var name."""
    return os.environ.get(name, os.environ.get(name.replace("RELAYPILOT_", "AI_GATEWAY_"), default))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = _setting(name).strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def inline_token_threshold() -> int:
    """Configured upper-bound token budget for the complete inline request."""
    return _env_int("RELAYPILOT_INLINE_TOKEN_THRESHOLD", DEFAULT_INLINE_TOKEN_THRESHOLD)


def attachment_character_threshold() -> int:
    """Text/source files longer than this are attached by default."""
    return _env_int("RELAYPILOT_ATTACHMENT_CHAR_THRESHOLD", DEFAULT_ATTACHMENT_CHAR_THRESHOLD)


def attachment_size_threshold_bytes() -> int:
    """Files larger than this are attached by default, irrespective of type."""
    raw = _setting("RELAYPILOT_ATTACHMENT_SIZE_MB", "1").strip()
    try:
        megabytes = float(raw)
        if not math.isfinite(megabytes) or megabytes <= 0:
            raise ValueError
        return max(1, int(megabytes * 1024 * 1024))
    except ValueError:
        return DEFAULT_ATTACHMENT_SIZE_BYTES


def bulk_max_files() -> int:
    return _env_int("RELAYPILOT_BULK_MAX_FILES", DEFAULT_BULK_MAX_FILES)


def bulk_max_bytes() -> int:
    return _env_int("RELAYPILOT_BULK_MAX_BYTES", DEFAULT_BULK_MAX_BYTES)


def _looks_like_utf8_text(path: Path) -> bool:
    """Conservatively content-sniff an unknown extension without reading it all."""
    try:
        with path.open("rb") as stream:
            sample = stream.read(8192)
    except (OSError, ValueError):
        return False
    if not sample or b"\x00" in sample:
        return False
    decoded = sample.decode("utf-8", errors="replace")
    replacement_count = decoded.count("\ufffd")
    if replacement_count > max(1, len(decoded) // 100):
        return False
    disallowed_controls = sum(
        1 for char in decoded
        if ord(char) < 32 and char not in "\n\r\t\f\b"
    )
    return disallowed_controls <= max(1, len(decoded) // 100)


def classify_file(path: str | Path) -> FileKind:
    path = Path(path)
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in {"dockerfile", "makefile", "gnumakefile", ".gitignore", ".env.example"}:
        return FileKind.TEXT
    if suffix == ".ipynb":
        return FileKind.NOTEBOOK
    if suffix in TEXT_EXTENSIONS:
        return FileKind.TEXT
    if suffix in DOCUMENT_EXTENSIONS:
        return FileKind.DOCUMENT
    if suffix in IMAGE_EXTENSIONS:
        return FileKind.IMAGE
    if suffix in SPREADSHEET_EXTENSIONS:
        return FileKind.SPREADSHEET
    if suffix in ARCHIVE_EXTENSIONS:
        return FileKind.ARCHIVE
    # Do not require every emerging programming-language extension to be listed.
    if path.is_file() and _looks_like_utf8_text(path):
        return FileKind.TEXT
    return FileKind.UNKNOWN


_SOURCE_CONTENT_PATTERN = re.compile(
    r"(?im)^\s*(?:#\s*(?:include|define|import|!/)|"
    r"(?:async\s+)?(?:def|class|fn|func|function|package|module|namespace|using|"
    r"interface|struct|enum|trait|impl|type|program-id|SELECT|CREATE|WITH|BEGIN)\b|"
    r"(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*(?:=|:)|"
    r"(?:public|private|protected|static)\s+(?:class|interface|enum|void|int|String)\b)|"
    r"=>|:=|</?\?php\b"
)


def _looks_like_source_code(path: Path) -> bool:
    """Recognize source files with unfamiliar extensions for inline fallback.

    This heuristic is deliberately applied only to otherwise-unknown extensions;
    prose/document formats such as Markdown, JSON, CSV, and YAML are not promoted
    to source-code fallback merely because they contain punctuation.
    """
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            sample = stream.read(8192)
        if not sample or b"\x00" in sample:
            return False
        text = sample.decode("utf-8-sig", errors="strict")
    except (OSError, UnicodeError):
        return False
    return bool(_SOURCE_CONTENT_PATTERN.search(text))


def is_inline_fallback_eligible(path: str | Path) -> bool:
    """Allow safely readable text/source files to fall back to prompt content.

    This restores the earlier inline-context route for text formats such as
    .md, .json, .yaml and .csv, not only .py/.txt. Size and UTF-8 validation
    happen separately inside the bounded fallback reader. Binary documents,
    notebooks, and known-sensitive paths are never silently converted to text.
    """
    path = Path(path)
    return classify_file(path) == FileKind.TEXT and exclusion_reason(path) is None


def exclusion_reason(path: str | Path, *, root: str | Path | None = None) -> str | None:
    """Return a policy exclusion reason, optionally relative to a bulk workspace root."""
    path = Path(path)
    lower_name = path.name.lower()
    if lower_name in SENSITIVE_EXACT_NAMES:
        return "sensitive filename is excluded by default"
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "private-key or certificate material is excluded by default"
    if SENSITIVE_NAME_PATTERN.search(lower_name):
        return "filename resembles credentials or secret material"
    if (lower_name.startswith(".env") and lower_name != ".env.example") or (lower_name.endswith(".env") and lower_name != ".env.example"):
        return "environment files are excluded by default"

    try:
        relative = path.relative_to(Path(root).resolve()) if root is not None else path
        parts = relative.parts
    except (ValueError, OSError):
        parts = path.parts
    # Generated-directory checks apply to directory components, not a file merely
    # named "build" or "target".
    if any(part.casefold() in EXCLUDED_DIRECTORY_NAMES for part in parts[:-1]):
        return "file is inside a generated, private, or local-state directory"

    kind = classify_file(path)
    if kind == FileKind.UNKNOWN:
        return "binary or unsupported file type"
    if kind == FileKind.NOTEBOOK:
        return "notebook files use the existing cell-aware context workflow"
    # Archives are valid attachment candidates. Provider compatibility is checked
    # using the active input's live accept attribute before selection.
    return None


@dataclass(frozen=True)
class DeliveryDecision:
    mode: DeliveryMode
    estimated_tokens_lower: int
    estimated_tokens_upper: int
    file_count: int
    reason: str


def estimate_tokens(characters: int) -> tuple[int, int]:
    """Return a rough range; it is an estimate, not a provider/model guarantee."""
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
    """Compatibility helper for callers that plan a complete request at once."""
    if token_threshold is None:
        token_threshold = inline_token_threshold()
    token_threshold = max(1, int(token_threshold))
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
