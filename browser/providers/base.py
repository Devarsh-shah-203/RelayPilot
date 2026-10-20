"""Shared Playwright attachment mechanics; provider subclasses customize preparation."""

from __future__ import annotations

import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from attachments.models import AttachmentRecord, AttachmentStatus
from attachments.policy import classify_file, exclusion_reason, FileKind


_FILE_INPUTS = 'input[type="file"]'
_ATTACHMENT_SELECTORS = (
    '[data-testid*="attachment" i], '
    '[data-testid*="file" i], '
    '[aria-label*="attachment" i], '
    '[aria-label*="file" i], '
    '[class*="attachment" i], '
    '[class*="file-upload" i]'
)
_PROGRESS_SELECTORS = (
    '[role="progressbar"]:visible, '
    '[aria-busy="true"]:visible, '
    '[data-testid*="uploading" i]:visible, '
    '[data-testid*="progress" i]:visible, '
    '[class*="upload-progress" i]:visible, '
    '[class*="uploading" i]:visible'
)
_ERROR_TERMS = (
    "file too large", "file is too large", "maximum file size", "max file size",
    "file size limit", "upload limit", "upload failed", "failed to upload",
    "couldn't upload", "could not upload", "unsupported file", "file type isn't supported",
    "file type is not supported", "not supported for upload", "too many files",
    "exceeds the maximum file size", "exceeds max file size", "file size exceeds",
    "file is too big", "upload quota", "no more uploads",
    "you've reached your file upload limit", "you have reached your file upload limit",
    "you've reached your upload limit", "you have reached your upload limit",
    "file uploads are currently unavailable", "uploads are temporarily unavailable",
    "maximum number of files", "maximum files per message", "maximum number of attachments",
    "select fewer files", "only upload up to", "limit on the number of files",
)
_LIMIT_TERMS = (
    "file too large", "file is too large", "maximum file size", "max file size",
    "file size limit", "upload limit", "too many files", "upload quota", "no more uploads",
    "you've reached your file upload limit", "you have reached your file upload limit",
    "you've reached your upload limit", "you have reached your upload limit",
    "daily upload limit", "file uploads are currently unavailable",
    "uploads are temporarily unavailable", "maximum number of files",
    "maximum files per message", "maximum number of attachments", "select fewer files",
    "only upload up to", "limit on the number of files",
)


@dataclass(frozen=True)
class FileInputInfo:
    index: int
    accept: str
    multiple: bool
    disabled: bool
    label: str


@dataclass(frozen=True)
class PageSnapshot:
    body_text: str
    attachment_count: int
    attachment_metadata: tuple[str, ...]
    progress_active: bool
    alerts: tuple[str, ...]
    # Text scoped to the active composer, never the conversation sidebar/history.
    composer_text: str = ""


class BrowserAttachmentProvider:
    """Base class for browser upload adapters.

    Provider implementations must only stage/attach files; they must not submit
    the user's chat prompt. The adapter returns per-file observations, not a single
    ambiguous batch boolean.
    """

    name = "base"
    domains: tuple[str, ...] = ()
    # Conservative format family supported by AI Gateway. Live input accept
    # attributes still decide which of these the selected provider input accepts.
    allowed_kinds = {
        FileKind.TEXT, FileKind.DOCUMENT, FileKind.IMAGE, FileKind.SPREADSHEET,
    }

    def __init__(self, page: Page, *, upload_timeout_floor: float = 30.0,
                 upload_timeout_ceiling: float = 600.0, poll_interval: float = 0.5) -> None:
        self.page = page
        self.upload_timeout_floor = upload_timeout_floor
        self.upload_timeout_ceiling = upload_timeout_ceiling
        self.poll_interval = poll_interval

    def prepare_for_upload(self, paths: Sequence[Path]) -> None:
        """Provider-specific menu-opening hook. Base provider needs no action."""

    def _composer_text(self) -> str:
        """Read only the active message composer, avoiding sidebar/chat-history text.

        The whole page body is not reliable evidence: older conversation titles may
        contain phrases like "Upload Limit" or even the test filename. Prefer the
        nearest form/composer container associated with the active input.
        """
        selectors = (
            '[data-testid="composer"]',
            'form:has(#prompt-textarea)',
            'form:has([data-testid="prompt-textarea"])',
            'form:has([contenteditable="true"])',
            'form:has(textarea)',
        )
        for selector in selectors:
            try:
                matches = self.page.locator(selector)
                for index in range(matches.count() - 1, -1, -1):
                    item = matches.nth(index)
                    if not item.is_visible():
                        continue
                    text = item.inner_text(timeout=1000).strip()
                    if text:
                        return text
            except Exception:
                continue

        # Fallback for pages where the contenteditable is not inside a form.
        try:
            return self.page.evaluate(
                """() => {
                    const target = document.querySelector('#prompt-textarea')
                        || document.querySelector('[data-testid="prompt-textarea"]')
                        || document.querySelector('[contenteditable="true"]')
                        || document.querySelector('textarea');
                    if (!target) return '';
                    let node = target;
                    for (let i = 0; i < 8 && node; i += 1, node = node.parentElement) {
                        if (node.matches('form, [data-testid*="composer" i]')) {
                            return node.innerText || '';
                        }
                    }
                    return target.parentElement?.parentElement?.innerText || '';
                }"""
            ) or ""
        except Exception:
            return ""

    def _has_compatible_inputs(self, paths: Sequence[Path]) -> bool:
        """Return True when every path has at least one compatible, enabled input."""
        inputs = self._get_file_inputs()
        return all(
            any(not item.disabled and self._accepts(item.accept, path) for item in inputs)
            for path in paths
        )

    def _wait_for_compatible_inputs(
        self, paths: Sequence[Path], *, timeout: float = 8.0
    ) -> bool:
        """Wait for a provider's menu-driven file inputs to appear or update."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._has_compatible_inputs(paths):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self.poll_interval, remaining))

    def _click_named_upload_control(
        self, patterns: Sequence[str], *, roles: Sequence[str] = ("button", "menuitem")
    ) -> bool:
        """Click a visible, explicitly named upload control without using DOM indices."""
        for pattern in patterns:
            regex = re.compile(pattern, re.IGNORECASE)
            for role in roles:
                try:
                    matches = self.page.get_by_role(role, name=regex)
                    count = matches.count()
                except Exception:
                    continue
                for index in range(count):
                    item = matches.nth(index)
                    try:
                        if not item.is_visible() or not item.is_enabled():
                            continue
                        item.click(timeout=2000)
                        return True
                    except Exception:
                        continue
        return False

    def upload_files(self, paths: Sequence[Path]) -> list[AttachmentRecord]:
        normalized = [Path(path).expanduser().resolve() for path in paths]
        results: dict[Path, AttachmentRecord] = {}
        eligible: list[Path] = []

        for path in normalized:
            record = AttachmentRecord(path)
            results[path] = record
            if not path.exists() or not path.is_file():
                record.status = AttachmentStatus.FAILED
                record.message = "File does not exist or is not a regular file."
                continue
            reason = exclusion_reason(path)
            if reason:
                record.status = (
                    AttachmentStatus.BLOCKED
                    if any(token in reason for token in ("excluded", "secret", "credential", "environment", "private-key"))
                    else AttachmentStatus.UNSUPPORTED
                )
                record.message = reason
                continue
            if classify_file(path) not in self.allowed_kinds:
                record.status = AttachmentStatus.UNSUPPORTED
                record.message = f"No default attachment handler for {path.suffix or path.name}."
                continue
            eligible.append(path)

        if not eligible:
            return list(results.values())

        try:
            self.prepare_for_upload(eligible)
        except Exception as exc:
            for path in eligible:
                results[path].status = AttachmentStatus.FAILED
                results[path].message = f"Could not open the provider's attachment interface ({type(exc).__name__})."
            return list(results.values())

        inputs = self._get_file_inputs()
        if not inputs:
            for path in eligible:
                results[path].status = AttachmentStatus.UNKNOWN
                results[path].message = (
                    "No file input is currently available after checking the provider UI; "
                    "the upload menu may need manual opening. Upload support could not be determined."
                )
            return list(results.values())

        # Group by the best compatible live file input. This matters for Gemini,
        # where document and image inputs can be separate. Re-query the input on
        # each batch because React-style UIs may replace the DOM node after select.
        grouped: dict[str, list[Path]] = {}
        for path in eligible:
            candidate = self._best_input(inputs, [path])
            if candidate is None:
                results[path].status = AttachmentStatus.UNKNOWN
                results[path].message = (
                    f"No currently exposed provider input accepts {path.suffix or path.name}. "
                    "The upload menu may need another step, or the provider may not support "
                    "this format. Check the browser before retrying; this state is not retried automatically."
                )
                continue
            group_key = candidate.accept
            grouped.setdefault(group_key, []).append(path)

        for accept_key, group in grouped.items():
            current_inputs = self._get_file_inputs()
            candidate = self._best_input(current_inputs, group, preferred_accept=accept_key)
            if candidate is None:
                for path in group:
                    results[path].status = AttachmentStatus.UNKNOWN
                    results[path].message = "The provider's file input changed before selection; upload status is uncertain."
                continue
            batches = [group] if candidate.multiple or len(group) == 1 else [[path] for path in group]
            for batch in batches:
                self._upload_batch(batch, results)

        return list(results.values())

    def _get_file_inputs(self) -> list[FileInputInfo]:
        output: list[FileInputInfo] = []
        try:
            locator = self.page.locator(_FILE_INPUTS)
            count = locator.count()
        except Exception:
            return output

        for index in range(count):
            element = locator.nth(index)
            try:
                data = element.evaluate(
                    """el => ({
                        accept: el.getAttribute('accept') || '',
                        multiple: !!el.multiple,
                        disabled: !!el.disabled,
                        aria: el.getAttribute('aria-label') || '',
                        title: el.getAttribute('title') || '',
                        name: el.getAttribute('name') || ''
                    })"""
                )
                output.append(FileInputInfo(
                    index=index,
                    accept=str(data.get("accept", "")),
                    multiple=bool(data.get("multiple")),
                    disabled=bool(data.get("disabled")),
                    label=" ".join(str(data.get(key, "")) for key in ("aria", "title", "name")).strip(),
                ))
            except Exception:
                continue
        return output

    @staticmethod
    def _extension(path: Path) -> str:
        if path.name.lower() in {".gitignore", ".dockerignore", ".npmrc", ".editorconfig"}:
            return path.name.lower()
        return path.suffix.lower()

    @classmethod
    def _accepts(cls, accept: str, path: Path) -> bool:
        if not accept.strip():
            return True
        extension = cls._extension(path)
        mime, _encoding = mimetypes.guess_type(path.name)
        for token in (part.strip().lower() for part in accept.split(",")):
            if not token:
                continue
            if token.startswith(".") and token == extension:
                return True
            if token.endswith("/*") and mime and mime.startswith(token[:-1]):
                return True
            if mime and token == mime.lower():
                return True
        return False

    @classmethod
    def _best_input(cls, inputs: Sequence[FileInputInfo], paths: Sequence[Path],
                    preferred_accept: str | None = None) -> FileInputInfo | None:
        ranked: list[tuple[int, FileInputInfo]] = []
        for candidate in inputs:
            if candidate.disabled:
                continue
            if preferred_accept is not None and candidate.accept != preferred_accept:
                continue
            matches = sum(cls._accepts(candidate.accept, path) for path in paths)
            if matches != len(paths):
                continue
            # Unrestricted inputs are preferred for mixed/otherwise ambiguous
            # formats. When accept is explicit, prefer the most targeted compatible
            # input. Multi-file inputs are preferred for grouped uploads.
            specificity = 2 if candidate.accept else 1
            score = matches * 100 + (30 if candidate.multiple else 0) + specificity
            ranked.append((score, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return ranked[0][1]

    def _upload_batch(self, paths: Sequence[Path], results: dict[Path, AttachmentRecord]) -> None:
        for path in paths:
            results[path].status = AttachmentStatus.UPLOADING
        before = self._snapshot()
        inputs = self._get_file_inputs()
        candidate = self._best_input(inputs, paths)
        if candidate is None:
            for path in paths:
                results[path].status = AttachmentStatus.UNKNOWN
                results[path].message = "No compatible file input remained when the upload was attempted."
            return

        try:
            current = self.page.locator(_FILE_INPUTS).nth(candidate.index)
            current.set_input_files([str(path) for path in paths], timeout=15_000)
        except PlaywrightError as exc:
            # A browser operation error may occur after change handlers have run.
            # Do not automatically retry an ambiguous selection.
            for path in paths:
                results[path].status = AttachmentStatus.UNKNOWN
                results[path].message = f"Playwright could not confirm file selection ({type(exc).__name__}); upload state is uncertain."
            return
        except Exception as exc:
            for path in paths:
                results[path].status = AttachmentStatus.UNKNOWN
                results[path].message = f"Unexpected selection error ({type(exc).__name__}); upload state is uncertain."
            return

        status_by_path = self._wait_for_batch(paths, before)
        for path in paths:
            status, message, method = status_by_path.get(
                path, (AttachmentStatus.UNKNOWN, "Could not verify attachment state.", "none")
            )
            results[path].status = status
            results[path].message = message
            results[path].verification_method = method

    def _snapshot(self) -> PageSnapshot:
        body_text = ""
        composer_text = ""
        attachment_metadata: tuple[str, ...] = ()
        attachment_count = 0
        progress_active = False
        alerts: tuple[str, ...] = ()
        try:
            body_text = self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            pass
        try:
            composer_text = self._composer_text()
        except Exception:
            pass
        try:
            metadata = self.page.locator(_ATTACHMENT_SELECTORS).evaluate_all(
                """nodes => nodes.map(el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    if (!r.width || !r.height || s.display === 'none' || s.visibility === 'hidden') return null;
                    return [
                        el.innerText || '', el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '', el.getAttribute('data-filename') || '',
                        el.getAttribute('data-file-name') || '', el.getAttribute('data-testid') || '',
                        el.className && typeof el.className === 'string' ? el.className : ''
                    ].join(' ').replace(/\\s+/g, ' ').trim();
                }).filter(Boolean)"""
            )
            attachment_metadata = tuple(str(item) for item in metadata or [])
            attachment_count = len(attachment_metadata)
        except Exception:
            pass
        try:
            progress_active = self.page.locator(_PROGRESS_SELECTORS).count() > 0
        except Exception:
            pass
        try:
            # Restrict error detection to live alerts, dialogs, toasts, and
            # clearly named error containers. Never classify arbitrary body text.
            alert_items = self.page.locator(
                '[role="alert"]:visible, [aria-live="assertive"]:visible, '
                '[role="dialog"]:visible, [data-testid*="toast" i]:visible, '
                '[data-testid*="error" i]:visible, [class*="upload-error" i]:visible, '
                '[class*="error-message" i]:visible'
            )
            values = []
            for i in range(alert_items.count()):
                try:
                    value = alert_items.nth(i).inner_text(timeout=1000).strip()
                    if value:
                        values.append(value)
                except Exception:
                    continue
            alerts = tuple(values)
        except Exception:
            pass
        return PageSnapshot(
            body_text=body_text,
            attachment_count=attachment_count,
            attachment_metadata=attachment_metadata,
            progress_active=progress_active,
            alerts=alerts,
            composer_text=composer_text,
        )

    @staticmethod
    def _error_from_snapshot(before: PageSnapshot, after: PageSnapshot) -> tuple[AttachmentStatus, str] | None:
        # Search only newly visible provider error surfaces. Looking at a broad
        # page-body diff caused false upload-limit diagnoses when sidebar history
        # happened to contain titles such as "Upload Limits Explained".
        candidates = [item for item in after.alerts if item not in before.alerts]
        for candidate in candidates:
            text = candidate.casefold()
            matched = next((term for term in _ERROR_TERMS if term in text), None)
            if not matched:
                continue
            if any(term in text for term in _LIMIT_TERMS):
                status = AttachmentStatus.LIMIT_EXCEEDED
            elif any(term in text for term in (
                "unsupported file", "file type isn't supported",
                "file type is not supported", "not supported for upload",
            )):
                status = AttachmentStatus.UNSUPPORTED
            else:
                status = AttachmentStatus.FAILED
            return status, candidate.strip()[:500] or matched
        return None

    @staticmethod
    def _new_filename_evidence(path: Path, before: PageSnapshot, after: PageSnapshot) -> bool:
        """Require new filename evidence from the composer or a changed attachment node.

        A filename appearing somewhere in the full page is insufficient because it
        might only be present in a historic message or a conversation title.
        """
        name = path.name.casefold()
        composer_evidence = (
            name in after.composer_text.casefold()
            and name not in before.composer_text.casefold()
        )
        previous_metadata = set(before.attachment_metadata)
        new_metadata = [
            item for item in after.attachment_metadata
            if item not in previous_metadata
        ]
        attachment_evidence = any(name in item.casefold() for item in new_metadata)
        return composer_evidence or attachment_evidence

    def _timeout_for(self, paths: Sequence[Path]) -> float:
        total_mib = sum(path.stat().st_size for path in paths if path.exists()) / (1024 * 1024)
        # A size-aware wait budget with a hard cap. Progress is checked throughout;
        # timeout alone is never interpreted as proof that an upload failed.
        return min(self.upload_timeout_ceiling, max(self.upload_timeout_floor, 20.0 + total_mib * 8.0))

    def _wait_for_batch(self, paths: Sequence[Path], before: PageSnapshot) -> dict[Path, tuple[AttachmentStatus, str, str]]:
        start = time.monotonic()
        deadline = start + self._timeout_for(paths)
        hard_deadline = start + self.upload_timeout_ceiling
        last_signature: tuple[object, ...] | None = None
        stable_since: float | None = None
        last_change = start
        count_delta_seen = False
        while time.monotonic() < deadline:
            after = self._snapshot()
            error = self._error_from_snapshot(before, after)
            if error:
                status, message = error
                return {path: (status, message, "provider_error") for path in paths}

            signature = (
                after.composer_text, after.attachment_metadata,
                after.attachment_count, after.progress_active, after.alerts,
            )
            now = time.monotonic()
            if signature != last_signature:
                last_signature = signature
                last_change = now
                stable_since = None
                # If the provider UI is still changing (for example, an upload
                # card or progress indicator is advancing), allow another bounded
                # interval. Never extend past the hard per-batch safety ceiling.
                if after.progress_active or after.attachment_count > before.attachment_count:
                    deadline = min(hard_deadline, max(deadline, now + 20.0))
            elif stable_since is None:
                stable_since = now

            explicit_names = {
                path: self._new_filename_evidence(path, before, after)
                for path in paths
            }
            all_names_visible = all(explicit_names.values())
            # Gemini's selected-file chips may not expose their filename in body
            # text. The diagnostic probe observed attachment DOM marker increases;
            # use that only as a provider-specific, low-confidence fallback signal.
            marker_delta = after.attachment_count - before.attachment_count
            if self.name == "gemini" and marker_delta >= len(paths) and marker_delta > 0:
                count_delta_seen = True

            stable_for = now - (stable_since if stable_since is not None else last_change)
            quiet = not after.progress_active
            if (all_names_visible or count_delta_seen) and quiet and stable_for >= 2.0:
                method = "filename_visible" if all_names_visible else "stable_attachment_dom_delta"
                return {
                    path: (AttachmentStatus.ATTACHED, "", method)
                    for path in paths
                }

            # An explicit upload spinner/status lets us keep waiting for slow files.
            # With no verifiable signal, keep waiting until the size-aware deadline.
            if time.monotonic() >= deadline and after.progress_active:
                # A visible active upload signal gets a brief grace period when
                # the DOM has changed recently; a stalled spinner eventually
                # becomes UNKNOWN rather than an automatic retry.
                if now - last_change < 60.0 and now < hard_deadline:
                    deadline = min(hard_deadline, now + 20.0)
                else:
                    break
            time.sleep(self.poll_interval)

        final = self._snapshot()
        error = self._error_from_snapshot(before, final)
        if error:
            status, message = error
            return {path: (status, message, "provider_error") for path in paths}
        # If file selection did change the attachment DOM but completion is not
        # proven, return UNKNOWN. The manager will not blindly retry this state.
        return {
            path: (
                AttachmentStatus.UNKNOWN,
                "File selection occurred, but upload completion could not be verified.",
                "none",
            )
            for path in paths
        }
