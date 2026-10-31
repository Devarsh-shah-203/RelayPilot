"""ChatGPT browser attachment adapter.

Opening the attachment menu is a *precondition* for checking quota messages that
only appear in that menu. The composer can expose the trigger through different
accessible labels, a stable test ID, or an icon-only menu trigger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .base import BrowserAttachmentProvider


class ChatGPTAttachmentProvider(BrowserAttachmentProvider):
    name = "chatgpt"
    domains = ("chatgpt.com", "chat.openai.com")

    def _upload_menu_is_open(self) -> bool:
        """Check only visible, upload-related menu surfaces, not chat history."""
        try:
            return bool(self.page.evaluate(r"""() => {
                const selectors = [
                    '[role="menu"]', '[data-testid*="attachment-menu" i]',
                    '[data-testid*="upload-menu" i]',
                    '[data-radix-menu-content]', '[data-radix-popper-content-wrapper]',
                ];
                for (const el of selectors.flatMap(s => Array.from(document.querySelectorAll(s)))) {
                    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                    if (!r.width || !r.height || r.bottom < 0 || r.top > innerHeight ||
                        cs.display === 'none' || cs.visibility === 'hidden' ||
                        el.getAttribute('data-state') === 'closed' ||
                        el.getAttribute('aria-hidden') === 'true' ||
                        el.closest('article, aside, nav, [data-message-author-role], [data-testid*="conversation-turn" i]')) continue;
                    const body = (el.innerText || '').slice(0, 2500);
                    if (/upload\s+files|add\s+from\s+library|more\s+uploads|create\s+image/i.test(body)) {
                        return true;
                    }
                }
                return false;
            }"""))
        except Exception:
            return False

    def _click_composer_upload_trigger(self) -> bool:
        """Find an icon-only trigger, tightly scoped to the active composer.

        Do not click arbitrary plus buttons on the page or a generic Send button.
        The candidate needs both a credible attachment/menu signal and a live
        adjacent message composer; coordinates must place it next to that editor.
        """
        try:
            handle = self.page.evaluate_handle(r"""() => {
                const editors = [
                    ...document.querySelectorAll(
                        '#prompt-textarea, [data-testid="prompt-textarea"], '
                        + 'form textarea, form [contenteditable="true"]'
                    )
                ].filter(e => {
                    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                    return r.width && r.height && s.visibility !== 'hidden' && s.display !== 'none';
                });
                const editor = editors[editors.length - 1];
                if (!editor) return null;
                const editorBox = editor.getBoundingClientRect();
                const host = editor.closest('form, [data-testid*="composer" i]')
                    || editor.parentElement?.parentElement?.parentElement;
                if (!host) return null;
                const candidates = Array.from(host.querySelectorAll('button, [role="button"]'));
                const matches = candidates.filter(button => {
                    const box = button.getBoundingClientRect(), style = getComputedStyle(button);
                    if (!box.width || !box.height || style.visibility === 'hidden' ||
                        style.display === 'none' || button.disabled ||
                        button.getAttribute('aria-disabled') === 'true') return false;
                    const horizontal = box.right >= editorBox.left - 140 &&
                                       box.left <= editorBox.right + 140;
                    const vertical = box.bottom >= editorBox.top - 150 &&
                                     box.top <= editorBox.bottom + 150;
                    if (!horizontal || !vertical) return false;
                    const label = [button.getAttribute('aria-label') || '',
                        button.getAttribute('title') || '',
                        button.getAttribute('data-testid') || '',
                        button.innerText || ''].join(' ').toLowerCase();
                    const menuTrigger = button.getAttribute('aria-haspopup') === 'menu';
                    const stronglyNamed = /(?:add|attach|upload).*(?:file|tool)|composer.*(?:plus|attach)|(?:file|attachment).*menu|^(?:\s*\+\s*)$/.test(label);
                    const weakTestId = /(?:plus|attachment|upload)[-_]?(?:btn|button|trigger)?/.test(label);
                    // A genuinely icon-only menu button can lack even a test
                    // ID. Accept it only at the composer editor's left edge;
                    // do not guess among unrelated model/settings dropdowns.
                    const leftEdge = box.left < editorBox.left + Math.min(editorBox.width * .3, 170);
                    return stronglyNamed || (menuTrigger && (weakTestId || leftEdge));
                });
                // Never guess if multiple attachment-like buttons are present.
                return matches.length === 1 ? matches[0] : null;
            }""")
            try:
                element = handle.as_element()
                if element is None:
                    return False
                element.click(timeout=2000)
                return True
            finally:
                handle.dispose()
        except Exception:
            return False

    def prepare_for_upload(self, paths: Sequence[Path]) -> None:
        # A session with an already-visible menu quota needs no more clicks:
        # clicking the toggle again can close the menu and hide that warning.
        if self.preflight_upload_limit(paths) or self._has_compatible_inputs(paths):
            return

        if not self._upload_menu_is_open():
            opened = self._click_named_upload_control((
                r"^add files and tools$",
                r"^add files and more$",
                r"^add files(?: and tools)?$",
                r"^attach files$",
                r"^add photos (?:and|&) files$",
                r"^open (?:attachment|file|upload) menu$",
                r"^attachments?$",
            ), roles=("button",))
            if not opened:
                # Newer ChatGPT builds may use an unlabelled plus icon. Only
                # inspect buttons tightly attached to the visible composer.
                opened = self._click_composer_upload_trigger()
            if opened:
                # The quota warning can appear only after the menu opens.
                if self.preflight_upload_limit(paths):
                    return
                # Allow React to mount the menu/input, then probe quota again.
                # Keep this separate from a direct upload attempt: when quota
                # is visible, no file should be selected.
                self._wait_for_compatible_inputs(paths, timeout=2.0)

        if self.preflight_upload_limit(paths):
            return
        if not self._has_compatible_inputs(paths):
            self._click_named_upload_control((
                r"^upload files$",
                r"^attach files$",
                r"^browse (?:files|computer)$",
            ), roles=("menuitem", "button"))

        if self.preflight_upload_limit(paths):
            return
        self._wait_for_compatible_inputs(paths, timeout=3.0)
