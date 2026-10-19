"""Internal components for the provider-neutral browser-chat transport.

"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .selectors import _BUTTON_SELECTOR, _COMPOSER_SELECTOR, _DIALOG_SELECTOR


class PageStateMixin:
    """Detect authentication, rate-limit, user-action, and generation states."""

    def detect_page_state(self) -> Tuple[Optional[str], Optional[str]]:
        """Detect visible dialogs indicating a blocked or interrupted request."""
        try:
            dialogs = self.page.locator(_DIALOG_SELECTOR)
            dialog_count = dialogs.count()
        except Exception:
            return None, None

        for index in range(dialog_count):
            dialog = dialogs.nth(index)
            try:
                text = dialog.inner_text().strip()
            except Exception:
                text = ""
            if not text:
                continue

            lower = text.lower()
            if any(
                keyword in lower
                for keyword in (
                    "usage limit",
                    "reached your limit",
                    "limit reached",
                    "message limit",
                    "rate limit",
                    "too many requests",
                    "you've run out",
                )
            ):
                return "RATE_LIMITED", text

            if any(
                keyword in lower
                for keyword in (
                    "session expired",
                    "please sign in",
                    "please log in",
                    "authentication required",
                    "your session has expired",
                )
            ):
                return "AUTH_REQUIRED", text

            return "USER_ACTION_REQUIRED", text

        # Keep URL checks generic and use them only as a fallback clue.
        try:
            if re.search(r"/(login|log-in|signin|sign-in|authenticate)(/|$|\?)", self.page.url, re.I):
                return "AUTH_REQUIRED", "The current page appears to require authentication."
        except Exception:
            pass

        return None, None


    def _is_generation_active(self) -> bool:
        """Detect streaming using explicit stop controls and generic busy signals.

        Do not infer completion merely because the response text has stopped
        changing: streaming can pause briefly between chunks. In particular,
        some interfaces expose an icon-only stop control whose only useful marker
        is a test ID such as ``stop-button`` rather than visible button text.
        """
        try:
            # Inspect visible controls first. Include common stop/cancel markers
            # from accessible names, titles, test IDs, and nested SVG labels.
            controls = self.page.locator(_BUTTON_SELECTOR)
            for index in range(controls.count()):
                control = controls.nth(index)
                try:
                    if not control.is_visible():
                        continue
                    label = control.evaluate(
                        """el => [
                            el.innerText || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('title') || '',
                            el.getAttribute('data-testid') || '',
                            el.getAttribute('data-state') || '',
                            ...Array.from(el.querySelectorAll('[aria-label], [title], [data-testid]'))
                                .flatMap(n => [
                                    n.getAttribute('aria-label') || '',
                                    n.getAttribute('title') || '',
                                    n.getAttribute('data-testid') || ''
                                ])
                        ].join(' ').toLowerCase().trim()"""
                    )
                    if re.search(
                        r"\b(stop|cancel|interrupt)(?:[-_ ](?:generating|generation|response|streaming))?\b",
                        label,
                    ):
                        return True
                except Exception:
                    continue

            # Busy/streaming attributes may be on the response container rather
            # than on a button. Check visible nodes, not hidden stale elements.
            busy = self.page.locator(
                '[aria-busy="true"]:visible, '
                '[data-streaming="true"]:visible, '
                '[data-loading="true"]:visible, '
                '[data-is-streaming="true"]:visible, '
                '[data-testid*="streaming" i]:visible, '
                '[data-testid*="generating" i]:visible'
            )
            if busy.count() > 0:
                return True
        except Exception:
            pass
        return False


    def _send_control_available(self) -> Optional[bool]:
        """Return whether a usable submit control is present, or None if unknown.

        This is supplementary completion evidence only. Providers vary, and some
        keep Send visible during generation, so this signal must never override an
        explicit stop/busy signal or be required on interfaces with unusual DOMs.
        """
        composer, _score = self._find_composer_on_page(self.page, require_ready=False)
        if composer is None:
            return None
        button, enabled = self._find_send_button(composer)
        if button is None:
            return None
        try:
            if not button.is_visible():
                return False
        except Exception:
            return None
        return bool(enabled)

