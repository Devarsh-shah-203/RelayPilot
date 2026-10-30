"""Internal components for the provider-neutral browser-chat transport.

"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from playwright.sync_api import Locator

from .markdown_extraction import ANSWER_TO_MARKDOWN_JS

from .selectors import (
    _ASSISTANT_SELECTORS,
    _GENERIC_MESSAGE_SELECTORS,
    _USER_SELECTORS,
)


class ResponseExtractionMixin:
    """Extract assistant messages, response text, and generated-media markers."""

    @staticmethod
    def _extract_element_text(locator: Locator) -> str:
        """Serialize the visible assistant DOM to Markdown, preserving lists and tables.

        Only assistant-originated nodes are passed here by the caller. DOM
        serialization happens in the browser without introducing terminal UI
        dependencies into Playwright. Falls back to plain text on DOM failures.
        """
        try:
            result = locator.evaluate(ANSWER_TO_MARKDOWN_JS)
            return result or ""
        except Exception:
            try:
                return locator.inner_text().strip()
            except Exception:
                return ""


    def get_assistant_messages(self) -> Locator:
        """Return commonly marked assistant/model response elements."""
        return self.page.locator(_ASSISTANT_SELECTORS)


    def _generic_message_items(self) -> List[Tuple[Locator, str]]:
        """Return message-like elements from the first useful generic selector.

        The active outgoing prompt is excluded so a user bubble is not mistaken
        for the model's reply when a page provides no explicit role markers.
        """
        active_prompt = self._normalize_text(self._active_prompt or "")

        for selector in _GENERIC_MESSAGE_SELECTORS:
            items: List[Tuple[Locator, str]] = []
            try:
                matches = self.page.locator(selector)
                count = matches.count()
            except Exception:
                continue

            for index in range(count):
                item = matches.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    text = self._extract_element_text(item)
                    normalized = self._normalize_text(text)
                    if not normalized:
                        continue
                    # Exclude a generic bubble whose content is the outgoing prompt.
                    # Do not exclude every message that CONTAINS the prompt: a valid
                    # assistant answer may quote or echo the user's text verbatim.
                    # Marked user-only containers are filtered separately below.
                    if active_prompt and normalized == active_prompt:
                        continue
                    # Some sites mark user messages in an ancestor while their
                    # generic selector also matches that ancestor. Avoid treating
                    # that node as a model response.
                    try:
                        is_user_only = item.evaluate(
                            """el => {
                                const user = el.matches(
                                    '[data-message-author-role=\"user\"], [data-role=\"user\"], [data-author=\"user\"], [data-message-role=\"user\"]'
                                ) || el.querySelector(
                                    '[data-message-author-role=\"user\"], [data-role=\"user\"], [data-author=\"user\"], [data-message-role=\"user\"]'
                                );
                                const assistant = el.matches(
                                    '[data-message-author-role=\"assistant\"], [data-role=\"assistant\"], [data-author=\"assistant\"], [data-message-role=\"assistant\"]'
                                ) || el.querySelector(
                                    '[data-message-author-role=\"assistant\"], [data-role=\"assistant\"], [data-author=\"assistant\"], [data-message-role=\"assistant\"]'
                                );
                                return !!user && !assistant;
                            }"""
                        )
                    except Exception:
                        is_user_only = False
                    if is_user_only:
                        continue
                    items.append((item, text))
                except Exception:
                    continue

            if items:
                return items
        return []


    def _assistant_count(self) -> int:
        try:
            return self.get_assistant_messages().count()
        except Exception:
            return 0


    @staticmethod
    def _is_image_generation_request(prompt: str) -> bool:
        """Return whether the request explicitly asks the model to create visuals.

        This is intentionally a conservative heuristic: it affects timeout
        selection and whether newly rendered response images are reported as
        generated outputs, so ordinary web/article thumbnails should not trigger
        it merely because the prompt contains the word "image".
        """
        text = (prompt or "").lower()
        visual_nouns = (
            r"image|picture|illustration|artwork|photo|photograph|graphic|"
            r"logo|icon|wallpaper|poster|banner|thumbnail|visual|diagram|"
            r"drawing|painting|portrait|concept art|character|mascot|mockup|"
            r"flyer|infographic|cover|sticker|emblem|avatar|sprite"
        )
        creation_verbs = r"generate|create|draw|make|design|render|paint|illustrate|produce"
        if re.search(
            rf"\b(?:{creation_verbs})\b.{{0,160}}\b(?:{visual_nouns})\b",
            text,
            re.S,
        ):
            return True
        if re.search(r"\b(?:draw|paint|illustrate)\b.{0,100}", text, re.S):
            return True
        if re.search(
            rf"\b(?:{visual_nouns})\b.{{0,100}}\b(?:{creation_verbs})\b",
            text,
            re.S,
        ):
            return True
        # Some natural requests omit an explicit creation verb, e.g. "visual of BIT"
        # or "diagram of a segment tree". Treat these as potential image-generation
        # requests so they receive the longer timeout from the start.
        if re.search(
            r"\b(?:visual|image|picture|illustration|diagram|drawing|chart|infographic|"
            r"poster|wallpaper|logo|icon|portrait)\s+(?:of|for|showing)\b",
            text,
        ):
            return True
        if re.search(
            r"\b(?:show|give)\s+(?:me\s+)?(?:a|an)?\s*"
            r"(?:visual|image|picture|illustration|diagram|drawing|chart)\b",
            text,
        ):
            return True
        return False


    def _latest_response_locator(
        self,
        previous_assistant_count: Optional[int] = None,
        previous_generic_count: Optional[int] = None,
        previous_text: str = "",
    ) -> Optional[Locator]:
        """Locate the newest assistant response, including an image-only turn."""
        try:
            messages = self.get_assistant_messages()
            count = messages.count()
            if count and (
                previous_assistant_count is None
                or count > previous_assistant_count
            ):
                for index in range(count - 1, -1, -1):
                    candidate = messages.nth(index)
                    try:
                        if candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
        except Exception:
            pass

        try:
            items = self._generic_message_items()
            if items:
                if (
                    previous_generic_count is not None
                    and len(items) > previous_generic_count
                ) or (
                    previous_text
                    and self._normalize_text(items[-1][1])
                    != self._normalize_text(previous_text)
                ):
                    return items[-1][0]
        except Exception:
            pass

        try:
            messages = self.get_assistant_messages()
            for index in range(messages.count() - 1, -1, -1):
                candidate = messages.nth(index)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            pass
        try:
            items = self._generic_message_items()
            if items:
                return items[-1][0]
        except Exception:
            pass
        return None


    def _response_media_signature(
        self,
        previous_assistant_count: Optional[int] = None,
        previous_generic_count: Optional[int] = None,
        previous_text: str = "",
    ) -> Tuple[str, ...]:
        """Describe sizeable image/media elements in the latest assistant turn."""
        locator = self._latest_response_locator(
            previous_assistant_count=previous_assistant_count,
            previous_generic_count=previous_generic_count,
            previous_text=previous_text,
        )
        if locator is None:
            return ()
        try:
            entries = locator.evaluate(
                r"""root => {
                    const selector = 'img, canvas, video, [style*="background-image"]';
                    const nodes = [];
                    if (root.matches && root.matches(selector)) nodes.push(root);
                    nodes.push(...root.querySelectorAll(selector));
                    return nodes.map(node => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        const width = Math.max(rect.width || 0, node.naturalWidth || 0, node.videoWidth || 0);
                        const height = Math.max(rect.height || 0, node.naturalHeight || 0, node.videoHeight || 0);
                        const visible = rect.width > 0 && rect.height > 0 &&
                            style.display !== 'none' && style.visibility !== 'hidden';
                        if (!visible || width < 80 || height < 80) return null;
                        return [
                            (node.tagName || '').toLowerCase(),
                            node.currentSrc || node.src || '',
                            node.getAttribute('alt') || '',
                            node.getAttribute('title') || '',
                            Math.round(width), Math.round(height),
                            node.complete === false ? 'loading' : 'ready'
                        ].join('|');
                    }).filter(Boolean);
                }"""
            )
            return tuple(str(entry) for entry in entries or [])
        except Exception:
            return ()


    def _generated_image_signature(self) -> Tuple[str, ...]:
        """Return visible image-output markers created by the chat UI.

        Prefer explicit generated-image markers over arbitrary <img> elements:
        web previews and article thumbnails should not trigger an image-generated
        notification. ChatGPT currently exposes these as elements such as
        [data-testid="generated-image-preview"] with an accessible label.
        """
        selector = (
            '[data-testid="generated-image-preview"], '
            '[aria-label^="Generated image" i]'
        )
        try:
            nodes = self.page.locator(selector)
            count = nodes.count()
        except Exception:
            return ()

        entries: List[str] = []
        for index in range(count):
            node = nodes.nth(index)
            try:
                result = node.evaluate(
                    r"""node => {
                        const image = node.matches('img') ? node : node.querySelector('img');
                        const visual = image || node;
                        const rect = visual.getBoundingClientRect();
                        const style = window.getComputedStyle(visual);
                        const visible = rect.width > 0 && rect.height > 0 &&
                            style.display !== 'none' && style.visibility !== 'hidden';
                        if (!visible) return null;

                        const label = node.getAttribute('aria-label') ||
                            node.getAttribute('title') ||
                            (image && (image.getAttribute('alt') || image.getAttribute('title'))) ||
                            'generated-image';
                        const src = image ? (image.currentSrc || image.src || '') : '';
                        const width = Math.round(Math.max(rect.width || 0, image?.naturalWidth || 0));
                        const height = Math.round(Math.max(rect.height || 0, image?.naturalHeight || 0));
                        if (width < 50 || height < 50) return null;

                        const ready = !!image && image.complete && image.naturalWidth > 0;
                        // `key` remains stable as a loading image becomes ready.
                        const key = [label, src || 'pending', width, height].join('|');
                        return { key, ready };
                    }"""
                )
                if result and result.get('key'):
                    state = 'ready' if result.get('ready') else 'loading'
                    entries.append(f"{result['key']}|{state}")
            except Exception:
                continue
        return tuple(entries)


    @staticmethod
    def _media_keys(signature: Tuple[str, ...]) -> set[str]:
        """Strip the loading/ready suffix so load progress isn't a new image."""
        return {entry.rsplit('|', 1)[0] for entry in signature}


    def get_latest_assistant_text(self) -> str:
        """Read the latest reply, preferring current-turn generic nodes in flight.

        Some providers leave older assistant-role nodes in the DOM while using a
        different structure for the newest reply. During an active request, search
        the generic message candidates first so an old marked response cannot hide
        a newly rendered unmarked response. The active user prompt is filtered out
        by _generic_message_items().
        """
        if self._active_prompt:
            generic_items = self._generic_message_items()
            if generic_items:
                return generic_items[-1][1]

        try:
            messages = self.get_assistant_messages()
            for index in range(messages.count() - 1, -1, -1):
                message = messages.nth(index)
                try:
                    if not message.is_visible():
                        continue
                    text = self._extract_element_text(message)
                    if text:
                        return text
                except Exception:
                    continue
        except Exception:
            pass

        generic_items = self._generic_message_items()
        if generic_items:
            return generic_items[-1][1]
        return ""

