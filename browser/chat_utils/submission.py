"""Internal components for the provider-neutral browser-chat transport.

"""

from __future__ import annotations

import time
from threading import Event
from typing import Optional

from .selectors import _GENERIC_MESSAGE_SELECTORS, _USER_SELECTORS


class SubmissionVerificationMixin:
    """Verify outgoing user turns and gather privacy-safe diagnostics."""

    def _user_message_count(self) -> int:
        try:
            return self.page.locator(", ".join(_USER_SELECTORS)).count()
        except Exception:
            return 0


    def _prompt_is_visible_as_user_message(self, prompt: str) -> bool:
        expected = self._normalize_text(prompt)
        if not expected:
            return False

        selectors = list(_USER_SELECTORS) + list(_GENERIC_MESSAGE_SELECTORS)
        for selector in selectors:
            try:
                matches = self.page.locator(selector)
                for index in range(matches.count() - 1, -1, -1):
                    item = matches.nth(index)
                    try:
                        if not item.is_visible():
                            continue
                        content = self._normalize_text(self._extract_element_text(item))
                        if expected and expected in content:
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False


    def _wait_for_submission(
        self,
        prompt: str,
        previous_user_count: int,
        previous_text: str = "",
        previous_assistant_count: Optional[int] = None,
        previous_generic_count: Optional[int] = None,
        timeout: Optional[float] = None,
        cancel_event: Event | None = None,
    ) -> bool:
        """Confirm submission from the outgoing turn, composer, or new response.

        Some chat UIs do not expose a user-message node or clear the composer in
        a way our existing checks can detect. A newly appearing assistant reply
        is also strong evidence that the click was accepted. These checks only
        observe the page; they never click Send again.
        """
        effective_timeout = self.submission_timeout if timeout is None else timeout
        deadline = time.monotonic() + effective_timeout
        composer_clear_since: Optional[float] = None
        expected = self._normalize_text(prompt)

        while time.monotonic() < deadline:
            if self._prompt_is_visible_as_user_message(prompt):
                return True

            try:
                current_count = self._user_message_count()
                if current_count > previous_user_count:
                    # Prefer matching the actual outgoing text, but retain the
                    # count as a fallback signal for UIs that transform messages.
                    if self._prompt_is_visible_as_user_message(prompt):
                        return True
            except Exception:
                pass

            # A response can begin even when user-message discovery fails. Check
            # against the pre-click baseline so an older answer is not enough.
            try:
                current_text = self.get_latest_assistant_text()
                current_assistant_count = self._assistant_count()
                current_generic_count = len(self._generic_message_items())

                new_assistant_node = (
                    previous_assistant_count is not None
                    and current_assistant_count > previous_assistant_count
                )
                new_generic_node = (
                    previous_generic_count is not None
                    and current_generic_count > previous_generic_count
                )
                new_assistant_text = bool(
                    current_text and current_text != previous_text
                )

                if new_assistant_node or new_generic_node or new_assistant_text:
                    return True
            except Exception:
                # Keep the established submission signals available even if a
                # provider's response DOM cannot be inspected during this poll.
                pass

            if cancel_event is not None and cancel_event.is_set():
                return False

            composer, _score = self._find_composer_on_page(
                self.page, require_ready=False
            )
            if composer is not None:
                current = self._normalize_text(self._read_composer_text(composer))
                if current != expected:
                    if not current:
                        if composer_clear_since is None:
                            composer_clear_since = time.monotonic()
                        elif time.monotonic() - composer_clear_since >= 1.0:
                            # Composer clearing after a single click is strong
                            # evidence of acceptance, even if turns are virtualized.
                            return True
                    else:
                        composer_clear_since = None

            time.sleep(self.poll_interval)
        return False


    def _response_diagnostic(self, latest_text: str = "") -> str:
        """Describe detector state without dumping the prompt or answer text."""
        try:
            title = self.page.title()
        except Exception:
            title = "<unavailable>"
        try:
            generic_count = len(self._generic_message_items())
        except Exception:
            generic_count = -1
        try:
            assistant_count = self._assistant_count()
        except Exception:
            assistant_count = -1
        try:
            active = self._is_generation_active()
        except Exception:
            active = False
        return (
            f"page_title={title!r}, assistant_nodes={assistant_count}, "
            f"generic_candidates={generic_count}, latest_text_chars={len(latest_text)}, "
            f"generation_active={active}"
        )

