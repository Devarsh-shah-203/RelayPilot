"""Internal components for the provider-neutral browser-chat transport.

"""

from __future__ import annotations

import time
import unicodedata
from typing import List, Optional, Tuple
from threading import Event
import logging

from playwright.sync_api import Browser, Locator, Page

from .selectors import _BUTTON_SELECTOR, _COMPOSER_SELECTOR

logger = logging.getLogger(__name__)


class ComposerMixin:
    """Page discovery, composer interaction, insertion and submit-control logic."""

    def _find_chat_page(self) -> Optional[Page]:
        """Choose the open page with the strongest editable-composer signal.

        URL and page title are intentionally not required. If a chat composer is
        not available yet (for example, a sign-in page is open), fall back to the
        most recently opened non-blank page so send_prompt can return a useful
        readiness error rather than failing during object construction.
        """
        pages: List[Page] = []
        for context in self.browser.contexts:
            for page in context.pages:
                try:
                    if not page.is_closed():
                        pages.append(page)
                except Exception:
                    continue

        if not pages:
            return None

        best_page: Optional[Page] = None
        best_score = float("-inf")

        for page in pages:
            try:
                _locator, score = self._find_composer_on_page(
                    page, require_ready=False
                )
                if score is None:
                    score = -1000
                # Prefer a page that already looks like a conversation when
                # composer quality is otherwise similar.
                if page.locator(
                    '[role="log"], [role="feed"], article, '
                    '[data-testid*="conversation-turn" i]'
                ).count():
                    score += 8
                # Prefer the foreground browser tab when several provider tabs
                # expose equally plausible composers. This makes the provider
                # shown by RelayPilot match the tab the user is actually viewing.
                try:
                    foreground = page.evaluate(
                        "document.visibilityState === 'visible'"
                    )
                    if foreground:
                        score += 1000
                except Exception:
                    pass
                if score > best_score:
                    best_page = page
                    best_score = score
            except Exception:
                continue

        if best_page is not None and best_score >= 28:
            return best_page

        for page in reversed(pages):
            try:
                if page.url and page.url != "about:blank":
                    return page
            except Exception:
                continue
        return pages[-1]


    @staticmethod
    def _composer_score(locator: Locator) -> int:
        """Score an element structurally; visible labels are optional hints."""
        try:
            return int(
                locator.evaluate(
                    """(el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        const tag = (el.tagName || '').toLowerCase();
                        const type = (el.getAttribute('type') || '').toLowerCase();
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        const label = [
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('placeholder') || '',
                            el.getAttribute('title') || ''
                        ].join(' ').toLowerCase();
                        let score = 0;

                        if (el.isContentEditable) score += 75;
                        else if (tag === 'textarea') score += 65;
                        else if (role === 'textbox') score += 45;
                        else if (tag === 'input') score += 18;

                        if (type === 'search' || /search|find/.test(label)) score -= 28;
                        if (/password|e-mail|email|username|sign in|log in/.test(label)) score -= 35;
                        if (/message|chat|prompt|reply|ask/.test(label)) score += 8;

                        if (rect.width >= 350) score += 18;
                        else if (rect.width >= 180) score += 10;
                        else if (rect.width < 90) score -= 12;
                        if (rect.height >= 28) score += 5;
                        if (rect.bottom >= window.innerHeight * 0.55) score += 12;
                        if (rect.bottom >= window.innerHeight * 0.78) score += 6;
                        if (style.display === 'none' || style.visibility === 'hidden') score -= 100;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') score -= 18;
                        if (el.readOnly) score -= 30;

                        const form = el.closest('form');
                        if (form && form.querySelector('button, [role="button"], input[type="submit"]')) {
                            score += 12;
                        }
                        return score;
                    }"""
                )
            )
        except Exception:
            return -1000


    def _find_composer_on_page(
        self,
        page: Page,
        *,
        require_ready: bool = True,
    ) -> Tuple[Optional[Locator], Optional[int]]:
        """Return the highest-scoring visible composer candidate on a page."""
        try:
            candidates = page.locator(_COMPOSER_SELECTOR)
            candidate_count = candidates.count()
        except Exception:
            return None, None

        best_locator: Optional[Locator] = None
        best_score: Optional[int] = None

        for index in range(candidate_count):
            candidate = candidates.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                if require_ready and (
                    not candidate.is_enabled() or not candidate.is_editable()
                ):
                    continue
                score = self._composer_score(candidate)
                if score < 25:
                    continue
                if best_score is None or score > best_score:
                    best_locator = candidate
                    best_score = score
            except Exception:
                continue

        return best_locator, best_score


    def get_textbox(self) -> Locator:
        """Return the best composer candidate (legacy method name retained)."""
        locator, _score = self._find_composer_on_page(
            self.page, require_ready=False
        )
        if locator is not None:
            return locator
        # Return a valid, empty locator rather than None for callers that inspect
        # count()/wait_for() on this legacy method.
        return self.page.locator(".__relaypilot_missing_composer__")


    def check_textbox_ready(self) -> Tuple[bool, str]:
        """Return readiness and a readable explanation, for compatibility."""
        locator, _score = self._find_composer_on_page(
            self.page, require_ready=True
        )
        if locator is None:
            return False, "No visible, editable chat composer was found."
        return True, ""


    def _wait_for_composer(
        self, timeout: float, cancel_event: Event | None = None
    ) -> Optional[Locator]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return None
            locator, _score = self._find_composer_on_page(
                self.page, require_ready=True
            )
            if locator is not None:
                return locator
            delay = min(self.poll_interval, max(0.0, deadline - time.monotonic()))
            if cancel_event is not None:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)
        return None


    @staticmethod
    def _read_composer_text(locator: Locator) -> str:
        try:
            tag = locator.evaluate("el => (el.tagName || '').toLowerCase()")
            if tag in {"input", "textarea"}:
                return locator.input_value()
            return locator.inner_text()
        except Exception:
            return ""


    @staticmethod
    def _normalize_text(value: str) -> str:
        """Normalize harmless DOM/text-editor whitespace differences."""
        value = value or ""
        value = unicodedata.normalize("NFC", value)
        value = value.replace("\u00a0", " ")
        # Contenteditable editors can include invisible formatting characters
        # around pasted text. They must not cause an otherwise exact match to fail.
        value = value.translate({
            ord("\u200b"): None,
            ord("\u200c"): None,
            ord("\u200d"): None,
            ord("\u2060"): None,
            ord("\ufeff"): None,
        })
        return " ".join(value.split())


    @staticmethod
    def _scaled_prompt_timeout(
        prompt: str,
        base_timeout: float,
        *,
        minimum: float = 12.0,
        maximum: float = 120.0,
        reference_chars: int = 10_000,
    ) -> float:
        """Scale browser-operation timeouts to the amount of text being inserted.

        Character count is a lightweight proxy for payload size: it includes any
        attached project memory and file context without adding a tokenizer
        dependency. ``base_timeout`` applies at 10,000 characters by default; small prompts
        use a short floor and large prompts receive a bounded longer allowance.
        """
        size_factor = max(0.0, len(prompt)) / max(1, reference_chars)
        return min(maximum, max(minimum, base_timeout * size_factor))


    @staticmethod
    def _contains_text_with_only_extra_characters(
        expected: str,
        observed: str,
        max_extra: int = 2,
    ) -> bool:
        """Return True only if all expected characters occur in order in observed.

        Some contenteditable implementations expose a couple of extra characters
        in ``innerText`` compared with the text that was filled. This check allows
        a very small number of *additional* observed characters, but never accepts
        a candidate that is shorter than expected or that has lost/replaced any
        expected character. It is intentionally stricter than fuzzy similarity.
        Both inputs must already be normalized.
        """
        if len(observed) < len(expected):
            return False
        extra_budget = len(observed) - len(expected)
        if extra_budget < 0 or extra_budget > max_extra:
            return False

        expected_index = 0
        observed_index = 0
        extras = 0
        while expected_index < len(expected) and observed_index < len(observed):
            if expected[expected_index] == observed[observed_index]:
                expected_index += 1
                observed_index += 1
            else:
                # Skip only in the observed text. Never skip any expected prompt
                # character, because all of the requested content must be present.
                extras += 1
                observed_index += 1
                if extras > max_extra:
                    return False

        extras += len(observed) - observed_index
        return expected_index == len(expected) and extras == extra_budget and extras <= max_extra


    def _wait_for_prompt_in_composer(
        self, prompt: str, timeout: float, cancel_event: Event | None = None
    ) -> bool:
        """Poll visible composers until they contain the complete prompt.

        Do not rely only on the single highest-scoring composer: some chat UIs
        replace or reorder contenteditable nodes while a large prompt is rendered.
        Inspect visible candidates and compare multiple DOM text representations.
        Exact matches are preferred. A candidate up to two characters longer is
        accepted only if every expected character is still present in order; this
        handles tiny editor-added artifacts without accepting truncated prompts.
        """
        expected = self._normalize_text(prompt)
        deadline = time.monotonic() + max(0.0, timeout)
        last_visible: list[dict[str, object]] = []

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return False
            visible: list[dict[str, object]] = []
            try:
                candidates = self.page.locator(_COMPOSER_SELECTOR)
                candidate_count = candidates.count()
            except Exception:
                candidate_count = 0
                candidates = None

            for index in range(candidate_count):
                candidate = candidates.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    values = candidate.evaluate(
                        """el => ({
                            tag: (el.tagName || '').toLowerCase(),
                            value: typeof el.value === 'string' ? el.value : '',
                            innerText: typeof el.innerText === 'string' ? el.innerText : '',
                            textContent: typeof el.textContent === 'string' ? el.textContent : ''
                        })"""
                    )
                    variants = [
                        values.get("value", ""),
                        values.get("innerText", ""),
                        values.get("textContent", ""),
                    ]
                    normalized_variants = [self._normalize_text(v) for v in variants]
                    visible.append({
                        "tag": values.get("tag", "?"),
                        "lengths": [len(v) for v in normalized_variants],
                    })
                    exact_match = any(value == expected for value in normalized_variants)
                    near_match = False
                    extra_characters = 0
                    if not exact_match:
                        for value in normalized_variants:
                            if self._contains_text_with_only_extra_characters(
                                expected, value, max_extra=2
                            ):
                                near_match = True
                                extra_characters = len(value) - len(expected)
                                break

                    if exact_match or near_match:
                        self._last_prompt_verification = {
                            "matched": True,
                            "verification": "exact" if exact_match else "all_expected_chars_present_with_small_editor_extras",
                            "extra_observed_characters": extra_characters if near_match else 0,
                            "expected_chars": len(expected),
                            "visible_candidates": len(visible),
                            "observed": visible,
                        }
                        if near_match and not exact_match:
                            logger.debug(
                                "Prompt verified with %s editor-added character(s).",
                                extra_characters,
                            )
                        return True
                except Exception:
                    # DOM replacement during a React-style update is transient;
                    # retry on the next poll rather than treating it as failure.
                    continue

            last_visible = visible
            remaining = deadline - time.monotonic()
            if remaining > 0:
                delay = min(self.poll_interval, remaining)
                if cancel_event is not None:
                    cancel_event.wait(delay)
                else:
                    time.sleep(delay)

        # Keep privacy-safe diagnostics: character counts and element types only,
        # never the prompt or attached project-memory content.
        self._last_prompt_verification = {
            "matched": False,
            "expected_chars": len(expected),
            "visible_candidates": len(last_visible),
            "observed": last_visible,
            "timeout_seconds": timeout,
        }
        return False


    def _find_send_button(self, composer: Locator) -> Tuple[Optional[Locator], bool]:
        """Find the submit control structurally, without requiring its label.

        Buttons near the composer, inside its form/container, or aligned with its
        lower-right edge rank highest. Send-like labels are positive hints only;
        clearly unrelated controls (microphone, attachment, stop, etc.) are down-
        ranked so an unlabeled icon button can still be selected.
        """
        try:
            ranked = composer.evaluate(
                """editor => {
                    const selector = 'button, [role="button"], input[type="submit"]';
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const er = editor.getBoundingClientRect();
                    const editorForm = editor.closest('form');
                    const buttons = Array.from(document.querySelectorAll(selector)).filter(visible);

                    return buttons.map((button, index) => {
                        const br = button.getBoundingClientRect();
                        const label = [
                            button.innerText || '',
                            button.getAttribute('aria-label') || '',
                            button.getAttribute('title') || '',
                            button.getAttribute('data-testid') || '',
                            button.getAttribute('value') || ''
                        ].join(' ').toLowerCase();
                        let score = 0;

                        if (editorForm && editorForm.contains(button)) score += 48;

                        let ancestor = editor.parentElement;
                        let depth = 0;
                        while (ancestor && depth < 6) {
                            if (ancestor.contains(button)) {
                                score += Math.max(0, 27 - depth * 4);
                                break;
                            }
                            ancestor = ancestor.parentElement;
                            depth += 1;
                        }

                        const dx = Math.max(0, Math.max(er.left - br.right, br.left - er.right));
                        const dy = Math.max(0, Math.max(er.top - br.bottom, br.top - er.bottom));
                        const distance = Math.hypot(dx, dy);
                        if (distance < 35) score += 22;
                        else if (distance < 90) score += 15;
                        else if (distance < 180) score += 8;
                        else if (distance > 550) score -= 35;

                        if (Math.abs(br.right - er.right) < 135) score += 8;
                        if (br.bottom >= er.top - 80 && br.top <= er.bottom + 120) score += 7;
                        if ((button.getAttribute('type') || '').toLowerCase() === 'submit') score += 24;
                        if (/\\b(send|submit|ask|reply)\\b/.test(label)) score += 32;
                        if (/\\b(voice|microphone|dictation|attach|upload|stop generating|cancel generation|new chat|search)\\b/.test(label)) score -= 45;

                        return {
                            index,
                            score,
                            disabled: !!button.disabled || button.getAttribute('aria-disabled') === 'true'
                        };
                    }).sort((a, b) => b.score - a.score);
                }"""
            )
        except Exception:
            return None, False

        if not ranked:
            return None, False
        best = ranked[0]
        # A button with no meaningful relationship to the composer is not safe
        # to click. The threshold is low enough for an unlabeled nearby icon.
        if best.get("score", -1000) < 15:
            return None, False

        try:
            button = self.page.locator(_BUTTON_SELECTOR).nth(int(best["index"]))
            return button, bool(button.is_enabled()) and not bool(best.get("disabled"))
        except Exception:
            return None, False

