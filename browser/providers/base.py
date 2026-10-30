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
from attachments.limits import UploadLimitKind, classify_upload_limit


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
    # Conservative format family supported by RelayPilot. Live input accept
    # attributes still decide which of these the selected provider input accepts.
    allowed_kinds = {
        FileKind.TEXT, FileKind.DOCUMENT, FileKind.IMAGE, FileKind.SPREADSHEET,
        FileKind.ARCHIVE,
    }

    def __init__(self, page: Page, *, upload_timeout_floor: float = 30.0,
                 upload_timeout_ceiling: float = 600.0, poll_interval: float = 0.5) -> None:
        self.page = page
        self.upload_timeout_floor = upload_timeout_floor
        self.upload_timeout_ceiling = upload_timeout_ceiling
        self.poll_interval = poll_interval

    def prepare_for_upload(self, paths: Sequence[Path]) -> None:
        """Provider-specific menu-opening hook. Base provider needs no action."""

    def preflight_upload_limit(self, paths: Sequence[Path]) -> str | None:
        """Return a clearly visible account/session quota warning, if present.

        This deliberately checks only active accessible error surfaces, never page
        history or the sidebar. Providers do not expose remaining upload quota in a
        reliable universal API, so ``None`` means no explicit warning was observed,
        not that quota is guaranteed to be available.
        """
        snapshot = self._snapshot()
        for alert in snapshot.alerts:
            if classify_upload_limit(alert) == UploadLimitKind.PROVIDER_QUOTA:
                return alert.strip()[:500]
        return None

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
    def _extensions(path: Path) -> set[str]:
        """Return both ordinary and compound extensions for accept matching.

        File inputs sometimes advertise ``.tar.gz`` while ``Path.suffix`` exposes
        only ``.gz``. Keep both forms so the more specific provider filter and the
        generic compression suffix can be honored.
        """
        name = path.name.casefold()
        dotfiles = {".gitignore", ".dockerignore", ".npmrc", ".editorconfig"}
        if name in dotfiles:
            return {name}
        suffixes = {suffix.casefold() for suffix in Path(name).suffixes}
        suffixes.add(Path(name).suffix.casefold())
        # Include the combined suffix for compressed tar archives.
        for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
            if name.endswith(compound):
                suffixes.add(compound)
        return {item for item in suffixes if item}

    @classmethod
    def _extension(cls, path: Path) -> str:
        """Backward-compatible primary extension helper."""
        extensions = cls._extensions(path)
        name = Path(path).name.casefold()
        if name in {".gitignore", ".dockerignore", ".npmrc", ".editorconfig"}:
            return name
        for compound in (".tar.bz2", ".tar.gz", ".tar.xz", ".tar.zst"):
            if name.endswith(compound):
                return compound
        return Path(path).suffix.casefold()

    @classmethod
    def _accepts(cls, accept: str, path: Path) -> bool:
        tokens = [part.strip().casefold() for part in (accept or "").split(",") if part.strip()]
        # An absent accept filter or the universal MIME wildcard means the input
        # advertises no format restriction. The live provider still decides whether
        # its upload operation actually accepts the file.
        if not tokens or "*" in tokens or "*/*" in tokens:
            return True

        extensions = cls._extensions(path)
        mime, _encoding = mimetypes.guess_type(path.name)
        mime = mime.casefold() if mime else None
        kind = classify_file(path)

        for token in tokens:
            if token.startswith(".") and token in extensions:
                return True
            if token == "application/octet-stream" and kind in {
                FileKind.TEXT, FileKind.DOCUMENT, FileKind.IMAGE,
                FileKind.SPREADSHEET, FileKind.ARCHIVE,
            }:
                # A generic binary input is compatible with the supported families;
                # per-file validation and the real provider response remain decisive.
                return True
            if token.endswith("/*"):
                media_family = token[:-2]
                if mime and mime.startswith(media_family + "/"):
                    return True
                # MIME databases vary by OS. For a content-sniffed source file, a
                # text/* input can be matched even if Python has no MIME mapping.
                if media_family == "text" and kind == FileKind.TEXT:
                    return True
                # Some platforms lack MIME mappings for compressed and office
                # formats; their classified family still belongs to application/*.
                if media_family == "application" and kind in {
                    FileKind.TEXT, FileKind.DOCUMENT, FileKind.SPREADSHEET,
                    FileKind.ARCHIVE,
                }:
                    return True
                continue
            if mime and token == mime:
                return True

        # Providers may advertise a narrow, text-oriented input which doesn't list
        # newer source-language suffixes (for example .rs or .zig). Only positively
        # classified UTF-8 text can use this compatibility path; images, PDFs,
        # office documents, spreadsheets, and archives cannot match text filters.
        if kind == FileKind.TEXT:
            text_tokens = {
                ".txt", ".text", ".md", ".csv", ".json", ".py", ".js", ".ts",
                "text/plain", "text/*",
            }
            if any(token in text_tokens or token.startswith("text/") for token in tokens):
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
        # Do not scan the full page body here. Chat history/sidebar text is not
        # upload evidence and reading it can be expensive on long conversations.
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

            limit_kind = classify_upload_limit(candidate)
            if limit_kind in {
                UploadLimitKind.BATCH_COUNT,
                UploadLimitKind.FILE_SIZE,
                UploadLimitKind.PROVIDER_QUOTA,
            }:
                status = AttachmentStatus.LIMIT_EXCEEDED
            elif any(term in text for term in (
                "unsupported file", "file type isn't supported",
                "file type is not supported", "not supported for upload",
            )):
                status = AttachmentStatus.UNSUPPORTED
            elif any(term in text for term in (
                "upload failed", "failed to upload", "couldn't upload",
                "could not upload", "unable to upload", "failed to attach",
            )):
                status = AttachmentStatus.FAILED
            else:
                # A generic message such as "Upload limit reached" is not enough
                # to distinguish per-file size, batch count, or account quota.
                # Mark it uncertain to prevent automatic retry and false certainty.
                status = AttachmentStatus.UNKNOWN
            return status, candidate.strip()[:500] or matched
        return None

    @staticmethod
    def _has_persistent_upload_warning(before: PageSnapshot, after: PageSnapshot) -> bool:
        """Return true if the same upload-related warning remains visible.

        A warning that existed before file selection must not be ignored merely
        because it did not change text. Otherwise filename evidence alone could
        incorrectly mark a failed or blocked upload as attached.
        """
        previous = {item for item in before.alerts if any(term in item.casefold() for term in _ERROR_TERMS)}
        current = {item for item in after.alerts if any(term in item.casefold() for term in _ERROR_TERMS)}
        return bool(previous & current)

    @staticmethod
    def _map_batch_error(
        paths: Sequence[Path],
        before: PageSnapshot,
        after: PageSnapshot,
        error: tuple[AttachmentStatus, str],
    ) -> dict[Path, tuple[AttachmentStatus, str, str]]:
        """Map one provider error to cautious per-file outcomes.

        Only a known batch-count limit permits strong filename evidence to count
        as success for part of the batch. A size/quota/ordinary error plus a visible
        filename remains uncertain because providers may display failed-file cards.
        """
        status, message = error
        limit_kind = classify_upload_limit(message)
        mapped: dict[Path, tuple[AttachmentStatus, str, str]] = {}
        for path in paths:
            filename_visible = BrowserAttachmentProvider._new_filename_evidence(
                path, before, after
            )
            if status == AttachmentStatus.UNSUPPORTED:
                mapped[path] = (status, message, "provider_error")
            elif (
                not after.progress_active
                and filename_visible
                and status == AttachmentStatus.LIMIT_EXCEEDED
                and limit_kind == UploadLimitKind.BATCH_COUNT
            ):
                mapped[path] = (
                    AttachmentStatus.ATTACHED,
                    "Filename is visible in the active composer; a batch-count limit affected another item.",
                    "filename_visible_partial_batch",
                )
            elif status == AttachmentStatus.LIMIT_EXCEEDED and not after.progress_active:
                # Preserve explicit size/quota limits even if a failed-file chip is
                # visible. The chip is not proof that upload completed.
                mapped[path] = (status, message, "provider_error")
            elif after.progress_active or filename_visible:
                mapped[path] = (
                    AttachmentStatus.UNKNOWN,
                    "Provider reported an upload error, but filename/progress evidence is insufficient to determine this file's final state. "
                    + message[:300],
                    "uncertain_provider_error",
                )
            else:
                mapped[path] = (status, message, "provider_error")
        return mapped

    @staticmethod
    def _new_filename_evidence(path: Path, before: PageSnapshot, after: PageSnapshot) -> bool:
        """Require exact new filename evidence from the composer or changed attachment nodes.

        A filename appearing in history, a conversation title, or as a substring of
        another filename is insufficient evidence that the requested file was staged.
        """
        name = path.name
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
            re.IGNORECASE,
        )
        composer_evidence = (
            bool(pattern.search(after.composer_text))
            and not bool(pattern.search(before.composer_text))
        )
        previous_metadata = set(before.attachment_metadata)
        new_metadata = [
            item for item in after.attachment_metadata
            if item not in previous_metadata
        ]
        attachment_evidence = any(pattern.search(item) for item in new_metadata)
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
        pending_error: tuple[AttachmentStatus, str] | None = None
        while time.monotonic() < deadline:
            after = self._snapshot()
            observed_error = self._error_from_snapshot(before, after)
            if observed_error:
                pending_error = observed_error

            # A warning already visible before selection may persist unchanged. It
            # cannot prove this upload failed, but it also prevents us from treating
            # a newly visible filename as proof of success while the warning remains.
            if self._has_persistent_upload_warning(before, after) and not after.progress_active:
                warning_text = next(
                    (item for item in after.alerts if any(term in item.casefold() for term in _ERROR_TERMS)),
                    "An existing upload warning remained visible.",
                )
                return {
                    path: (
                        AttachmentStatus.UNKNOWN,
                        "An upload warning remained visible across this selection; the file state is uncertain. "
                        + warning_text[:300],
                        "persistent_upload_warning",
                    )
                    for path in paths
                }

            # If the provider reports a problem while upload activity is still
            # visible, wait for that activity to settle before deciding which files
            # succeeded. If it never settles, the final states remain UNKNOWN and
            # will require a user decision instead of an automatic duplicate retry.
            if pending_error and not after.progress_active:
                return self._map_batch_error(paths, before, after, pending_error)

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
        error = self._error_from_snapshot(before, final) or pending_error
        if error:
            return self._map_batch_error(paths, before, final, error)
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
