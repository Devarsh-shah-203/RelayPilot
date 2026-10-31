"""Parse the existing attachment recovery text protocol for an ergonomic UI.

This module contains no Textual imports. Attachment policy and authorization
remain owned by attachments.integration; these helpers only interpret prompts.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_OPTION = re.compile(r"^\s*\[(\d+)\]\s+(.+?)\s*$")
from attachments.recovery_ui_protocol import CONFIRMED_CLEANUP_PREFIX, CONFIRMED_RETRY_PREFIX


@dataclass(frozen=True)
class RecoveryChoice:
    number: str
    label: str

    @property
    def is_retry(self) -> bool:
        return self.label.casefold().startswith("retry upload")


@dataclass(frozen=True)
class RecoveryPrompt:
    explanation: str
    choices: tuple[RecoveryChoice, ...]
    has_uncertain_upload: bool


def parse_recovery_prompt(prompt: str) -> RecoveryPrompt | None:
    """Only the first attachment-decision menu uses the structured picker.

    Notebook cell and inline fallback menus continue to use normal text input.
    """
    if not prompt.lstrip().startswith("Attachment upload needs your decision"):
        return None
    lines = prompt.splitlines()
    choices: list[RecoveryChoice] = []
    description: list[str] = []
    for line in lines:
        match = _OPTION.match(line)
        if match:
            choices.append(RecoveryChoice(match.group(1), match.group(2)))
        elif line.strip() != "Choice:" and line.strip() != "Choose how to proceed:":
            description.append(line)
    if not choices:
        return None
    return RecoveryPrompt(
        explanation="\n".join(description).strip(),
        choices=tuple(choices),
        has_uncertain_upload="UNKNOWN means a file may already be present" in prompt,
    )


def encode_recovery_choice(menu: RecoveryPrompt, choice_number: str, *, cleanup_checked: bool) -> str:
    """Return the existing numeric answer, or one verified retry decision.

    The marker tells the old policy layer that the *same* UI already required
    an explicit checkbox before authorizing an UNKNOWN upload retry.
    """
    selected = next((c for c in menu.choices if c.number == choice_number), None)
    if selected is None:
        raise ValueError("Unknown attachment recovery choice")
    if menu.has_uncertain_upload and selected.label.casefold().startswith((
        "retry upload", "append eligible", "skip the unresolved"
    )):
        if not cleanup_checked:
            raise ValueError("Check Chrome and acknowledge that staged copies were cleared before continuing")
        prefix = CONFIRMED_RETRY_PREFIX if selected.is_retry else CONFIRMED_CLEANUP_PREFIX
        return f"{prefix}{selected.number}"
    return selected.number

