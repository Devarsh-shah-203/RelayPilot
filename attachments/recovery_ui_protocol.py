"""Minimal acknowledgment protocol shared between upload policy and UI."""
from __future__ import annotations

CONFIRMED_RETRY_PREFIX = "RETRY_CHECKED:"
CONFIRMED_CLEANUP_PREFIX = "CLEANUP_CHECKED:"


def decode_recovery_choice(answer: str) -> tuple[str, bool]:
    """Decode a single-screen user-authorized retry; preserve CLI answers."""
    stripped = answer.strip()
    for prefix in (CONFIRMED_RETRY_PREFIX, CONFIRMED_CLEANUP_PREFIX):
        if stripped.upper().startswith(prefix):
            number = stripped[len(prefix):]
            if number.isdecimal() and int(number) > 0:
                return number, True
    return stripped, False
